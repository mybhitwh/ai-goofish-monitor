# 修复风控止损失效：重复调度与风控异常吞没

## Goal

让"命中闲鱼风控"成为一个不可忽略、能立即生效的止损信号。当前账号被风控后，程序的三层止损机制全部失效：一轮触发会并发跑多个任务、一次任务内连撞几十次、熔断器始终不打开。本任务修复这三处缺陷，使风控命中后一轮只跑一个任务、一次命中即停止采集、并自动打开 24 小时熔断暂停并通知。

用户价值：账号不会因为重复与并发请求被连续摩擦数小时；风控发生时用户会收到通知并看到明确的暂停状态，而不是看到"正常结束，本次运行共处理了 0 个新商品"。

## Background

2026-09-25 生产实例事故（`goofish.service`，任务组「iPad Air 8」id=0，4 个任务）：

- 风控首次命中 **17:50:58**，持续到 21:34。三个任务分别命中 32 / 34 / 33 次，合计约 99 次 `FAIL_SYS_USER_VALIDATE`；按小时分布为 17 时 47 次、18 时 37 次、21 时 15 次。三个任务在 17:51 同一秒首次命中。
- 仅**商品详情接口**被拦：搜索列表页正常，已存在商品走"已存在，跳过"不取详情，因此任务日志收尾为"处理了 0 个新商品"，外观正常。
- 重复调度证据：`journalctl -u goofish`，20:48:05 的 `reload_jobs` 同时打印了 4 条 `已为任务 '...' 添加定时规则: '0 9,21 * * *'` 和 1 条 `已为任务组 'iPad Air 8' 添加定时规则: '0 9,21 * * *' (模式: serial)`。21:04:37 一次触发拉起 4 个单任务爬虫（PID 68114/68116/68118/68120，并行）加 1 个任务组串行循环。
- 熔断状态证据：`logs/task-failure-guard.json` 中 4 个任务全部 `consecutive_failures: 0`、`paused_until: null`，且 `last_success_at` 被刷到 21:30:58 —— 熔断器认为任务成功。
- 已完成的止损：任务组与 4 个任务已置 `enabled=false`，调度器 `next_run_at` 全为 `None`，无残留爬虫进程。该状态保持到本任务验证通过且用户决定恢复。

## Requirements

### R1 (P0) 任务组 id=0 不得导致重复调度

`src/services/scheduler_service.py:92` 用 `if task.group_id else None` 判断任务是否属于任务组。`group_id` 为 `0` 是假值，而本实例任务组 id 恰为 0，导致"组内任务由任务组统一触发、跳过单独调度"的分支失效，组内任务被同时注册了单任务 job 和组 job。

约束：任务组禁用或未配置 cron 时，组内任务仍应回落到独立调度（与 `delete_group` 的"组内任务已恢复独立调度"语义一致）。`group_id` 为 `None` 的任务行为不变。

### R2 (P0) RiskControlError 必须穿透到任务级处理

`src/scraper.py:1156` 的商品级 `except Exception` 把 `RiskControlError`（`src/scraper.py:1035` 抛出）当作普通错误吞掉，只打印一行并 `continue` 到下一个商品，因此单次任务内会连续命中 8 次以上。

约束：风控异常必须在商品循环内被识别并终止本轮采集，向上传播到重试循环的任务级处理（`src/scraper.py:1305`），不得被商品级兜底吞掉；`PlaywrightTimeoutError` 等既有分支行为不变。

### R3 (P0) 风控命中必须立即打开熔断并通知

任务级处理（`src/scraper.py:1305`）捕获 `RiskControlError` 后只打印并 `break`，随后 `_notify_task_failure` 走默认阈值 3 才暂停。风控属于无需重试的确定性失败，应立即暂停。

约束：`FAIL_SYS_USER_VALIDATE`、`baxia-dialog`、`J_MIDDLEWARE_FRAME_WIDGET` 三种风控原因都按 `min_failures_to_pause=1` 打开熔断（沿用 `src/scraper.py:122` 已有的 `pause_immediately` 机制）。风控中止的那一次尝试不得调用 `record_success`（`src/scraper.py:1299`）。熔断状态文件格式不变，暂停时长与通知走既有 `FailureGuard` 语义。

### R4 (P1) 风控日志文案必须与实际行为一致

`src/scraper.py:1021-1028` 打印"程序将终止""现在将安全退出"，但实际只是 `sleep(3~60)` 后抛异常，而异常随后被吞掉、循环继续。文案与行为不符，误导排障。

约束：文案描述真实行为（暂停后抛出风控信号、由上层终止本轮采集），保留原有的随机长休眠。

### R5 (P1) 熔断器的登录态路径必须与实际使用的登录态一致

`src/services/process_service.py:54-63` 在任务未配置 `account_state_file` 时回退到 `src/config.py:11` 的 `STATE_FILE = "xianyu_state.json"`，该文件在仓库根目录不存在；实际登录态是 `state/acc1.json`。因此 `should_skip_start` 拿到的 `cookie_path` 为 `None`，"更新登录态后自动恢复"永远不触发，`record_failure` 也不落 `cookie_mtime`。

约束：`cookie_path` 必须指向任务实际使用的登录态文件；无法解析时保持返回 `None`（不得伪造路径），行为与现状一致地降级。

### R6 (P2 · 待用户决定) 错峰与并发上限

当前 4 个任务 cron 均为 `0 9,21 * * *`，同分钟启动。建议为同组任务加错峰与全局"同时只允许 1 个爬虫进程"的保护，使调度之外的路径（手动点击、组串行与单任务 job 并存）也无法并发采集。

### R7 (P2 · 待用户决定) 降低单轮请求量与反爬表现

`max_pages` 2–3、每页 30 条，新商品逐个取详情；建议评估降低轮次频率与单轮请求量，以及复用单一持久化浏览器上下文而非每任务独立进程。

## Acceptance Criteria

- [ ] AC1 构造一个 `group_id=0` 且任务组可调度的任务，`reload_jobs` 后调度器中只有 `group_0` job，不存在 `task_0` job（回归测试可复现修复前的错误行为）。
- [ ] AC2 构造 `group_id=None` 的任务，`reload_jobs` 后存在其单任务 job；构造任务组 `enabled=false` 的成员任务，`reload_jobs` 后其单任务 job 存在（R1 约束不被破坏）。
- [ ] AC3 商品详情返回 `FAIL_SYS_USER_VALIDATE` 时，`_run_scrape_attempt` 抛出 `RiskControlError` 而非正常返回；同一轮内后续商品不再发起详情请求（不再连撞）。
- [ ] AC4 风控原因经 `_notify_task_failure` 传入后，`FailureGuard` 的 `consecutive_failures` 为 1 且 `paused_until` 非空；随后 `should_skip_start` 返回 `skip=True`，即单次命中即暂停。
- [ ] AC5 风控中止的尝试不触发 `record_success`：熔断状态中 `last_success_at` 不被刷新、`consecutive_failures` 不被清零。
- [ ] AC6 `python -m pytest tests/ -s` 结果不差于基线（130 passed / 3 failed / 3 skipped），且新增用例全部通过。
- [ ] AC7 `logs/task-failure-guard.json` 文件格式与既有 `FailureGuard` 读写兼容，`tests/test_failure_guard.py` 全部通过。

## Out of Scope

- 不修改 `prompts/`、`jsonl/` 结果数据、`data/app.sqlite3` 业务库。
- 不重置或替换 `state/` 登录态、不修改 `.env`、不提交任何运行态文件。
- 不重新启用生产任务组与 4 个任务；恢复由用户决定（另见 Key Decisions 的冷却建议）。
- 不实现账号轮换 / 代理轮换策略的变更，不引入新的反检测技术（如指纹伪装）。
- 不修改 `spider_v2.py` 的进程退出码（影响面待单独评估）。

## Key Decisions

1. **范围**：本期做 R1–R5（P0+P1）。理由是 R4/R5 与本次事故同源且改动极小，R6/R7 涉及调度节奏与请求量的产品取舍，需要用户决定。
2. **风控用既有 `pause_immediately` 机制**（`src/scraper.py:122`）而不是新写一条 `record_failure` 调用：一处改动即可，保持与"确定性配置错误立即暂停"同一语义，避免双计数。
3. **R2 采用重抛而非就地 break**：重抛才能让任务级 `except RiskControlError`（`src/scraper.py:1305`）拿到 `last_error`，从而触发 `_notify_task_failure` 与熔断；就地 break 会让任务继续被当作成功。
4. **冷却与恢复**：建议账号冷却 ≥24 小时后、且修复提交完成，再由用户手动启用任务组与任务。任务保持禁用期间不会自行恢复。
5. **不在本任务引入 `trellis-implement` / `trellis-check` 子代理**：本会话未注册这两个 agent 类型，实现与检查在主会话完成，产物与验收标准不变。

## Open Questions

- 本期范围是否止于 R1–R5？是否把 R6（错峰与并发上限）一并纳入？（阻塞：影响 `implement.md` 的检查清单）