# Implement — 修复风控止损失效

范围：R1–R5（P0+P1）。R6/R7 待用户确认后另开或追加。

## 前置检查（动手前）

- [ ] 确认生产仍处于止血状态：`curl -s 127.0.0.1:8000/api/groups/0` 的 `enabled` 为 `false`，`next_run_at` 为 `null`；`ps -ef | grep spider_v2` 无输出。
- [ ] 工作区干净：`git status --short`（`.trellis/` 为未跟踪属正常）。
- [ ] 记录测试基线：`.venv/bin/python -m pytest tests/ -s` 应为 130 passed / 3 failed / 3 skipped。

## 实施清单

### 步骤 1 — R1 重复调度修复

- [ ] `src/services/scheduler_service.py:92`：`if task.group_id else None` → `if task.group_id is not None else None`。
- [ ] 同一函数内确认无第二处真值判断（全仓 `group_id` 审计在 PRD Background 中已完成，仅此一处）。
- [ ] 新增 `tests/unit/test_scheduler_service.py`：
  - `test_group_member_not_scheduled_individually_when_group_id_zero`：`group_id=0` + 可调度组 → job 集合只有 `group_0`（修复前该用例失败，是回归保护）。
  - `test_task_without_group_keeps_individual_job`：`group_id=None` → 存在 `task_{id}`。
  - `test_disabled_group_members_fall_back_to_individual_jobs`：组 `enabled=false` → 成员存在 `task_{id}`（R1 约束）。
  - 用 `asyncio.run` 包裹：`AsyncIOScheduler.start()` 需要运行中的事件循环；结束后 `shutdown()`。
- [ ] 验证：`.venv/bin/python -m pytest tests/unit/test_scheduler_service.py -s`。

### 步骤 2 — R2 风控异常穿透

- [ ] `src/scraper.py` 商品循环：在 `except PlaywrightTimeoutError` 与 `except Exception` 之间插入
      `except RiskControlError: stop_scraping = True; raise`（`stop_scraping` 在该作用域的初始化位置见 `src/scraper.py:546`）。
- [ ] 确认新分支位于宽泛 `except Exception`（`src/scraper.py:1156`）之前——顺序错误则修复无效。
- [ ] 确认 `_run_scrape_attempt` 外层 `except Exception`（`src/scraper.py:1178`）不吞 `RiskControlError`：该分支只对 `TargetClosedError` 返回、对 passport 重定向转 `LoginRequiredError`、其余 `raise`。
- [ ] 新增单元测试：断言风控响应判定与上抛（放到 `tests/unit/test_scraper_risk_control.py`，用最小假对象驱动判定分支，不启动 Playwright）。

### 步骤 3 — R3 风控接入熔断

- [ ] `src/scraper.py:122` 的 `pause_immediately` 标记元组追加：`"FAIL_SYS_USER_VALIDATE"`、`"baxia-dialog"`、`"J_MIDDLEWARE_FRAME_WIDGET"`。
- [ ] 确认 `_format_failure_reason`（`src/scraper.py:98`）只做空白归一与截断，不会改写标记文本。
- [ ] 确认风控路径不触达 `FAILURE_GUARD.record_success`（`src/scraper.py:1299`）：步骤 2 的重抛使该次尝试在 `break` 前就跳出，`record_success` 不执行。
- [ ] 新增单元测试：用 `tmp_path` 的 guard 路径 + `monkeypatch` 环境变量，断言风控原因一次调用即 `consecutive_failures=1`、`paused_until` 非空，且随后 `should_skip_start` 返回 `skip=True`；对照断言非风控原因（如"未知错误"）仍走阈值 3。

### 步骤 4 — R4 文案与行为一致

- [ ] `src/scraper.py:1021-1028` 三处文案改写：不再声称"程序将终止""安全退出"，改为说明"暂停随机时长后抛出风控信号，由上层终止本轮采集"。
- [ ] 保留 `random.randint(3, 60)` 长休眠与 `raise RiskControlError("FAIL_SYS_USER_VALIDATE")` 不变。
- [ ] 同批检查 `src/scraper.py:717-745` 的 `baxia-dialog` / `J_MIDDLEWARE_FRAME_WIDGET` 文案，同样去掉"程序将终止"的断言式表述（这两处确实会中止当前任务，措辞可保留但需与实际一致）。

### 步骤 5 — R5 登录态路径

- [ ] `src/failure_guard.py` 新增只读访问器 `remembered_cookie_path(task_key)`，读 `tasks[key].cookie_path`，缺失或非字符串返回 `None`。
- [ ] `src/services/process_service.py:54-63` `_resolve_cookie_path`：在 `account_state_file` 判定之后、`STATE_FILE` 回退之前，插入 guard 记忆路径分支。
- [ ] 新增单元测试：先 `record_failure(..., cookie_path="state/acc1.json")`，再断言 `_resolve_cookie_path` 返回该路径（用 `tmp_path` + `monkeypatch` 隔离 guard 文件）。
- [ ] 确认无第二处调用点需要同步（`_resolve_cookie_path` 仅被 `start_task` 使用）。

## 验证命令

```bash
# 定向
.venv/bin/python -m pytest tests/unit/test_scheduler_service.py tests/unit/test_scraper_risk_control.py tests/test_failure_guard.py tests/unit/test_process_service.py -s

# 全量（与基线逐项比对）
.venv/bin/python -m pytest tests/ -s
```

预期：新增用例全绿；存量 130 passed / 3 failed / 3 skipped 不变差（`test_frontend_build_paths`、`test_task_group::test_group_update_partial_apply` 的 NameError、`test_utils::test_save_to_jsonl` 为存量失败）。

## 高风险文件与回滚点

| 文件 | 风险 | 回滚 |
|------|------|------|
| `src/services/scheduler_service.py` | 调度注册规则变化影响所有定时任务 | `git revert`；回滚后组内任务会回到"组+单任务"双触发，故需同时保持任务禁用 |
| `src/scraper.py` | 异常传播路径变化可能影响正常商品处理 | `git revert`；观察一轮日志确认异常分支不影响正常路径后再部署生产 |
| `src/services/process_service.py` + `src/failure_guard.py` | 影响启动闸门与自动恢复 | `git revert`；guard 文件格式未变，无需数据迁移 |

不改数据库、不改 API、不改前端，无需构建 `web-ui/`。

## 生产恢复步骤（人工，非本任务自动执行）

- [ ] 代码提交并经用户确认后，提醒用户重启服务：`sudo systemctl restart goofish`（ZCode 会话内 sudo 被钩子拒绝，需用户执行）。
- [ ] 确认账号冷却已满足（建议 ≥24h，且建议更新 `state/` 登录态）。
- [ ] 在 Web UI 启用 4 个任务与任务组（注意：**只启用任务组而任务仍禁用时不会调度**；反之只启用任务会让成员回落到独立调度并同分钟并发）。
- [ ] 观察首轮日志：不应出现 `CRITICAL BLOCK DETECTED`；若出现，应看到任务被熔断暂停且收到通知。