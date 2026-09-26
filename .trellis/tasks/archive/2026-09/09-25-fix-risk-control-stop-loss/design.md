# Design — 修复风控止损失效

## 1. 边界与影响面

改动集中在 3 个文件，均为最小切口：

| 文件 | 改动 | 关联需求 |
|------|------|----------|
| `src/services/scheduler_service.py` | `reload_jobs` 中组成员判定由真值判断改为 `is not None` | R1 |
| `src/scraper.py` | 商品循环新增 `except RiskControlError` 分支；风控原因加入 `pause_immediately`；三处风控文案改写 | R2 / R3 / R4 |
| `src/services/process_service.py` | `_resolve_cookie_path` 回退路径解析到真实登录态 | R5 |

不触碰：`src/domain/**`（模型与契约不变）、`src/api/**`（HTTP 接口不变）、`src/failure_guard.py`（熔断语义与文件格式不变）、前端。

## 2. 契约

### 2.1 调度契约（R1）

`reload_jobs(tasks, groups)` 的注册规则，修复后精确表述为：

- 任务未启用或无 cron → 不注册。
- 任务 `group_id is None` → 注册单任务 job `task_{id}`。
- 任务 `group_id is not None` 且该组 `can_schedule()`（`enabled and cron`）为真 → **只由组 job `group_{id}` 触发，不注册 `task_{id}`**。
- 任务 `group_id is not None` 但组不存在、组禁用或无 cron → 注册 `task_{id}`（保持"组不调度则回落到独立调度"的既有语义，与 `delete_group` 的提示一致）。

不变量：同一个任务不应同时存在 `task_{id}` 与可调度组 job；`reload_jobs` 依旧先 `remove_all_jobs()`，因此该不变量在每次重载后成立。

### 2.2 风控信号契约（R2）

`RiskControlError` 是"本轮采集必须立刻终止"的信号，传播路径固定为：

```
商品详情响应含 FAIL_SYS_USER_VALIDATE        (src/scraper.py:1035)
  → 商品循环 except RiskControlError: 置 stop_scraping=True; raise   (新增)
  → 页面循环退出（stop_scraping）／异常继续上抛
  → _run_scrape_attempt 外层 except Exception: 打印并 raise          (src/scraper.py:1178)
  → 重试循环 except RiskControlError: last_error=str(e); break       (src/scraper.py:1305)
  → _notify_task_failure(..., reason) → FAILURE_GUARD.record_failure  (src/scraper.py:130)
```

要点：
- 新分支必须放在宽泛 `except Exception` **之前**，否则不生效。
- 不使用就地 `break`：`break` 只退出商品循环，`_run_scrape_attempt` 仍会正常返回，于是 `record_success`（`src/scraper.py:1299`）把熔断计数清零——这正是事故中"正常结束 0 个新商品"的成因。重抛是该需求的核心，不是风格选择。
- `_run_scrape_attempt` 内层 `finally` 仍会执行 `analysis_dispatcher.join()` 与 `run_stale_recheck`。复核流程已在 `src/services/item_recheck_service.py:290-294` 检测到 `RISK_CONTROL` 后立即中止本轮，因此不额外改动；仅额外产生至多 1 次详情请求。
- `baxia-dialog` / `J_MIDDLEWARE_FRAME_WIDGET`（`src/scraper.py:730,753`）本就直抛不被吞，无需改动。

### 2.3 熔断契约（R3）

- 触发时机：`_notify_task_failure` 的 `pause_immediately` 判据新增风控标记（`FAIL_SYS_USER_VALIDATE`、`baxia-dialog`、`J_MIDDLEWARE_FRAME_WIDGET`），命中即以 `min_failures_to_pause=1` 调用 `record_failure`。
- 结果：`consecutive_failures=1`、`paused_until=now+pause_seconds`（默认 24h）、`should_notify=True`（当日未通知过），与既有确定性失败（代理/登录态缺失）行为一致。
- 不新增第二次 `record_failure` 调用：一次失败只计一次，避免计数翻倍导致暂停时长与通知语义漂移。
- `record_success` 不被风控路径触发（见 2.2 的重抛），因此暂停状态不会被随后的成功清零。
- 恢复路径保持既有两条：`paused_until` 到期，或登录态文件 mtime 变化触发 `record_success`（依赖 R5 修好后 `cookie_path` 才是真实路径）。

### 2.4 登录态路径契约（R5）

问题的不对称之处：**写侧是对的，读侧是错的**。写侧 `_notify_task_failure` 由 scraper 传入 `last_state_path`（`src/scraper.py:1288,1313`），即本次实际使用的登录态文件（`state/acc1.json`），已正确落进 guard；读侧 `start_task` 传的是 `_resolve_cookie_path(task_name)`，任务未配置 `account_state_file` 时退回 `STATE_FILE`，而该文件不存在，于是得到 `None`，`should_skip_start` 的自动恢复分支（`src/failure_guard.py:247-261`）因 `cookie_path` 为空而永不进入。

因此 `_resolve_cookie_path(task_name)` 的判定顺序改为：

1. 任务配置了 `account_state_file` 且非空 → 直接使用（现有行为）。
2. 否则读 guard 中该任务上次失败**实际使用过**的 `cookie_path` → 用它。
3. 否则仍退回 `STATE_FILE`（仅当文件存在），都取不到时返回 `None`。

选这个方案而不是在 `process_service` 里复刻 `_get_rotation_settings` + `resolve_account_runtime_plan` + RotationPool 的选号逻辑：后者会把账号解析规则复制到第二个地方，且轮换/固定策略下的实际选号结果本就无法在启动前可靠预测；而 guard 里记的是真值，自愈且零耦合。代价是首次失败前解析仍是旧行为（`STATE_FILE` 或 `None`），但自动恢复只在"已暂停"时才有意义，而暂停必然发生在一次失败之后。

为此在 `src/failure_guard.py` 增加一个只读访问器（如 `remembered_cookie_path(task_key) -> Optional[str]`），不改变文件格式与既有语义。

## 3. 权衡

- **重抛 vs 就地停止**：重抛会多走一层 `except Exception`（打印"爬取过程中发生未知错误"）再被任务级捕获。日志会多一行噪声，但换来了熔断与通知的正确触发，接受。
- **阈值 1 vs 阈值 3**：阈值 1 会在单次风控后暂停 24 小时。对一个已被风控的账号这是正确的保守选择；代价是偶发的、单次的风控也会暂停整天，用户可通过更新登录态或等暂停到期恢复。
- **不修退出码**：`spider_v2.py` 用 `asyncio.gather(return_exceptions=True)` 收集异常，失败任务仍以退出码 0 结束。串行组循环不读退出码，熔断器才是有效闸门，因此本期不改退出码（列入 Out of Scope），避免影响 CLI 测试与外部脚本。
- **`enabled=false` 的组会让成员回落到独立调度**：这是既有语义（R1 约束里显式保留）。运维含义是"只禁用组不等于停任务"，因此本次事故中同时禁用了组与 4 个任务；该含义会写入 PRD/设计并在恢复步骤里提醒。

## 4. 兼容与回滚

- 无数据库迁移、无 API 契约变化、无前端改动。
- `logs/task-failure-guard.json` 字段不变（仅 `cookie_path`/`cookie_mtime` 在有值时被填充），旧文件可直接被新代码读取。
- 回滚：`git revert` 对应提交即可；运行态无需迁移。若需在回滚后继续止血，保持任务组与任务 `enabled=false`。
- 生产恢复步骤（人工，且需账号冷却）：确认修复已部署 → 确认 `state/` 登录态已更新 → 在 Web UI 逐个启用 4 个任务与任务组 → 观察首轮日志无 `CRITICAL BLOCK`。

## 5. 验证策略

- R1：新增单元测试直接驱动 `reload_jobs`，用 APScheduler 实例断言 job 集合（`asyncio.run` 包裹启动调度器）。这是唯一能可靠复现 `group_id=0` 错误的层次。
- R2/R3：新增单元测试覆盖两段纯逻辑——商品的详情响应判定路径与 `_notify_task_failure` 的暂停阈值。`_run_scrape_attempt` 全链路依赖 Playwright，不在单测里造假页面；改为对"风控响应 → 抛 `RiskControlError`"与"风控原因 → `min_failures_to_pause=1`"分别断言，并在验收中通过日志确认线上行为。
- R4/R5：文案由人工核对；R5 通过解析函数的单元测试断言返回真实存在的登录态路径。
- 全量：`python -m pytest tests/ -s`，与基线（130 passed / 3 failed / 3 skipped）逐项比对，新增用例必须全绿。