# 日志规范（Logging Guidelines）

> 本文件描述本仓库**当前实际**的日志做法，不是理想方案。读者：后续 AI 子代理、新同事。
> 目录与命名职责见 `directory-structure.md`；异常类型与传播见 `error-handling.md`；`data/` 库文件见 `database-guidelines.md`。

---

## 速览

| 通道 | 落点 | 谁看 |
|---|---|---|
| 子进程 stdout/stderr | `logs/<任务名>_<任务ID>.log` | Web 日志页、排障 |
| API 主进程 stdout/stderr | systemd journal（`journalctl -u goofish`） | 服务排障 |
| 熔断状态 | `logs/task-failure-guard.json`（机器可读，原子写） | 程序 + 人工 |
| 复核证据 | `logs/recheck_debug.jsonl`（追加式） | 校准判定规则 |
| AI 请求 | `logs/ai/<YYYYmmdd_HHMMSS>.log`（摘要，保留 1 天） | AI 排障 |
| 业务结果 | SQLite `data/app.sqlite3`（**不是** `jsonl/`） | 产品功能 |

原则：**进度走 stdout 文本，状态/证据走 JSON 文件，业务结果走 SQLite**。三者不要混。

---

## 1. 输出通道：`print` / `safe_print` / `log_time`

项目**没有使用标准库 `logging`**（全仓 `src/` 无 `import logging`、无 logger/handler 配置）。日志就是 stdout 文本流，由 `ProcessService` 重定向到文件：

- `src/ai_handler.py:66-76` 定义 `safe_print(text)`：捕获 `UnicodeEncodeError`，降级为 `text.encode('ascii', errors='ignore')`，再失败打 `[输出包含无法显示的字符]`。原因见同文件 `:14-18`——Windows 控制台默认编码会让中文/emoji 直接抛异常；`ai_handler.py` 在 win32 下把 stdout/stderr 换成 UTF-8 writer。
- `safe_print` **只在 `src/ai_handler.py` 内部使用**（`grep -rn "safe_print" src/` 无其它模块命中）。其它模块用裸 `print` 或 `log_time`，因为爬虫子进程由 `src/services/process_service.py:107-110` 设置 `PYTHONIOENCODING=utf-8`、`PYTHONUTF8=1`，Windows 下也不会崩。
- `src/utils.py:68-74` 定义 `log_time(message, prefix="")`，输出形如 `[ 2026-09-25 17:42:10] 步骤 0 - 模拟真实用户访问首页...`（时间戳格式串 `' %Y-%m-%d %H:%M:%S'` 自带一个前导空格，是历史形态，照抄即可）。
- 真实日志样例：`logs/iPad_Air_M4_256G_1.log`。

结论：新增日志点选 `print` 还是 `log_time` 看是否需要时间戳——**轮次级里程碑用 `log_time`**（`src/scraper.py:948`、`src/services/item_recheck_service.py:261`），**行内细节用 `print`**（`src/scraper.py:363`、`src/ai_handler.py:307`）。在 `ai_handler.py` 内一律用 `safe_print`，不要换回裸 `print`。

---

## 2. 每任务运行日志 `logs/<任务名>_<任务ID>.log`

- 命名规则：`src/utils.py:77-90`。`sanitize_filename` 把 `[^a-zA-Z0-9_-]` 全部替换为 `_`、折叠连续下划线，文件名是 `f"{safe_name}_{task_id}.log"`，目录固定为相对路径 `logs/`。例：任务 `iPad Air M4 256G 上海包邮或自取`（id=1）→ `logs/iPad_Air_M4_256G_1.log`。
- 打开方式：`src/services/process_service.py:83-87`，`os.makedirs("logs", exist_ok=True)` + 以 `"a"`（追加）打开、`encoding="utf-8"`。**同一任务多次运行共用同一个文件**，不会每次清空。
- 子进程：`src/services/process_service.py:89-117`，`sys.executable -u spider_v2.py --task-name <名称>`；`-u` 保证无缓冲实时刷盘（前端增量读日志依赖它），`stdout=stderr=同一个句柄`。调试上限由 `SPIDER_DEBUG_LIMIT` 环境变量追加 `--debug-limit`（`:97-99`）。
- 终止标记：`src/services/process_service.py:233-241` 停下任务时向日志追加 `[ YYYY-MM-DD HH:MM:SS] !!! 任务已被终止 !!!`。看到这行意味着人/调度器主动停了任务，不是崩溃。
- 读取：`src/api/routes/logs.py:55-101`（按 `from_pos` 增量读）、`:104-161`（`/api/logs/tail` 按行分页，默认 50、上限 1000）。
- 清理：应用启动时 `src/app.py:80` 调 `cleanup_task_logs(keep_days=app_settings.task_log_retention_days)`；默认 7 天，可由 `TASK_LOG_RETENTION_DAYS` 覆盖（`src/infrastructure/config/settings.py:103`）。`src/services/task_log_cleanup_service.py:10-51` 只删 `logs/` **顶层** `*.log`，按 mtime 判断，**不递归**——`tests/unit/test_task_log_cleanup_service.py:23-44` 专门钉住 `logs/ai/` 子目录与 `task-failure-guard.json` 不会被误删。
- 删除任务时同步删日志：`src/api/routes/tasks.py:263-265` 走 `resolve_task_log_path`（`src/utils.py:93-102`，任务名对不上时回退按 `*_<task_id>.log` glob）。

---

## 3. 机器可读的状态与证据

**熔断状态 `logs/task-failure-guard.json`**（`src/failure_guard.py`）：

- 默认路径 `logs/task-failure-guard.json`，可用 `TASK_FAILURE_GUARD_PATH` 覆盖（`:164-168`）；`threshold`/`pause_seconds`/`tz_name` 分别读 `TASK_FAILURE_THRESHOLD`（默认 3）、`TASK_FAILURE_PAUSE_SECONDS`（默认 24h）、`TASK_FAILURE_TZ`（默认 Asia/Shanghai）（`:169-177`）。
- 原子写：`_atomic_write_json` 先写 `.tmp`、`flush`+`fsync`、再 `os.replace`（`:136-143`）；读改写用 `fcntl.flock` 锁（`:90-110`、`:189-202`），因为 API 主进程和爬虫子进程会并发读写同一文件。
- 文件损坏时**保留现场**：重命名为 `<path>.corrupt.<epoch>` 再返回空状态（`:119-133`）。实例中真实存在 `logs/task-failure-guard.json.corrupt.1790330755`。
- 这属于运行时状态，不要手改来"恢复任务"；正确做法是更新 cookies/登录态文件，`should_skip_start` 比较 `cookie_mtime` 后自动恢复（`:247-261`）。
- 状态变更时的人读输出：`[FailureGuard] 任务 'x' 失败计数 1/3，暂不通知。`（`src/scraper.py:136-139`）、`[FailureGuard] 跳过启动任务 ...`（`src/services/process_service.py:163-167`）。

**复核证据 `logs/recheck_debug.jsonl`**（`src/services/item_recheck_service.py`）：

- 追加式 JSONL，每行一条：`{time, item_id, title, last_seen, verdict, evidence}`，仅当判定规则需要校准/回归时读它（`:9-10`、`:286-288`）。
- 每个商品最多一条证据（`:34` `RECHECK_DEBUG_LIMIT_PER_ITEM = 1`，防重复刷盘）；异常也写一条 `verdict="EXCEPTION"`、`error` 截断 300 字符（`:270-279`）。
- 写证据失败**静默吞掉且必须注释说明**：`except Exception: pass`（`:47-48`，注释"证据日志失败不影响复核主流程"）。这是**业务/管线日志**里唯一被允许的静默失败场景（基础设施自愈类先例见 `quality-guidelines.md` §8）；新增类似代码请照抄注释格式，并补上本先例欠下的独立用例。

**业务结果不写 `jsonl/`**：`save_to_jsonl` 只是兼容旧名的包装，实际调 `save_result_record` 写 SQLite（`src/utils.py:122-128`）；`jsonl/` 目录只在迁移导入时被读取（`src/infrastructure/persistence/sqlite_bootstrap.py:21,32,129`）。不要为"结构化输出"新增 jsonl 文件——除非像 `recheck_debug.jsonl` 一样是**证据/审计**性质，且明确接受它没有时间清理、会随访问量增长（现有先例只做了"每商品每轮最多 1 条"的限流，见 `src/services/item_recheck_service.py:34`）。

---

## 4. AI 请求日志 `logs/ai/<YYYYmmdd_HHMMSS>.log`

- 每次 AI 调用前，`src/ai_handler.py:340-369` 在 `logs/ai/` 下建一个以时间命名的文件；**写入的是摘要**：`{timestamp, task_name, product_id, title, image_count}`（`:352-360`），一行 JSON。
- 注意 `:340` 的注释写着"保存最终传输内容"，与实现不一致——**以实现为准**：完整 messages（含 base64 图片）和原始响应都不落盘。保留 1 天，启动清理按文件名前 15 字符 `%Y%m%d_%H%M%S` 解析、解析失败跳过（`:212-225`）。
- 需要看完整请求/响应时开 `AI_DEBUG_MODE=true`（`src/config.py:48`，默认 false，`.env.example:61`）：会向控制台打印商品 JSON、完整 prompt、`_build_debug_request_summary` 返回值、原始响应，异常时打印 `repr(e)` 和 `traceback.format_exc()`（`src/ai_handler.py:317-323`、`:397-405`、`:415-418`、`:470-473`）。写入日志文件的动作失败只提示 `[日志] 保存AI分析日志时出错: {e}` 并继续（`:368-369`）。
- 已知风险：文件名精度只到秒、以 `"w"` 打开；同一秒并发两次分析会互相覆盖。新增同类日志时用更细粒度或以任务+商品维度命名。

---

## 5. 文案、级别与噪声控制

**没有 logging 级别体系**，约定靠前缀和中文措辞区分：

| 语义 | 实际形态 | 例证 |
|---|---|---|
| 正常里程碑 | `log_time(...)`，可选 `[反爬]`、`[复核]` 前缀 | `src/scraper.py:668`、`src/services/item_recheck_service.py:261` |
| 正常细节 | 3 空格缩进 + `[AI分析]`/`[图片]`/`[清理]`/`[日志]` | `src/ai_handler.py:307`、`:172`、`:205`、`:366` |
| 警告（可继续） | `警告：...` 或 `[AI分析] 警告：...` | `src/config.py:65`、`src/ai_handler.py:253` |
| 错误（可继续/降级） | `错误:` / `[错误]` / `...失败:` | `src/prompt_utils.py:156`、`src/scraper.py:442`、`:1140` |
| 调试 | 仅 `AI_DEBUG_MODE` 下打印 | `src/ai_handler.py:317-323` |

要求的习惯：

- **中文文案**。偶有英文异常信息原样透传（如 `Login required: redirected to ... (cookies/state likely expired)`，`src/scraper.py:690`），这是故意保留的定位关键词，不要翻译掉。
- 层级前缀用方括号，且已有词汇表：`[反爬]`、`[复核]`、`[AI分析]`、`[图片]`、`[清理]`、`[日志]`、`[延迟]`、`[API捕获]`、`[采集阶段]`、`[滚动超时]`、`[FailureGuard]`、`[页内进度 i/n]`。新增前缀前先 greps 是否已有近义词。
- 缩进是**可读层级**而非格式要求：`   `（3 空格）用于 AI 管线内层，`      `（6 空格）用于用户主页采集内层（`src/scraper.py:363`），详情循环错误行是 4 空格。
- **商品级噪声必须收敛**：逐商品的标题截断到 20/30 字符（`src/scraper.py:991`、`:996`），只打"已存在，跳过"或"发现新商品"；轮次结束打汇总（`spider_v2.py:211`、`:217` 输出"任务 'x' 正常结束，本次运行共处理了 N 个新商品"；复核汇总在 `src/services/item_recheck_service.py:313-317`）。当前日志体积大头是 `src/utils.py:61-65` 每次反爬等待都打的 `[延迟]` 行——不要在此基础上再加每商品逐条调试输出，调试用 `--debug-limit N` 限流。
- **落盘信息一律截断**：重试时的 HTTP 响应体截 300 字符（`src/utils.py:29-39`），失败原因截 500（`src/scraper.py:108-116`）再落熔断状态时截 1000（`src/failure_guard.py:331`），复核异常截 300（`src/services/item_recheck_service.py:277`）。

---

## 6. 服务侧日志从哪里看

- API 主进程（FastAPI/uvicorn，systemd `goofish.service`）：stdout 进 journald。用 `journalctl -u goofish -n 200 --no-pager` 或 `journalctl -u goofish -f`。生命周期打印见 `src/app.py:78-103`（"正在启动应用..." / "应用启动完成" / "正在关闭应用..." / "应用已关闭"）。
- 任务子进程：**不经过 journald**，全部在 `logs/<任务名>_<任务ID>.log`；Web 界面"日志页按任务展示运行过程"（`README.md:112-115`）走 `/api/logs`。
- Docker 部署：`docker compose logs -f app`（AGENTS.md「构建、运行与测试 → 一键启动与 Docker」）。
- 生产健康检查三件套（2026-09-26 在本实例上实测通过）：`systemctl is-active goofish`（active）、`curl 127.0.0.1:8000/api/groups`（200）、`curl 127.0.0.1:8080/goofish/`（200）。

---

## 7. 禁止打印/落盘的内容

1. **cookies / 登录态内容**：`state/` 下的文件（如 `state/acc1.json`）、Playwright `storage_state`。已有防线：`_build_extra_headers` 明确排除 `cookie`、`content-length`（`src/scraper.py:332-339`）。只允许打**文件路径**和"登录态可能过期"的定位语（`src/scraper.py:588`、`:690`）。
2. **`.env` 密钥与通知凭据**：配置缺失时只提示"未在 .env 文件中完整设置..."，绝不回显值（`src/config.py:63-65`）。通知设置 GET 接口把 `GOTIFY_TOKEN`/`BARK_URL`/`WX_BOT_URL`/`TELEGRAM_BOT_TOKEN`/`WEBHOOK_URL`/`WEBHOOK_HEADERS` 返回为空串，另发 `*_SET` 布尔（`src/services/notification_config_service.py:90-117`，测试 `tests/integration/test_api_settings.py:110-140`）。新增"配置回显"接口必须沿用这个模式。
3. **大 payload 原文**：不要打印/写入完整 messages、base64 图片、完整 API 响应、完整商品 JSON。AI 请求日志只存摘要（见第 4 节）；`AI_DEBUG_MODE` 下的全量打印是唯一例外，且默认关闭。
4. **运行态目录内容**：`data/`、`state/` 的字节内容不进日志；图片目录只打路径（`src/ai_handler.py:199-209`）。
5. 日志文件本身**不入库**：`.gitignore:7`（`logs/`）、`:4`（`.env`）、`:12`（`state/`）、`:17`（`data/`）。日志里出现过的敏感串也不要粘进 issue/提交说明。

---

## 8. 反模式（看到即改）

- 新增 `import logging` / 自建 handler：现有通道是 stdout→文件，加了以后 Web 日志页与 journalctl 都收不到。
- 在 `src/ai_handler.py` 里用裸 `print` 取代 `safe_print`：丢掉 Windows 编码兜底。
- 在商品循环内逐条 `print` 完整 JSON 或响应体：噪声 + 泄漏面。用计数、截断、轮次汇总。
- 把异常吞掉且不留任何痕迹。仅有的两处静默 `pass` 都是基础设施自愈/证据写入（`src/services/item_recheck_service.py:47-48`、`src/failure_guard.py:127-133`），且**都没有独立用例**；新增代码要么打印、要么写证据文件，不要照抄这一点。
- 往 `logs/` 顶层塞新后缀文件却不加保留策略：`cleanup_task_logs` 只清顶层 `*.log`（7 天），`.json`/`.jsonl` 永远不会被清，只会无限长大。
- 用 `logs/ai/<时间>.log` 存完整请求/图片 base64。
- 手工编辑 `logs/task-failure-guard.json` 来解封任务（应更新登录态文件自动恢复；手工改需停服且会被下一次原子写覆盖）。
- 为调试把 `.env` 值或 cookie 内容打进日志。

---

## 9. 交叉引用

- 目录职责与新增文件放哪：`directory-structure.md`
- 异常类型、抛出与 API 错误响应：`error-handling.md`
- `data/app.sqlite3`、`jsonl/` 历史导入与迁移：`database-guidelines.md`
- 测试不得触碰 `logs/`/`data/`/`state/`：`quality-guidelines.md`