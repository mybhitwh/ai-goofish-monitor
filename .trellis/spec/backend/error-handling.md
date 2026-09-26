# 错误处理指南

> 本文件描述本仓库**实际**的错误处理做法，含 2026-09-25 生产事故的教训。
> 读者：后续 AI 子代理与新同事。最后核对：2026-09-26（风控止损修复 R1–R5 已落地，任务 `.trellis/tasks/09-25-fix-risk-control-stop-loss/`）。
> 日志格式与级别见 `logging-guidelines.md`；测试基线与提交规范见 `quality-guidelines.md`。

---

## 1. 异常类型与语义

| 类型 | 定义位置 | 语义 | 处理方式 |
| --- | --- | --- | --- |
| `RiskControlError(Exception)` | `src/scraper.py:66` | 闲鱼风控/验证命中：`baxia-dialog`（`:756`）、`J_MIDDLEWARE_FRAME_WIDGET`（`:779`）、`FAIL_SYS_USER_VALIDATE`（`:125`，`_check_detail_risk_control`） | 确定性失败：中止本轮采集并向上传播，不轮换重试；三条原因一次命中即熔断（§3.2） |
| `LoginRequiredError(Exception)` | `src/scraper.py:70` | 跳转到 passport/mini_login，登录态失效 | 确定性失败：中止并交由熔断暂停，不重试 |
| `PlaywrightTimeoutError` | `playwright.async_api.TimeoutError` 别名，`src/scraper.py:9-13` | 元素/响应等待超时 | 多数场景是「可继续」：筛选失败、广告弹窗未出现等只打日志继续 |
| `EmptyAIResponseError(ValueError)` | `src/services/ai_response_parser.py:8` | AI 返回空内容/缺 message | 被 `ai_handler` 视为可重试，内部重试耗尽后抛出 |
| `NotificationSettingsValidationError(ValueError)` | `src/services/notification_config_service.py:80` | 通知配置校验失败 | 路由层转 422 |
| `ValueError` | 服务层惯例 | 非法输入：文件名、status 值、空 key | 路由层统一转 400 |

- `RiskControlError` / `LoginRequiredError` 不继承公共基类，也不带字段，靠类型本身传达「不要重试」。
- `retry_on_failure(retries, delay)`（`src/utils.py:18-48`）是通用异步重试装饰器：捕获 `APIStatusError`/`HTTPError`、`json.JSONDecodeError` 与其余 `Exception`，**重试耗尽后 `return None` 而不是抛出**。它只用于旁路动作：`_download_single_image`（`src/ai_handler.py:116`）与 `send_ntfy_notification`（`:279`）。调用方必须判 `None`（图片下载按「跳过此图」处理，`ai_handler.py:184-196`）。
- AI 主分析**不用**该装饰器，而是在 `get_ai_analysis` 内手写 4 次尝试循环（`ai_handler.py:372-479`），失败参数（JSON 输出、temperature）逐次降级，最后一次仍失败才 `raise`。

## 2. 分层传播规则：商品级 / 任务级 / 进程级

### 规则 2.1 确定性失败禁止被兜底吞掉

商品循环的宽泛 `except Exception` 只允许吸收「这一个商品」的局部失败（详情页超时、解析失败），**不得吞掉风控与登录失效**。识别到风控后应停止本轮剩余商品并**重抛**给任务级处理，而不是 `continue` 或就地 `break`。

- 为什么必须重抛：任务级 `except RiskControlError`（`src/scraper.py:1319-1323`）是 `last_error` 的唯一来源；只有它被赋值，收尾的 `_notify_task_failure`（`:1331`）才会被调用，从而触发 `FailureGuard` 熔断与通知。就地 `break` 会让本轮以「成功」收尾并执行 `record_success`（`:1313`），熔断器永远打不开。
- 事故例证（2026-09-25，生产实例）：`FAIL_SYS_USER_VALIDATE` 在商品详情接口被拦，商品级 `except Exception`（`src/scraper.py:1170-1174`）把它当普通错误打印后继续下一个商品；三个任务分别连撞 32/34/33 次（合计约 99 次），`logs/task-failure-guard.json` 中 4 个任务 `consecutive_failures` 全为 0，任务日志收尾却是「正常结束，本次运行共处理了 0 个新商品」。完整证据见 `.trellis/tasks/09-25-fix-risk-control-stop-loss/research/risk-control-incident-evidence.md`。
- **已落地实现（2026-09-26，任务 `09-25-fix-risk-control-stop-loss`）**：契约钉在四处，改动本链路必须同时满足——
  1. 判定与抛出集中在模块级纯逻辑 `_check_detail_risk_control`（`src/scraper.py:105-125`）：`ret` 含 `FAIL_SYS_USER_VALIDATE` → 打印 `CRITICAL BLOCK` → `random.randint(3, 60)` 长休眠 → `raise RiskControlError("FAIL_SYS_USER_VALIDATE")`（由原内联块纯搬移，为的是可在无 Playwright 下单测）；
  2. 商品详情循环的 `except RiskControlError`（`:1165-1169`）必须排在宽泛 `except Exception`（`:1170-1174`）**之前**，且 handler 为 `stop_scraping = True` + **裸 `raise`**；
  3. `record_success` 只在尝试正常返回路径（`:1313`）；风控异常路径必须不可达；
  4. 异常经任务级 `except RiskControlError`（`:1319-1323`）落到 `_notify_task_failure`（`:1331`）。
  回归钉子：`tests/unit/test_scraper_risk_control.py`（行为用例 + 结构契约）与 `tests/unit/test_scheduler_service.py`（同任务的 R1：`group_id=0` 不得重复调度）。

### 规则 2.2 允许降级的只有非关键旁路

与主流程解耦的辅助数据可以整体降级，但必须打日志、返回部分结果，且不把失败伪装成成功：

- 卖家资料采集：`scrape_user_profile` 整体 `except Exception` 后保留已采到的 `profile_data`（`src/scraper.py:441-443`），调用方 `_load_seller_info` 再兜一层并打印（`src/services/item_analysis_dispatcher.py:81-91`）。
- 图片下载：单图失败跳过，其余继续（`ai_handler.py:184-196`）。
- 复核证据日志：`_append_debug_log` 失败静默（`src/services/item_recheck_service.py:41-48`）。
- 过期复核：单商品异常计入 `errors` 并继续（`item_recheck_service.py:265-281`）；命中风控则 `aborted` 中止本轮复核（`:290-294`）——确定性失败同样要停下它所属的那一批。
- 通知发送：失败只打印，绝不让通知异常反过来打断业务（`scraper.py:161-164`、`dispatcher:167-173`）。

### 规则 2.3 进程退出码

现状是**进程退出码不反映任务失败**：`scrape_xianyu` 无论 `last_error` 是否为空都返回已处理商品数（`src/scraper.py:1316-1322`），`spider_v2.py` 用 `asyncio.gather(..., return_exceptions=True)` 打印「正常结束/因异常而终止」但不设置非零退出码（`spider_v2.py:203-217`）。这是事故中「外观正常」的组成部分，属已知技术债，风控修复任务明确将「修改 `spider_v2.py` 退出码」列为 Out of Scope；不要据此假设上游能感知失败，失败感知目前只靠 `FailureGuard` 与日志。

## 3. 任务级重试与熔断

### 3.1 尝试级重试（`src/scraper.py:1271-1332`）

- `attempt_limit = max(account_retry_limit, proxy_retry_limit, 1)`；循环内：
  - `LoginRequiredError` → 记 `last_error` 后 `break`（不轮换重试，`:1315-1318`）；
  - `RiskControlError` → 记 `last_error` 后 `break`（风控不是轮换能解决的，`:1319-1323`）；
  - 其余 `Exception` → 记 `last_error`，按配置轮换账号/IP 后重试（`:1324-1329`）。
- 只有整轮成功才 `FAILURE_GUARD.record_success`（`:1313`）；`last_error` 非空则调 `_notify_task_failure`（`:1331`）。

### 3.2 `FailureGuard` 熔断（`src/failure_guard.py`）

- 状态文件：`logs/task-failure-guard.json`（可用 `TASK_FAILURE_GUARD_PATH` 覆盖，`:164-168`）；写入用文件锁 + 临时文件 `fsync` + `os.replace` 原子替换（`:90-110`、`:136-143`）；损坏文件重命名为 `*.corrupt.<ts>` 后重置，避免无限解析失败（`:119-133`）。
- 参数：`TASK_FAILURE_THRESHOLD` 默认 3（`:169-171`）；`TASK_FAILURE_PAUSE_SECONDS` 默认 86400 秒、下限 60 秒（`:172-176`）；时区 `TASK_FAILURE_TZ` 默认 `Asia/Shanghai`。
- 语义：
  - `record_failure` 累加 `consecutive_failures`，达到阈值写入 `paused_until = now + pause_seconds` 并置 `opened_circuit`；通知每天最多一次（`:344-347`）。传 `min_failures_to_pause=1` 可让某类失败一次即熔断（`:304`）。
  - `should_skip_start` 在暂停期内返回 `skip=True`；若登录态文件 mtime 变新，则自动 `record_success` 恢复（`:247-261`）。
  - `record_success` 清零计数与暂停（`:204-218`）。
- 两个拦截点：任务启动前 `ProcessService.start_task` 先查 `should_skip_start`（`src/services/process_service.py:140-146`）；爬虫进程内 `scrape_xianyu` 开头再查一次（`scraper.py:1230-1255`）。
- **确定性失败立即暂停**的既有机制：`_notify_task_failure` 内 `pause_immediately` 标记元组（`scraper.py:145-154`），命中则 `min_failures_to_pause=1`（`:160`，一次计数即熔断，无第二次 `record_failure`）。**现含五条标记**：两条配置类（「未找到可用的代理地址」「未找到可用的登录状态文件」）+ 风控三条（`FAIL_SYS_USER_VALIDATE` / `baxia-dialog` / `J_MIDDLEWARE_FRAME_WIDGET`，2026-09-26 加入）。
  > **Warning（隐式契约）**：标记是**异常文案的子串**——命中判定比对 `str(exc)`（`:145-148`），文案由三处 `raise RiskControlError(...)` 提供（`:125`、`:756`、`:779`）。改 raise 文案而不改标记，会让熔断静默失效；改任一侧都要同步另一侧并跑 `tests/unit/test_scraper_risk_control.py`（其中三个标记各有一条参数化用例断言「一次命中即暂停」）。
- 登录态路径解析（2026-09-26 修复）：`ProcessService._resolve_cookie_path`（`process_service.py:54-69`）的判定顺序为 **任务 `account_state_file` → `FailureGuard.remembered_cookie_path(task_name)`（`failure_guard.py:359-371`，只读访问器，从历史失败记录取实际用过的路径）→ `STATE_FILE`（仅当文件真实存在）→ `None`**；解析不到一律返回 `None`，不得伪造路径。原 `except Exception: pass` 已改为打印一行日志（见 §7.7）。`should_skip_start` 的「更新登录态自动恢复」依赖这个值，返回 `None` 时该分支不可达。

## 4. API 层错误映射

- 分层：`HTTPException` 只在 `src/api/routes/*.py` 出现；`src/services/`、`src/infrastructure/`、`src/domain/` 不 import fastapi（已用 `grep -rl "fastapi\|HTTPException" src/services src/infrastructure src/domain` 核对为无）。服务层用 `ValueError` 表达参数非法，路由层翻译。
- 常用状态码与例证：
  - 400 参数/文件名非法：`results.py:80`（非法文件路径）、`:111`（筛选互斥）、`:133`（`except ValueError` 包装）、`accounts.py:48`、`tasks.py:55`；
  - 404 未找到：`results.py:85,226,248`、`tasks.py:88`、`task_groups.py:69`、`prompts.py:44`；
  - 409 冲突：`accounts.py:99`；
  - 422 配置校验：`settings.py:134-135`（`NotificationSettingsValidationError`）、`:156-157,166`；
  - 500 内部错误：`dashboard.py:22`、`login_state.py:37`、`results.py:135,196`（`except Exception` + 中文前后缀）、`settings.py:139`。
- 固定写法：
  - `except ValueError as exc: raise HTTPException(status_code=400, detail=str(exc))`；
  - 先抛的 `HTTPException` 不得被宽泛兜底改写，必须 `except HTTPException: raise`（`results.py:257-260`、`tasks.py:137-138`）；
  - `detail` 一律中文、面向用户可读（前端直接展示），内部堆栈只打印到日志（`tasks.py:140-144`）。
- 校验位置：路由层负责显式校验（互斥参数、空 body、路径穿越），如 `results.py:110-111`、`:240-241`、`result_file_service.validate_result_filename`（`src/services/result_file_service.py:14-16`）。
- 已知不一致：个别下载接口失败时 `return {"error": ...}` 且 HTTP 200（`results.py:64-68`），不是 `HTTPException`；新增接口不要沿用。

## 5. 错误分类的既有风格：文本识别纯函数

- AI 兼容网关的错误类型不稳定（OpenAI SDK 异常对象 vs 网关裸文本），所以分类用 `is_*_error(error: Exception) -> bool` 纯函数而不是异常类型匹配，集中在 `src/services/ai_request_compat.py`：
  - `is_json_output_unsupported_error`（`:78-91`）：先看结构化字段 `error.body.param`，再退化到 `"not supported" + marker`；
  - `is_responses_api_unsupported_error` / `is_chat_completions_api_unsupported_error`（`:94-101`）：markers + 特殊 404 兜底（`:178-195`）；
  - `is_temperature_unsupported_error`（`:160-168`）。
- 调用侧不抛出，而是基于判定调整后续请求参数：`ai_handler.py:445-468` 分别回退 API 模式、关闭 `response_format`、去掉 `temperature`，然后继续下一次尝试；4 次耗尽才 `raise`。
- 取舍：文本识别会漏判新网关的措辞变体；约定是「新增网关问题必须同时补 marker 与单测」，现有用例在 `tests/unit/test_ai_request_compat.py`、`tests/unit/test_ai_response_parser.py`。
- 不要把异常文本当业务控制流散落在业务代码里；需要分支时收进 `is_*_error` 纯函数并配单测。数据协议层的字符串判定除外，例如详情接口 `ret` 中含 `FAIL_SYS_USER_VALIDATE` 判定风控（现收敛为 `scraper.py:105-125` 的 `_check_detail_risk_control`；复核侧 `item_recheck_service.py:181`），这是接口协议而非异常消息。

## 6. 落库的失败语义

- AI/管线失败不向爬取管线抛：`ItemAnalysisDispatcher._run_ai_analysis` 的 `except Exception` 统一转成失败结果（`src/services/item_analysis_dispatcher.py:112-142`）：

```python
{ "analysis_source": "ai", "is_recommended": False,
  "reason": "AI分析异常: <exc>", "keyword_hit_count": 0, "error": "<exc>" }
```

- 结果照常 `save_result_record` 入库，`raw_json.ai_analysis.error` 是「失败」与「AI 判定不推荐」的唯一区分点（两者 `is_recommended` 都是 false）。下游导出/insights 按 `is_recommended`/`analysis_source` 统计（`src/services/result_export_service.py:48-50`、`result_storage_service.py:410-421`），目前不展示 `error`。
- 规则：**失败态不得伪装成业务判定**。不要用 `status`、黑名单、标签或「推荐」字段编码「分析失败」；失败只在 `ai_analysis` 内以 `is_recommended=false` + `error` 表示，`reason` 保留可读前缀。
- 其它失败落库约定：卖家资料采集失败落空 dict 而非 error 字段；`SKIP_AI_ANALYSIS=true` 走 `_build_skip_ai_result`（`dispatcher:104-110`），是显式的「跳过」而不是失败，两者不要混用。

## 7. 反模式清单（含真实事故）

1. **宽泛 `except Exception: continue` 吞确定性失败**：导致同一轮内风控连撞数十次、熔断器全程关闭（2026-09-25 事故）。识别出风控/登录失效后必须停止本轮并向上传播。
2. **就地 `break`/`continue` 代替重抛**：任务级拿不到 `last_error` → `_notify_task_failure` 不触发 → 失败被记成成功（`record_success`）。这是「止损失效」的直接原因。
3. **重试确定性失败**：配置缺失、风控、登录失效不应进入轮换重试；只有瞬时网络/解析错误才重试（`scraper.py:1301-1314` 的分支就是这条规则）。
4. **把异常文本当控制流**：散落的字符串比较没有单测防护；收敛到 `is_*_error` 纯函数（见 §5）。
5. **忽略 `retry_on_failure` 的 `None` 返回**：重试耗尽不抛异常只返回 `None`，调用方不判会静默丢失（图片下载、通知路径）。
6. **通知/辅助动作反噬主流程**：通知、证据日志、清理动作失败都必须吞掉并打日志。
7. **静默吞掉基础设施异常**：`_resolve_cookie_path` 曾用 `except Exception: pass` 让「自动恢复」静默失效（2026-09-26 已改为打印一行日志，`process_service.py:60-63`）。同类新代码一律至少留一行日志；静默 `pass` 只允许出现在证据写入与损坏文件隔离（判定口径见 `quality-guidelines.md` §8）。

## 8. 验证与排查命令

```bash
# 找出全部宽泛兜底，逐个判断是否吞了确定性失败
grep -rn "except Exception" src/ --include=*.py | grep -v __pycache__

# 风控/登录异常的定义与传播路径
grep -rn "RiskControlError\|LoginRequiredError" src/ --include=*.py

# 定向测试
.venv/bin/python -m pytest tests/unit/test_scraper_risk_control.py tests/unit/test_scheduler_service.py \
  tests/test_failure_guard.py tests/unit/test_process_service.py \
  tests/unit/test_ai_request_compat.py tests/unit/test_ai_handler_analysis.py -s

# 熔断状态（生产）
cat logs/task-failure-guard.json
```

- 事故原始证据与复现命令：`.trellis/tasks/09-25-fix-risk-control-stop-loss/research/risk-control-incident-evidence.md`；需求与验收标准：`.trellis/tasks/09-25-fix-risk-control-stop-loss/prd.md`。
- §2.1「已落地实现」四点是本链路的契约，改动时必须同步维护；引用行号以 grep 实测为准（本文件最后核对 2026-09-26）。任务完成后不要保留「待实施」措辞——规则与代码脱节一个提交都可能误导下一位读者。