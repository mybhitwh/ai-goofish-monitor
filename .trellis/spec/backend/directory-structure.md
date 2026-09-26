# 目录结构与分层规范

> 新代码放哪里、怎么命名、层与层之间允许怎么依赖。**写的是代码实际怎么做，不是理想设计。**
> 数据库细节见 `database-guidelines.md`，异常语义见 `error-handling.md`，日志口径见 `logging-guidelines.md`，测试/构建/提交见 `quality-guidelines.md`，本文件只管结构与命名。

---

## Overview

- 设计意图是四层单向依赖（`AGENTS.md` / `CLAUDE.md` 口径）：

  ```
  API 层 src/api/ → 服务层 src/services/ → 领域层 src/domain/ → 基础设施层 src/infrastructure/
  ```

- **实际现状**：分层靠约定，没有 import linter 强制。多数路由只依赖 services，但存在下文「实际依赖方向」如实列出的越层/反向引用，它们是存量，不是可以模仿的范式。
- 组装点（composition root）只有两处：`src/app.py`（创建单例、注册路由、启动/关闭）与 `src/api/dependencies.py`（提供 `Depends` 工厂）。业务代码不要在别处 new 仓储或全局服务。
- 爬虫管线有自己的一条竖直链路（`spider_v2.py` → `src/scraper.py` → `src/ai_handler.py` 等），不经过 FastAPI，但会复用 services 与 infrastructure。

---

## Directory Layout

```
ai-goofish-monitor/
├── src/
│   ├── app.py                     # FastAPI 组装根：lifespan、路由注册、/static 与 dist 挂载、SPA 兜底
│   ├── config.py                  # 旧版全局配置：os.getenv 直读 + AsyncOpenAI client 单例（历史模块，见下）
│   ├── api/
│   │   ├── dependencies.py        # FastAPI 依赖注入工厂 + 全局服务 setter
│   │   └── routes/                # 每个资源一个路由模块，APIRouter(prefix="/api/...")
│   ├── services/                  # 业务服务：类或模块级函数，按职责命名
│   ├── domain/
│   │   ├── models/                # Pydantic 实体与 DTO（Task/TaskGroup/TaskGenerationJob）
│   │   └── repositories/          # 仓储抽象基类（TaskRepository）
│   ├── infrastructure/
│   │   ├── config/                # settings.py（pydantic-settings）+ env_manager.py（.env 读写）
│   │   ├── persistence/           # SQLite 仓储实现、连接/schema、启动迁移、命名规则
│   │   └── external/              # AIClient、notification_clients/<channel>_client.py + factory
│   ├── core/cron_utils.py         # cron 解析/校验/触发器（注意：core/ 没有 __init__.py）
│   ├── scraper.py                 # Playwright 抓取主流程（管线核心）
│   ├── ai_handler.py              # AI 请求、图片下载/清理、通知触发
│   ├── ai_message_builder.py      # 多模态消息体拼装（纯函数）
│   ├── keyword_rule_engine.py     # 关键词规则匹配引擎（纯函数）
│   ├── parsers.py / utils.py / rotation.py / failure_guard.py / prompt_utils.py
│   └── __init__.py
├── spider_v2.py                   # 仓库根，爬虫 CLI 入口（--task-name/--debug-limit/--config）
├── desktop_launcher.py            # 仓库根，PyInstaller 桌面入口（起 uvicorn + 开浏览器）
├── web-ui/                        # Vue 3 源码；vite outDir 指向仓库根 ../dist
├── tests/{unit,integration,live}/ + tests/fixtures/
├── static/                        # 随仓库发布的静态资源（已跟踪）
├── prompts/ state/ data/ logs/ images/   # 运行时数据（见「运行时目录」）
└── jsonl/ price_history/          # 旧数据源，仅首次迁移导入用
```

---

## 实际依赖方向（已核对，按此为新代码定界）

| 方向 | 现状 | 证据 |
|------|------|------|
| routes → services | 主流做法，绝大多数路由只 `Depends(get_xxx_service)` | `src/api/routes/dashboard.py:6-8`、`src/api/routes/results.py:13-35` |
| routes → domain DTO | 请求体直接用领域 DTO | `src/api/routes/tasks.py:26`、`src/api/routes/task_groups.py:12` |
| routes → infrastructure | 存量直连：设置/账号路由读 `.env`，任务路由用文件名规则 | `src/api/routes/settings.py:11-17`、`src/api/routes/accounts.py:11`、`src/api/routes/tasks.py:30` |
| routes 直接文件 IO | 存量：登录态、账号、prompt 三个路由自己读写文件，没有 service | `src/api/routes/login_state.py:33-34`、`src/api/routes/accounts.py:100-101`、`src/api/routes/prompts.py:46-63` |
| services → infrastructure | 普遍：服务直接引用 sqlite 连接、设置、客户端工厂 | `src/services/result_storage_service.py:11-13`、`src/services/price_history_service.py:14-15`、`src/services/process_service.py:17` |
| services → 根目录管线模块 | 服务复用纯函数模块（关键词引擎、prompt 工具） | `src/services/item_analysis_dispatcher.py:11`、`src/services/result_blacklist_service.py:9` |
| domain → services（反向） | 存量违规：`Task` 校验 import 账号策略服务；另引 `core/cron_utils` | `src/domain/models/task.py:11-15` |
| infrastructure → domain | 合法向上：仓储实现领域接口、返回领域实体 | `src/infrastructure/persistence/sqlite_task_repository.py:10-11` |
| 管线 → services/infrastructure | `scraper.py` 顶部同时 import pipeline、services、infrastructure | `src/scraper.py:15-63` |
| CLI → infrastructure（绕过 services） | `spider_v2.py` 直接 new `SqliteTaskRepository` 取任务 | `spider_v2.py:11,45-47` |
| app.py 组装一切 | 全局服务单例 + `set_*` 注入 + lifespan 启停 | `src/app.py:40-48,69-71,74-103` |

**新增代码的判定规则**：只允许向下依赖（routes → services → domain；services/infrastructure 可引用 domain）。需要「向上」时，把接口下沉到 domain（照 `TaskRepository` 的做法），或把编排提到 service；不要新增上表里的存量式直连。

---

## Module Organization

### API 层（`src/api/`）

- 每个资源一个模块，模块内声明 `router = APIRouter(prefix="/api/<资源>", tags=["..."])`（`src/api/routes/tasks.py:33`、`src/api/routes/logs.py:14`、`src/api/routes/task_groups.py:19`）；WebSocket 例外，无前缀（`src/api/routes/websocket.py:9`）。
- 路由函数只做三件事：参数校验、调 service、把领域对象转成响应 dict。序列化辅助放 `src/services/*_payloads.py`（如 `serialize_task`，`src/services/task_payloads.py:16`）。
- 依赖通过 `Depends(get_xxx_service)` 获取；模块级 `set_xxx_service` 只由 `src/app.py` 调用（`src/api/dependencies.py:24-39,66-84`）。
- 应用级端点（`/health`、`/auth/status`、`/` SPA 兜底）直接写在 `src/app.py:137-195`，不建 routes 模块。

### 服务层（`src/services/`）

- 有状态/可注入的编排对象写成 `XxxService` 类，构造器收仓储或协作服务（`TaskService`，`src/services/task_service.py:10-16`）。
- 无状态逻辑写成模块级函数，文件名即职责：`dashboard_service.py` 的 `build_dashboard_snapshot()`（`src/services/dashboard_service.py:37`）、`task_log_cleanup_service.py` 的 `cleanup_task_logs()`（`src/services/task_log_cleanup_service.py`）。
- 序列化/拼装辅助单独放 `*_payloads.py`（`task_payloads.py`、`dashboard_payloads.py`），不混进 service 主流程。
- 进程与调度是常驻单例：`ProcessService` 管子进程与日志文件（`src/services/process_service.py`），`SchedulerService` 管 APScheduler 与任务组触发（`src/services/scheduler_service.py:23-47`）。
- 与 SQLite 打交道的「存储服务」是现状最大的一组：`result_storage_service.py`、`price_history_service.py`、`item_annotation_service.py`、`item_recheck_service.py` 直接持有 `sqlite_connection()`，不经过仓储。

### 领域层（`src/domain/`，实际很薄）

- 只有两块：`models/` 是 Pydantic 实体与 DTO（`Task`/`TaskCreate`/`TaskUpdate`，`src/domain/models/task.py:107-292`；`TaskGroup`，`src/domain/models/task_group.py:18`；`TaskGenerationJob`，`src/domain/models/task_generation.py:24`）。
- `src/domain/repositories/task_repository.py:12-33` 是一个 ABC 接口；没有别的仓储抽象、没有领域服务、没有值对象层。
- 领域模型承担输入规范化：`model_validator`/`field_validator` 做关键词、价格、cron、账号策略的清洗与校验（`src/domain/models/task.py:135-143,215-224`）。

### 基础设施层（`src/infrastructure/`）

- `config/settings.py`：pydantic-settings 声明环境变量（`AISettings`/`NotificationSettings`/`ScraperSettings`/`AppSettings`，`src/infrastructure/config/settings.py:41-113`），并在模块底部导出全局单例 `settings` / `ai_settings` 等（`:142-145`）；`reload_settings()` 支持运行时重载（`:127-138`）。
- `config/env_manager.py`：`.env` 读写与 `get_value` 回退（`EnvManager`，`src/infrastructure/config/env_manager.py:16-50`）。
- `persistence/`：`sqlite_connection.py`（连接 + `SCHEMA_STATEMENTS`）、`sqlite_bootstrap.py`（启动建表 + 旧 `config.json`/`jsonl/`/`price_history/` 迁移）、`sqlite_task_repository.py` / `sqlite_task_group_repository.py`、`storage_names.py`（`data/app.sqlite3` 与 `*_full_data.jsonl` 命名规则，`src/infrastructure/persistence/storage_names.py:7-16`）。
- `external/`：`ai_client.py`（OpenAI 兼容客户端）与 `notification_clients/`（每渠道一个 `<channel>_client.py`，工厂 `build_notification_clients()` 汇总，`src/infrastructure/external/notification_clients/factory.py:15`）。
- `json_task_repository.py` 是旧 JSON 仓储实现，当前无调用方（`grep` 仅命中自身），别拿它当参考。

### 管线与 CLI（仓库根 + `src/` 根模块）

- 入口：`spider_v2.py`（argparse，`--task-name` 单任务由调度器调用，`--debug-limit` 调试）并发跑 `scrape_xianyu()`（`spider_v2.py:192,205`）。
- `src/scraper.py` 是抓取编排：Playwright 启动、反检测、分页（`search_pagination`）、卖家画像缓存、`ItemAnalysisDispatcher` 提交分析（`src/scraper.py:621-631`）。
- `src/ai_handler.py` 负责图片下载/清理与 AI 调用；`src/ai_message_builder.py` 只做消息体拼装；`src/keyword_rule_engine.py` 只做规则判定，被 `item_analysis_dispatcher.py` 与 `result_blacklist_service.py` 复用。
- `src/utils.py` 是历史杂物间：`safe_get`、`save_to_jsonl`（现已转写 SQLite，`src/utils.py:122-128`）、日志路径工具等，新工具按归属放分层目录，不要再往这里加。

### 前端边界（`web-ui/` → 仓库根 `dist/`）

- 源码只在 `web-ui/src/`：页面 `views/XxxView.vue`，组件 `components/<域>/`（layout/results/settings/tasks/ui），组合式函数 `composables/useXxx.ts`，HTTP 封装 `lib/http.ts` + `api/<资源>.ts`。
- 构建产物固定输出仓库根 `dist/`（`web-ui/vite.config.ts:10-13`），`base: '/goofish/'` 子路径部署；后端在 `dist` 存在时才挂载 `/assets` 并回 `index.html`（`src/app.py:133-134,165-195`）。
- **改任何 `web-ui/src/**` 后必须 `cd web-ui && pnpm build`**，否则 SPA 读到的还是旧 `dist/`。`dist/` 不入库但必须存在，改动 `.gitignore`/Dockerfile/start.sh 中关于它的口径会被 `tests/test_frontend_build_paths.py` 盯住。

### 测试与夹具

- 三个子目录职责固定：`tests/unit/`（纯逻辑）、`tests/integration/`（TestClient + `src/api/routes` 组装，见 `tests/conftest.py:16-18`）、`tests/live/`（真实流量，默认 skip，需 `RUN_LIVE_TESTS=1`，`tests/live/conftest.py:25-32`）。
- 夹具放 `tests/fixtures/`（`config.sample.json`、`search_results.json` 等）；测试直接 import `src.domain`/`src.services`/`src.infrastructure`，不受分层约束。

### 运行时目录（哪些入库）

| 目录/文件 | 内容 | 入库 | 写入方 |
|-----------|------|------|--------|
| `data/` | `app.sqlite3` 业务主库（任务/结果/价格历史/标注） | 否 | `sqlite_connection` / 各存储服务 |
| `state/` | 多账号登录态 `<name>.json` | 否 | 账号路由、`rotation.load_state_files` |
| `prompts/` | 任务提示词；**例外**：`base_prompt.txt`、`macbook_criteria.txt` 两个文件已跟踪 | 部分 | prompt 路由、任务生成 runner |
| `logs/` | `logs/<task>_<id>.log`、`logs/task-failure-guard.json`、`logs/recheck_debug.jsonl` | 否 | `ProcessService`、`FailureGuard`、复核服务 |
| `images/` | `images/task_images_<task>/` 商品图 | 否 | `ai_handler.download_all_images` |
| `jsonl/`、`price_history/` | 旧文件数据源，仅首次启动迁移导入 | 否 | `sqlite_bootstrap`（只读） |
| `static/` | 图标/截图等发布资源 | 是 | 人工维护 |
| `dist/` | 前端构建产物 | 否（但必须构建） | `pnpm build` |
| `.env` | 密钥与运行时配置 | 否，禁止提交/覆盖 | `env_manager` |

> 需要落新运行时数据时优先复用上表目录与既有文件名规则（`storage_names.py`），不要新增顶层数据目录。

---

## 新增文件该放哪（决策表）

| 改动类型 | 目标位置 | 参照 |
|----------|----------|------|
| 新 HTTP 接口/资源 | `src/api/routes/<resource>.py`，`APIRouter(prefix="/api/...")`，再到 `src/app.py` `include_router` | `src/api/routes/dashboard.py`、`src/app.py:115-124` |
| 新依赖注入工厂 | `src/api/dependencies.py`；需要全局单例时加 `set_*` 并在 `app.py` 调用 | `get_task_service`，`src/api/dependencies.py:43-46` |
| 新业务编排/领域逻辑 | `src/services/<name>_service.py`（类）或 `<name>.py`（模块级函数） | `task_service.py`、`dashboard_service.py` |
| 响应序列化/字段拼装 | `src/services/<domain>_payloads.py` | `task_payloads.py`、`dashboard_payloads.py` |
| 请求/实体模型与校验 | `src/domain/models/<entity>.py` | `task.py`、`task_group.py` |
| 新数据访问抽象 | 接口进 `src/domain/repositories/`，实现进 `src/infrastructure/persistence/` | `task_repository.py` + `sqlite_task_repository.py` |
| 新外部服务客户端 | `src/infrastructure/external/`（多渠道类再建 `notification_clients/<x>_client.py` 并注册进工厂） | `ai_client.py`、`notification_clients/factory.py` |
| 新环境变量/配置项 | 在 `src/infrastructure/config/settings.py` 对应 Settings 类加 `_env_field`，同步 `.env.example` | `settings.py:41-113` |
| 爬虫抓取/反检测 | `src/scraper.py`；分页进 `src/services/search_pagination.py` | `scraper.py:451` |
| AI 请求/图片处理 | `src/ai_handler.py`；消息体进 `src/ai_message_builder.py`；兼容性补丁进 `src/services/ai_request_compat.py` | `ai_handler.py:298` |
| 结果解析纯函数 | `src/parsers.py`（页面 JSON 解析） | `parsers.py:8-147` |
| 任务生成流程 | `src/services/task_generation_service.py`（作业状态）+ `task_generation_runner.py`（执行器） | — |
| 前端页面/组件/状态 | `web-ui/src/views/`、`components/<域>/`、`composables/`、`api/` | `views/ResultsView.vue`、`composables/useResults.ts` |
| 单元/集成/真实流量测试 | `tests/unit/`、`tests/integration/`、`tests/live/`；夹具 `tests/fixtures/` | `tests/unit/test_task_group.py` |
| 爬虫临时产物/日志 | 复用 `logs/`、`images/`，文件名走 `utils.build_task_log_path` / `storage_names` | `src/utils.py:86-102` |

---

## Naming Conventions

- Python 模块一律 `snake_case`；类 `PascalCase`；模块内私有辅助 `_leading_underscore`（`src/services/price_history_service.py:40`）。
- 服务类后缀 `Service`：`TaskService`、`ProcessService`、`SchedulerService`、`NotificationService`；作业型服务 `TaskGenerationService`。
- 路由模块名 = 资源名，prefix 用 `/api/<资源>` 且多为复数或连字符（`/api/tasks`、`/api/groups`、`/api/login-state`、`/api/dashboard`），`tags` 用资源名。
- 仓储：领域接口 `TaskRepository`；实现前缀技术名 `SqliteTaskRepository`（`JsonTaskRepository` 为旧实现）。Pydantic 实体无后缀，DTO 用 `XxxCreate`/`XxxUpdate`/`XxxRequest`（`task.py:159,227,295`）。
- SQLite 表/列 `snake_case`（`result_items`、`link_unique_key`，`sqlite_connection.py:38-58`）；结果文件名由 `build_result_filename(keyword)` 统一生成，禁止手拼 `_full_data.jsonl`。
- 通知渠道实现 `<channel>_client.py` + `XxxClient` 类（`ntfy_client.py`、`wecom_bot_client.py`）。
- 前端：视图 `XxxView.vue`，组合式函数 `useXxx.ts`，类型 `types/<域>.d.ts`，接口封装 `api/<资源>.ts`。
- 测试：文件 `test_*.py`、函数 `test_*`（pyproject 已固定 `python_files`/`python_functions`）。

---

## Examples（新增代码照这些写）

- 最薄的路由：`src/api/routes/dashboard.py`（依赖注入 + 一行 service 调用 + 异常转 HTTPException）。
- 类服务的标准形：`src/services/task_service.py:10-45`（构造器注入 `TaskRepository`，方法只编排仓储）。
- 仓储实现：`src/infrastructure/persistence/sqlite_task_repository.py:10-18`（实现领域接口，`_row_to_task` 做行转换）。
- 无状态服务模块：`src/services/dashboard_service.py:37-64`（纯函数入口，辅助函数拆到 `dashboard_payloads.py`）。
- 管线抽出的并发组件：`src/services/item_analysis_dispatcher.py`（构造器注入 `seller_loader`/`image_downloader`/`ai_analyzer`/`notifier`/`saver`，`scraper.py:621-631` 组装）。
- 模块边界最干净的一组：`ai_message_builder.py`（纯拼装）、`keyword_rule_engine.py`（纯规则）、`ai_response_parser.py`（纯解析）——需要可复用时照此拆。

---

## 禁止 / 反模式

1. **不要新增向上依赖**：`src/domain/**` 不许再 import services（`src/domain/models/task.py:11-15` 是存量违规，别扩散）；需要共享规则时下沉到 domain 或提到服务层。
2. **不要在路由里直接读写文件**：`prompts.py`/`accounts.py`/`login_state.py` 的直连文件 IO 是存量；新增文件类操作抽 `src/services/`，路径安全校验照 `prompts._safe_prompt_path`（`src/api/routes/prompts.py:16-23`）。
3. **不要绕过 `dependencies.py` 在路由里 new 仓储/服务**；单例只由 `app.py` 创建并 `set_*` 注入。
4. **不要新开顶层数据目录或散落根模块**：运行时数据只落 `data/`/`state/`/`logs/`/`images/`；新模块进分层目录，`src/` 根只保留管线历史模块（`scraper.py`、`ai_handler.py`、`parsers.py`、`utils.py`、`rotation.py`、`failure_guard.py`、`prompt_utils.py`、`config.py`）。
5. **不要让服务再直接连 SQLite 连接**：`result_storage_service.py` 等直持 `sqlite_connection()` 是存量；新增持久化走 `infrastructure/persistence`，SQL 不写在 routes 里。
6. **不要手写结果文件名**：一律 `storage_names.build_result_filename()`，前端拿到的也应是该规则生成的 `<keyword>_full_data.jsonl`。
7. **不要手工改 `dist/`**：它是构建产物；改前端 = 改 `web-ui/src/**` + `pnpm build`。
8. **不要把 `.env`、`data/`、`state/` 当普通文件**：生产实例运行时状态，禁止提交、覆盖、重置。
9. **两个配置系统不要互相抄**：新配置项加在 `infrastructure/config/settings.py`（pydantic-settings）；`src/config.py` 是旧的 `os.getenv` 直读 + OpenAI client 单例，只有管线模块在用，新代码不要往里加。