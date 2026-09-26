# Implement — 修复风控止损失效

范围：R1–R5（P0+P1）。R6/R7 已定另立任务（2026-09-26 评审）。

## 前置检查（动手前）

- [x] 确认生产仍处于止血状态：`/api/groups/0` 的 `enabled` 为 `false`、`next_run_at` 为 `null`；`ps -ef | grep spider_v2` 无输出。（2026-09-26 实测确认）
- [x] 工作区干净。
- [x] 记录测试基线：实测 130 passed / 3 failed / 3 skipped（与预期一致）。

## 实施清单

### 步骤 1 — R1 重复调度修复

- [x] `src/services/scheduler_service.py:92`：`if task.group_id else None` → `if task.group_id is not None else None`。
- [x] 同一函数内确认无第二处真值判断（`src/` 内仅此一处；**前端另有同类** `TasksTable.vue:53` / `useTasks.ts:68`，已登记为任务 `09-26-fix-frontend-group-id-zero`）。
- [x] 新增 `tests/unit/test_scheduler_service.py`：
  - `test_group_member_not_scheduled_individually_when_group_id_zero`：`group_id=0` + 可调度组 → job 集合只有 `group_0`（修复前实测 job 集合为 `{'group_0','task_1'}`，双触发复现）。
  - `test_task_without_group_keeps_individual_job`：`group_id=None` → 存在 `task_{id}`。
  - `test_disabled_group_members_fall_back_to_individual_jobs`：组 `enabled=false`/`cron=None` → 成员存在 `task_{id}`（R1 约束）。
  - 用 `asyncio.run` 包裹：`AsyncIOScheduler.start()` 需要运行中的事件循环；结束后 `shutdown()`。
- [x] 验证：4 passed（含参数化共 4 例）。

### 步骤 2 — R2 风控异常穿透

- [x] `src/scraper.py` 商品循环：在 `except PlaywrightTimeoutError` 与 `except Exception` 之间插入
      `except RiskControlError: stop_scraping = True; raise`（`stop_scraping` 在该作用域的初始化位置见 `src/scraper.py:546`）。现为 `:1165-1169`。
- [x] 确认新分支位于宽泛 `except Exception`（现 `:1170-1174`）之前——顺序错误则修复无效。
- [x] 确认 `_run_scrape_attempt` 外层 `except Exception`（现 `:1194-1203`）不吞 `RiskControlError`：该分支只对 `TargetClosedError` 返回、对 passport 重定向转 `LoginRequiredError`、其余 `raise`（AST 实测链条到任务级 `:1319-1323`）。
- [x] 新增单元测试：判定与抛出的行为用例 + 「handler 顺序与裸 raise」的结构契约（`tests/unit/test_scraper_risk_control.py`，不启动 Playwright）。
      **偏离说明**：为让判定可单测，原 `:1016-1035` 的内联块**纯搬移**为模块级 `_check_detail_risk_control`（`:105-125`）；验证子代理比对前后代码确认语义等价，仅打印文案按 R4 改写。

### 步骤 3 — R3 风控接入熔断

- [x] `src/scraper.py:122` 的 `pause_immediately` 标记元组追加：`"FAIL_SYS_USER_VALIDATE"`、`"baxia-dialog"`、`"J_MIDDLEWARE_FRAME_WIDGET"`。现为 `:145-154`，共五条标记。
- [x] 确认 `_format_failure_reason`（现 `:128-134`）只做空白归一与截断，不会改写标记文本。
- [x] 确认风控路径不触达 `FAILURE_GUARD.record_success`（现 `:1313`）：重抛使该次尝试直接跳出，`record_success` 不可达。
- [x] 新增单元测试：`tmp_path` 的 guard 路径 + `monkeypatch`，断言三种风控原因一次调用即 `consecutive_failures=1`、`paused_until` 非空、`should_skip_start=skip=True`、触发通知；对照断言非风控原因仍走阈值 3（修复前三个风控参数全部失败）。

### 步骤 4 — R4 文案与行为一致

- [x] 三处文案改写（现 `:116/:119-120/:123`）：不再声称"程序将终止""安全退出"，改为"长休眠后抛出风控信号、由上层终止本轮采集并暂停任务"。
- [x] 保留 `random.randint(3, 60)` 长休眠与 `raise RiskControlError("FAIL_SYS_USER_VALIDATE")` 不变。
- [x] 检查 `baxia-dialog` / `J_MIDDLEWARE_FRAME_WIDGET` 文案（现 `:745`、`:768`）：表述为「无法继续操作」「任务将在此处中止」，与实际行为一致，**未改动**。

### 步骤 5 — R5 登录态路径

- [x] `src/failure_guard.py` 新增只读访问器 `remembered_cookie_path(task_key)`（现 `:359-371`），读 `tasks[key].cookie_path`，缺失或非字符串返回 `None`。
- [x] `src/services/process_service.py` `_resolve_cookie_path`（现 `:54-69`）：判定顺序改为 任务 `account_state_file` → guard 记忆路径 → 存在的 `STATE_FILE` → `None`；原 `except Exception: pass` 改为打印一行。
- [x] 新增单元测试：`record_failure(..., cookie_path="state/acc1.json")` 后 `_resolve_cookie_path` 返回该路径（`tmp_path` + `monkeypatch` 隔离 guard 文件）；并断言任务配置优先、都取不到时返回 `None`。
- [x] 确认无第二处调用点需要同步（`_resolve_cookie_path` 仅被 `start_task` 使用）。

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

- [x] 代码提交后由用户重启服务：`sudo systemctl restart goofish`（**2026-09-26 11:16:54 已完成**，进程启动时间晚于代码改动，修复已生效）。
- [ ] 确认账号冷却已满足（建议 ≥24h：末次命中 2026-09-25 21:34 → 冷却到期 **2026-09-26 21:34**；建议同时更新 `state/` 登录态）。
- [ ] 在 Web UI 启用任务组与成员任务（当前库里剩 2 个任务 id 0/1，均属组 0；注意：**只启用任务组而任务仍禁用时不会调度**；反之只启用任务会让成员回落到独立调度）。
- [ ] 观察首轮日志：`journalctl -u goofish | grep 添加定时规则` 应只为组注册一条规则、无单任务规则；`ps -ef | grep spider_v2` 峰值 ≤1；若出现 `CRITICAL BLOCK DETECTED`，应看到本轮立即结束、`logs/task-failure-guard.json` 写入 `paused_until` 并收到通知（AC3 的端到端验证，单测不覆盖）。

## 收口记录（2026-09-26）

- 实现：R1–R5 全部落地，提交 `5598103`；文档与 spec 同步提交 `ff66acd`。
- 验证：`trellis-check` 子代理独立核验通过（三条传播链 AST 实测、修复前复现 9 个新用例失败、全量 145 passed / 3 failed / 3 skipped、运行态文件哈希未变）；主会话复跑定向 11 passed、全量 145/3/3 一致。
- 派生任务：`09-26-fix-frontend-group-id-zero`（前端同类假值，P2，提交 `c36bbfa`）；spec 侧新增「熔断标记是异常文案子串」的隐式契约 Warning 与 `importlib.reload` 测试陷阱。
- 未完成：上面三条生产步骤（冷却、启用、首轮观测）仍是本任务的生产验收尾巴。