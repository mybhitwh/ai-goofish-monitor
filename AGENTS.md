# Repository Guidelines

本文件是仓库对 AI 助手与协作者的约定入口。**动手前先读「环境约定（u12 主仓）」与「生产实例运维（u12）」**——本仓库同时也是线上生产实例，误操作会直接影响真实监控任务。

## 项目概览

基于 Playwright + AI 的闲鱼智能监控机器人：FastAPI 后端 + Vue 3 前端，支持多任务并发监控、多模态 AI 商品分析、多渠道通知推送。分层架构（避免跨层耦合）：

```
API 层 src/api/routes/ → 服务层 src/services/ → 领域层 src/domain/ → 基础设施层 src/infrastructure/
```

## 目录结构

- **后端**：入口 `src/app.py`；API 路由 `src/api/routes/`（依赖注入 `src/api/dependencies.py`）；服务层 `src/services/`；领域模型 `src/domain/`；基础设施 `src/infrastructure/`；爬虫与 AI 管线 `src/scraper.py`、`src/ai_handler.py`，爬虫 CLI `spider_v2.py`。
- **前端**：`web-ui/`（Vue 3 + Vite），视图 `web-ui/src/views/`，组件 `web-ui/src/components/`；构建产物输出到仓库根 `dist/`（vite `outDir` 已指向 `../dist`），SPA 服务直接依赖它。
- **测试**：`tests/`，含 `unit/`、`integration/`、`live/`（真实流量冒烟，需凭据），文件命名 `test_*.py` 或 `tests/*/test_*.py`。
- **运行数据与资源**：`static/` 是随仓库发布的静态资源（已跟踪）；结果数据存 `data/app.sqlite3`，运行日志、下载图片、登录态分别在 `logs/`、`images/`、`state/`（默认不入库）；`jsonl/` 与 `price_history/` 是历史数据格式，仅首次启动时由 `sqlite_bootstrap` 导入，不再作为结果出口；配置 `config.json` 与 `.env` 位于仓库根目录。

## 环境约定（u12 主仓，2026-09-25 起）

- **Python**：一律用仓库根 `.venv/bin/python`（系统 `python3` 与 `.venv` 同为 3.10，但项目依赖只装在 `.venv`）；依赖锁定文件为 `uv.lock`。
- **前端包管理器**：用 **pnpm**（已在 PATH 上）。`web-ui/package-lock.json` 是上游 npm 遗留，不要据此改用 npm。
- **改动即重建**：任何 `web-ui/` 改动后必须 `cd web-ui && pnpm build`，否则 `dist/` 与源码不一致，SPA 服务读到的还是旧产物。
- **测试基线**：`.venv/bin/python -m pytest tests/ -s` → 145 passed / 3 failed / 3 skipped（共 151 收集；2026-09-26 风控止损任务新增 15 个用例后由 130/136 升到本值）。3 个失败为存量、非新增回归：`test_frontend_build_paths`（`.dockerignore` 含 `web-ui/dist` 触发断言，属配置漂移）、`tests/unit/test_task_group.py::test_group_update_partial_apply`（NameError）、`test_save_to_jsonl`；Windows 侧基线为 127 passed / 6 failed。

## 构建、运行与测试

### 后端

- 开发运行：`.venv/bin/python -m src.app`，或 `uvicorn src.app:app --host 0.0.0.0 --port 8000 --reload`（默认监听 8000 端口）。
- 爬虫任务：`.venv/bin/python spider_v2.py --task-name "MacBook Air M1" --debug-limit 3`（`--config` 可指定自定义配置）。

### 前端

- 开发：`cd web-ui && pnpm install && pnpm dev`；构建：`cd web-ui && pnpm build`。
- 上游文档中的 `npm run dev` / `npm run build` 是等价 npm 命令，仅在没有 pnpm 时使用。

### 一键启动与 Docker

- 一键本地启动：`bash start.sh`（安装依赖 → 前端构建 → 启动后端）。注意该脚本是上游脚本，用的是系统 `python3` 与 `npm`；在主仓使用前先 `source .venv/bin/activate`，否则会绕过 u12 依赖环境。
- Docker：`docker compose up --build -d` 起生产形态（`docker-compose.yaml`）；需要挂载源码边改边看时用 `docker compose -f docker-compose.dev.yaml up`。日志 `docker compose logs -f app`，停止 `docker compose down`。

### 测试

- 框架 pytest，默认同步测试，无需 `pytest-asyncio`；配置见 `pyproject.toml`（`testpaths = ["tests"]`，markers `live` / `live_slow`）。
- 全量：`.venv/bin/python -m pytest tests/ -s`（基线见上）；定向：`.venv/bin/python -m pytest tests/unit/test_utils.py::test_safe_get_nested_and_default`。
- 覆盖率：`pytest --cov=src` 或 `coverage run -m pytest`。
- 真实流量冒烟默认不跑（`-m live`，需真实凭据与外部服务，对应 `tests/live/` 与 `run_live_smoke.sh`）；上游 CI 口径为 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest`，用于排除本机插件干扰。
- 覆盖重点：核心服务、爬虫管道的异常分支与重试逻辑。PR 前跑相关测试，新增逻辑补针对性用例。

## 编码风格与命名约定

- 保持分层：API → services → domain → infrastructure，避免跨层耦合，模块保持精简。
- Python 测试函数命名 `test_*`，文件与路径遵循上述测试目录规范。
- 使用描述性、任务导向的命名（如爬虫任务名、配置键），与业务含义对应。

## 架构与运行时

- 后端用 FastAPI 提供 API 与静态资源；爬虫与 AI 推理在独立任务进程中协作，前后端通过 HTTP / Web UI 交互。
- 任务运行把结果写入 `data/app.sqlite3`（`result_items` 等表，读路径装饰后供前端消费），并写 `logs/`（运行日志）、`images/`（下载图片）；`jsonl/` 只作历史导入，不是结果出口。
- 前端构建后静态文件由后端或 Docker 镜像直接提供；默认对外 8000 端口。

## 安全与配置提示

- 复制 `.env.example` 为 `.env`，必填 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL_NAME`；通知渠道（ntfy / Bark / 微信 / Telegram / Gotify / Webhook）按需配置。
- 不要提交真实凭据或 cookies（如 `state.json`）；`.env`、`state/`、`data/`、`logs/`、`images/`、`jsonl/`、`dist/` 均已在 `.gitignore` 中。注意两个特例：`prompts/` 虽被忽略但库内已有跟踪的提示词文件（`base_prompt.txt`、`macbook_criteria.txt`），新增提示词要先 `git add -f`；`config.json` 虽列在 `.gitignore` 里**却已被 git 跟踪**（历史 force-add），改动它会被提交，动手前先确认。
- Web 认证默认 `admin/admin123`，生产环境务必修改，推荐启用 HTTPS 并限制访问来源。
- Playwright 需本地浏览器；Docker 镜像已预装 Chromium。

## 提交与 PR 规范

- Commit 用中文类 Conventional Commits：`feat(...)`、`fix(...)`、`refactor(...)`、`chore(...)`、`docs(...)` 等。
- PR 需说明变更范围与影响模块；UI 变更在 `web-ui/` 提供截图；关联相关 Issue；提及配置或迁移步骤。

### 提交节奏（2026-09-26 定）

Trellis 让每个任务固定产生多条流程提交（创建、文档收口、归档、日志）。实测 9-25/9-26 两天 29 个提交里 14 个属流程与文档，其中可合并的部分按下面三条压掉：

- **任务创建与规划合成一条**：`task.py create` 之后先不提交，等 `prd.md`（复杂任务含 `design.md` / `implement.md`）落盘再一起提交，形如 `chore(trellis): 新建并规划 <slug>（<一句话范围>）`。
- **同一会话的 trellis 文档收口合成一条**：同一个任务的「同步 spec」「实施清单收口」等 `docs(trellis)` 编辑攒成一条 `docs(trellis): <任务> 收口（<具体内容>）`，不要每份文档一条。
- **同文件小改攒批**：同一轮工作内对 `AGENTS.md`、`.trellis/spec/**` 的多次小改（补一行、改一行）合并提交。

以下必须独立成提交，不参与合并：

- 两条承重提交（`fix(server): 监听地址支持 SERVER_HOST 环境变量覆盖`、`chore(prompts): 行尾符 CRLF 规范化（内容不变）`）——按 message 认、不认 sha，合并即失去可寻性。
- 后端与前端的工作改动分提交（拆分粒度不变）。
- 脚本自动产生的 `chore(task): archive <任务>` 与 `chore: record journal`——窄路径由 `task.py` / `add_session.py` 控制，不要手工合并或 `--amend`。

**多窗口并行（同一工作区开两个会话）**：Phase 3.4 的「哪些脏文件是我改的」这一步会双向失效——自己的提交一律用 `git commit -m <msg> -- <paths>`（绝不 `git add -A`），提交前先 `git status` 看有没有已被另一个窗口带走；发现自己的改动被人批量提交时，核对 diff 后接受，不要为了拆分去 rebase 改写历史。

**改写边界**：未推送的提交流可以在用户要求下整理（同类提交合并等），已推送的一律不改写；整理后必须同步 `.trellis/workspace/*/journal-*.md` 与 `index.md` 里的 hash 引用（改写会级联改写脚本产生的 archive / journal 提交），并用 `git diff <备份标签> master` 确认除预期以外零差异、两条承重提交仍能按 message 找回。

## 生产实例运维（u12）

- 本仓库同时是生产实例，由 systemd `goofish.service` 托管。
  - 重启：`sudo systemctl restart goofish`（u12 已配 NOPASSWD sudo）。
  - 健康检查三件套：`systemctl is-active goofish`；`curl 127.0.0.1:8000/api/groups` 返回 JSON；`curl 127.0.0.1:8080/goofish/` 返回 200。
- ⚠️ **运行时状态，禁止提交 / 覆盖 / 重置**：`data/`（app.sqlite3 业务库）、`state/`（闲鱼登录态）、`.env`（密钥）。
- ⚠️ **提交纪律**：`fix(server): 监听地址支持 SERVER_HOST 环境变量覆盖` 与 prompts CRLF 规范化两个提交被 systemd 单元依赖；rebase/reset 后按 commit message 认，不认 sha。
- **git 网络**：GitHub 走全局代理（`http.https://github.com/.proxy = 127.0.0.1:7897`，Clash）。fetch/push 失败先查代理与退出码；**管道后必须显式查 rc，防吞错假绿**。
- **remote 口径**：`origin` = fork `mybhitwh/ai-goofish-monitor`（推送目标）；上游 Usagi-org 仅 Windows 侧配置。

## 相关文档

- `README.md` / `README_EN.md` —— 用户文档与部署说明（上游口径，与本文件的 u12 约定冲突时以本文件为准）。
- `CLAUDE.md` —— 面向 Claude Code 的项目说明。
- `.trellis/` —— Trellis 工作流：`workflow.md`（阶段与技能路由）、`spec/`（分层编码规范，改代码前读）、`tasks/`、`workspace/`。
- 文末由 `trellis init/update` 自动维护的「Trellis Instructions」区块（以 TRELLIS 起止注释标记界定）**勿手工编辑**，其内容会被下次 `trellis update` 覆盖；本文件其余部分的手工整理会被保留。

<!-- TRELLIS:START -->
# Trellis Instructions

These instructions are for AI assistants working in this project.

This project is managed by Trellis. The working knowledge you need lives under `.trellis/`:

- `.trellis/workflow.md` — development phases, when to create tasks, skill routing
- `.trellis/spec/` — package- and layer-scoped coding guidelines (read before writing code in a given layer)
- `.trellis/workspace/` — per-developer journals and session traces
- `.trellis/tasks/` — active and archived tasks (PRDs, research, jsonl context)

If a Trellis command is available on your platform (e.g. `/trellis:finish-work`, `/trellis:continue`), prefer it over manual steps. Not every platform exposes every command.

If you're using Codex or another agent-capable tool, additional project-scoped helpers may live in:
- `.agents/skills/` — reusable Trellis skills
- `.codex/agents/` — optional custom subagents

Managed by Trellis. Edits outside this block are preserved; edits inside may be overwritten by a future `trellis update`.

<!-- TRELLIS:END -->
