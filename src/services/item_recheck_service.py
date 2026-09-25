"""过期复核服务：对长期未出现在搜索快照中的在库商品回访详情页定性。

设计对齐记录（2026-09-25，与用户讨论定型）：
- 背景：搜索结果按"新发布"排序 + 时间窗口过滤，老商品会因"发布时间过期/被新货挤出页预算"
  而从扫描路径消失——**搜索缺席不能证明售出/下架**，只能作为"嫌疑"触发。
- 定性唯一可靠路径是回访商品详情页：itemDO 为空/接口失效 → 下架或删除；
  售出标记 → 已售；仍在售 → 刷新价格快照（重置计时，顺带延续降价追踪）。
- 复核范围：result_items.status = 'active'（用户拍板：只复核未隐藏的商品）。
- 已售/下架判定采取保守策略：仅在有明确证据时打 expired，存疑一律视为在售；
  每次复核的原始证据追加写入 logs/recheck_debug.jsonl，便于后续校准判定规则。

配置（.env，均有代码默认值）：
- RECHECK_STALE_DAYS：最后快照距今超过该天数即视为嫌疑（默认 3）
- RECHECK_MAX_PER_RUN：每轮复核上限，最老优先（默认 20）
"""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import DETAIL_API_URL_PATTERN
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.services.price_history_service import (
    parse_price_value,
    record_market_snapshots,
)
from src.services.result_storage_service import update_item_status
from src.utils import log_time, random_sleep, safe_get

RECHECK_DEBUG_LIMIT_PER_ITEM = 1  # 每个商品证据日志条数上限（防重复刷盘）


def _debug_log_path() -> Path:
    return Path("logs") / "recheck_debug.jsonl"


def _append_debug_log(record: Dict[str, Any]) -> None:
    try:
        path = _debug_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:  # 证据日志失败不影响复核主流程
        pass


def get_stale_active_items(stale_days: int, limit: int) -> List[Dict[str, Any]]:
    """找出 status='active' 且最后快照距今超过 stale_days 天的商品（最老优先）。

    仅覆盖有快照记录的商品（搜索通道入库的）；从未产生快照的商品（如 feed_scan
    直注入）不在复核范围——它们没有"从扫描路径消失"可言。
    """
    bootstrap_sqlite_storage()
    # 注意：snapshot_time 落库格式为 ISO 'T' 分隔（datetime.isoformat() 默认），
    # cutoff 必须用同一分隔符，否则同日内字符串比较会错位
    cutoff = (datetime.now() - timedelta(days=stale_days)).isoformat(
        timespec="seconds"
    )
    with sqlite_connection() as conn:
        rows = conn.execute(
            """
            SELECT r.result_filename AS result_filename,
                   r.item_id         AS item_id,
                   r.title           AS title,
                   MAX(s.snapshot_time) AS last_seen
            FROM result_items r
            JOIN price_snapshots s ON s.item_id = r.item_id
            WHERE r.status = 'active'
            GROUP BY r.item_id
            HAVING MAX(s.snapshot_time) < ?
            ORDER BY last_seen ASC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()

    candidates: List[Dict[str, Any]] = []
    seen_items: set = set()
    for row in rows:
        if row["item_id"] in seen_items:
            continue  # 同一商品被多个任务命中时只复核一次
        seen_items.add(row["item_id"])
        # 该商品当前 active 的所有任务文件（打 expired 时全部更新）
        with sqlite_connection() as conn2:
            files = [
                r[0]
                for r in conn2.execute(
                    "SELECT DISTINCT result_filename FROM result_items "
                    "WHERE item_id = ? AND status = 'active'",
                    (row["item_id"],),
                ).fetchall()
            ]
        candidates.append(
            {
                "item_id": row["item_id"],
                "title": row["title"] or "",
                "last_seen": row["last_seen"],
                "result_filenames": files,
            }
        )
    return candidates


def _extract_price_from_item_do(item_do: Dict[str, Any]) -> Optional[float]:
    for key in ("soldPrice", "price", "sellPrice"):
        raw = item_do.get(key)
        if raw in (None, "", "NaN"):
            continue
        price = parse_price_value(raw)
        if price is not None:
            return price
    return None


def _detect_sold_signals(item_do: Dict[str, Any], page_text: str) -> List[str]:
    """收集售出信号（保守：命中才判已售，全部未命中视为在售）。"""
    signals: List[str] = []
    if item_do.get("soldStatus") in (1, "1", True):
        signals.append("itemDO.soldStatus=1")
    if item_do.get("sold") in (1, "1", True):
        signals.append("itemDO.sold=1")
    for marker in ("已售出", "该宝贝已售", "该商品已售", "已经售出"):
        if marker in page_text:
            signals.append(f"页面文本含'{marker}'")
    return signals


def _derive_keyword_from_filename(filename: str) -> str:
    return filename.replace("_full_data.jsonl", "").strip()


async def _recheck_one(context, candidate: Dict[str, Any]) -> Dict[str, Any]:
    """回访单个商品详情页，返回复核结果（不抛异常，异常计入 errors）。"""
    item_id = candidate["item_id"]
    result: Dict[str, Any] = {
        "item_id": item_id,
        "title": candidate["title"],
        "last_seen": candidate["last_seen"],
        "verdict": None,
        "evidence": {},
    }

    # 从任一关联任务文件中取商品链接
    link = ""
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT raw_json FROM result_items WHERE item_id = ? LIMIT 1",
            (item_id,),
        ).fetchone()
    if row and row["raw_json"]:
        try:
            raw = json.loads(row["raw_json"])
            link = str(((raw.get("商品信息") or {}).get("商品链接")) or "").strip()
        except Exception:
            link = ""
    if not link:
        result["verdict"] = "ERROR"
        result["evidence"]["error"] = "无法取到商品链接"
        return result

    page = await context.new_page()
    try:
        async with page.expect_response(
            lambda r: DETAIL_API_URL_PATTERN in r.url, timeout=25000
        ) as response_info:
            await page.goto(link, wait_until="domcontentloaded", timeout=25000)
        response = await response_info.value
        if not response.ok:
            result["verdict"] = "GONE"
            result["evidence"]["http_status"] = response.status
            return result

        detail_json = await response.json()
        ret_list = await safe_get(detail_json, "ret", default=[])
        ret_string = str(ret_list)
        if "FAIL_SYS_USER_VALIDATE" in ret_string:
            result["verdict"] = "RISK_CONTROL"
            result["evidence"]["ret"] = ret_list[:3]
            return result

        item_do = await safe_get(detail_json, "data", "itemDO", default={})
        result["evidence"]["ret"] = ret_list[:3]
        result["evidence"]["itemdo_keys"] = (
            sorted(item_do.keys())[:30] if isinstance(item_do, dict) else str(type(item_do))
        )

        if not item_do:
            # 接口失败或商品已不存在（下架/删除）
            result["verdict"] = "GONE"
            result["evidence"]["note"] = "itemDO 为空"
            return result

        page_text = ""
        try:
            page_text = await page.evaluate("document.body.innerText")
        except Exception:
            page_text = ""
        sold_signals = _detect_sold_signals(item_do if isinstance(item_do, dict) else {}, page_text or "")
        result["evidence"]["sold_signals"] = sold_signals

        if sold_signals:
            result["verdict"] = "SOLD"
            return result

        # 仍在售：刷新价格快照（重置计时 + 延续降价追踪）
        price = _extract_price_from_item_do(item_do if isinstance(item_do, dict) else {})
        if price is not None:
            keyword = _derive_keyword_from_filename(candidate["result_filenames"][0])
            item_ref = {
                "商品ID": item_id,
                "商品链接": link,
                "商品标题": item_do.get("title") or candidate["title"],
                "当前售价": str(price),
                "发布时间": item_do.get("gmtCreateStr") or "",
                "卖家昵称": item_do.get("sellerNick") or "",
            }
            record_market_snapshots(
                keyword=keyword,
                task_name="详情复核",
                items=[item_ref],
                run_id=f"recheck-{int(time.time())}",
                snapshot_time=datetime.now().isoformat(timespec="seconds"),
            )
            result["verdict"] = "ALIVE"
            result["evidence"]["refreshed_price"] = price
        else:
            # 在售但取不到价格：不刷新快照也不定罪，仅记录证据
            result["verdict"] = "ALIVE_NO_PRICE"
        return result
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def run_stale_recheck(context, task_config: Dict[str, Any]) -> Dict[str, Any]:
    """扫描收尾时调用：复核嫌疑商品并更新状态。任何异常都不影响主流程。"""
    stale_days = int(os.getenv("RECHECK_STALE_DAYS", "3") or 3)
    max_per_run = int(os.getenv("RECHECK_MAX_PER_RUN", "20") or 20)

    candidates = get_stale_active_items(stale_days, max_per_run)
    summary: Dict[str, Any] = {
        "candidates": len(candidates),
        "checked": 0,
        "sold": 0,
        "gone": 0,
        "alive": 0,
        "errors": 0,
        "aborted": False,
    }
    if not candidates:
        return summary

    task_name = task_config.get("task_name", "未命名任务")
    log_time(
        f"[复核] 发现 {len(candidates)} 个超过 {stale_days} 天未见快照的在库商品，开始详情复核（上限 {max_per_run}/轮）。"
    )

    for candidate in candidates:
        if summary["checked"] >= max_per_run:
            break
        try:
            result = await _recheck_one(context, candidate)
        except Exception as e:  # 单个复核失败不影响其余
            summary["errors"] += 1
            _append_debug_log(
                {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "item_id": candidate["item_id"],
                    "verdict": "EXCEPTION",
                    "error": str(e)[:300],
                }
            )
            await random_sleep(3, 6)
            continue

        summary["checked"] += 1
        verdict = result["verdict"]
        result["task_name"] = task_name
        _append_debug_log(
            {"time": datetime.now().isoformat(timespec="seconds"), **result}
        )

        if verdict == "RISK_CONTROL":
            # 风控信号：立即停止本轮复核，避免加重暴露
            summary["aborted"] = True
            log_time("[复核] 检测到风控验证信号，本轮复核中止。")
            break
        if verdict in ("SOLD", "GONE"):
            summary["sold" if verdict == "SOLD" else "gone"] += 1
            for filename in candidate["result_filenames"]:
                try:
                    await update_item_status(filename, candidate["item_id"], "expired")
                except Exception as e:
                    log_time(f"[复核] 更新状态失败 {candidate['item_id']}: {e}")
            log_time(
                f"[复核] {candidate['title'][:24]}... 判定 {'已售' if verdict == 'SOLD' else '失效/下架'}，已打 expired。"
            )
        elif verdict in ("ALIVE", "ALIVE_NO_PRICE"):
            summary["alive"] += 1
            log_time(f"[复核] {candidate['title'][:24]}... 仍在售，已重置计时。")
        else:
            summary["errors"] += 1

        await random_sleep(3, 6)

    log_time(
        f"[复核] 本轮完成：嫌疑 {summary['candidates']}，复核 {summary['checked']}，"
        f"已售 {summary['sold']}，失效 {summary['gone']}，在售 {summary['alive']}，"
        f"异常 {summary['errors']}{'，风控中止' if summary['aborted'] else ''}。"
    )
    return summary
