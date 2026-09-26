"""
风控止损测试：详情接口命中风控后的处理链

背景（2026-09-25 生产事故）：商品级宽泛 `except Exception` 吞掉了 RiskControlError，
同一轮内连续命中数十次，且本轮以「成功」收尾清空了熔断计数。
本文件固定修复后的两段契约：
1. 详情响应含 FAIL_SYS_USER_VALIDATE → 抛出 RiskControlError（保留随机长休眠）；
2. 风控原因一次命中即打开熔断（min_failures_to_pause=1），不刷新 last_success_at。
另含一条结构契约：商品详情循环的异常处理链必须把 RiskControlError 排在宽泛
Exception 之前并重抛（该循环依赖 Playwright，无法在单测内直接驱动）。

注意：`src/scraper` 会被 tests/unit/test_scraper_browser_channel.py 用
`importlib.reload` 重载，因此这里通过模块属性在调用时取异常类与函数，
避免直接 import 到的旧类身份失效。
"""
import ast
import asyncio
import json
from pathlib import Path

import pytest

import src.scraper as scraper
from src.failure_guard import FailureGuard

RISK_CONTROL_MARKERS = [
    "FAIL_SYS_USER_VALIDATE",
    "baxia-dialog",
    "J_MIDDLEWARE_FRAME_WIDGET",
]

SCRAPER_PATH = Path(__file__).resolve().parents[2] / "src" / "scraper.py"


async def _noop_notify(*_args, **_kwargs):
    return None


def _handler_type_name(handler: ast.ExceptHandler):
    node = handler.type
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _find_risk_control_try(tree: ast.Module):
    """定位商品详情循环的 try：唯一同时捕获 PlaywrightTimeoutError 与 RiskControlError 的 try"""
    matches = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        names = [_handler_type_name(handler) for handler in node.handlers]
        if "PlaywrightTimeoutError" in names and "RiskControlError" in names:
            matches.append((node, names))
    return matches


def test_detail_risk_control_raises_after_long_sleep(monkeypatch, capsys):
    """AC3：详情响应含 FAIL_SYS_USER_VALIDATE 时抛出 RiskControlError，保留 3~60 秒长休眠"""
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(scraper.asyncio, "sleep", fake_sleep)

    with pytest.raises(scraper.RiskControlError) as exc_info:
        asyncio.run(scraper._check_detail_risk_control({"ret": ["FAIL_SYS_USER_VALIDATE"]}))

    assert str(exc_info.value) == "FAIL_SYS_USER_VALIDATE"
    assert len(sleeps) == 1
    assert 3 <= sleeps[0] <= 60
    assert "CRITICAL BLOCK" in capsys.readouterr().out


def test_detail_response_without_risk_control_is_ignored(monkeypatch):
    """对照：正常详情响应不抛异常、不休眠，商品继续处理"""
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(scraper.asyncio, "sleep", fake_sleep)

    asyncio.run(scraper._check_detail_risk_control({"ret": ["SUCCESS::调用成功"]}))

    assert sleeps == []


@pytest.mark.parametrize("marker", RISK_CONTROL_MARKERS)
def test_risk_control_reason_pauses_immediately(tmp_path, monkeypatch, marker):
    """AC4/AC5：风控原因一次命中即熔断暂停，且不写 last_success_at（不清零失败计数）"""
    guard = FailureGuard(path=str(tmp_path / "guard.json"))
    monkeypatch.setattr("src.scraper.FAILURE_GUARD", guard)
    notifications = []

    async def fake_notify(product_data, reason):
        notifications.append((product_data, reason))

    monkeypatch.setattr("src.scraper.send_ntfy_notification", fake_notify)

    task_name = f"task-{marker}"
    asyncio.run(
        scraper._notify_task_failure(
            {"task_name": task_name, "keyword": "kw"}, marker, cookie_path=None
        )
    )

    decision = guard.should_skip_start(task_name, cookie_path=None)
    assert decision.skip is True
    assert decision.consecutive_failures == 1
    assert decision.paused_until is not None

    entry = json.loads((tmp_path / "guard.json").read_text(encoding="utf-8"))["tasks"][
        task_name
    ]
    assert entry["consecutive_failures"] == 1
    assert entry["paused_until"]
    assert entry.get("last_success_at") is None

    assert notifications and marker in notifications[0][1]


def test_non_risk_control_reason_keeps_default_threshold(tmp_path, monkeypatch):
    """对照：普通失败仍按默认阈值 3 才暂停，风控标记不得扩大化"""
    guard = FailureGuard(path=str(tmp_path / "guard.json"), threshold=3)
    monkeypatch.setattr("src.scraper.FAILURE_GUARD", guard)
    monkeypatch.setattr("src.scraper.send_ntfy_notification", _noop_notify)

    asyncio.run(
        scraper._notify_task_failure(
            {"task_name": "task-generic", "keyword": "kw"},
            "TimeoutError: 页面超时",
            cookie_path=None,
        )
    )

    decision = guard.should_skip_start("task-generic", cookie_path=None)
    assert decision.skip is False
    assert decision.consecutive_failures == 1


def test_detail_loop_reraises_risk_control_before_generic_exception():
    """结构契约：商品详情循环必须重抛 RiskControlError，且排在宽泛 Exception 之前

    商品详情循环依赖 Playwright，无法在单测内驱动；此用例锁定修复的关键结构，
    防止宽泛兜底再次吞掉风控（修复前该 try 不存在 RiskControlError 处理器，用例失败）。
    """
    tree = ast.parse(SCRAPER_PATH.read_text(encoding="utf-8"))
    matches = _find_risk_control_try(tree)

    assert len(matches) == 1, "商品详情循环的异常处理链结构已变化，请同步更新本用例"
    node, names = matches[0]

    assert "Exception" in names
    risk_index = names.index("RiskControlError")
    assert risk_index < names.index("Exception"), (
        "except RiskControlError 必须排在宽泛 except Exception 之前，否则风控会被吞掉"
    )

    handler = node.handlers[risk_index]
    assert any(
        isinstance(stmt, ast.Raise) and stmt.exc is None for stmt in handler.body
    ), "风控处理器必须重抛（raise）而不是就地 break/continue"