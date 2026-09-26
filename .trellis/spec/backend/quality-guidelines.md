# 质量与测试规范（Quality Guidelines）

> 描述本仓库**当前实际**的测试、构建、提交与生产纪律。读者：后续 AI 子代理、新同事。
> 本仓库同时是 u12 生产实例，违反本文纪律可能直接影响真实监控任务。

---

## 1. 测试框架与布局

- 框架 pytest，**默认同步测试，不使用 `pytest-asyncio`**；异步逻辑在测试里用 `asyncio.run(...)` 驱动（`tests/unit/test_utils.py:42-45`、`tests/unit/test_process_service.py:74`）。
- 配置 `pyproject.toml:1-10`：`addopts = "-v --tb=short"`、`testpaths = ["tests"]`、文件 `test_*.py`、类 `Test*`、函数 `test_*`；markers 只有 `live` 与 `live_slow`。覆盖率 `source = ["src"]`（`pyproject.toml:12-13`）。
- 布局：`tests/unit/`（纯单元，主力）、`tests/integration/`（FastAPI TestClient + 依赖注入）、`tests/live/`（真实流量冒烟，默认跳过）；顶层散装文件 `tests/test_failure_guard.py`、`tests/test_frontend_build_paths.py` 是当前实际存在的位置。
- 共享设施：`tests/conftest.py` 提供 `api_context`/`api_client` fixture，以及 `FakeProcessService`、`FakeSchedulerService`（`:60-106`）——集成测试不启动真进程、不连真调度器。
- 新增测试放对应目录、函数名 `test_*`；目录与文件命名细节见 `directory-structure.md`。

---

## 2. 运行方式与 u12 基线

- **一律用仓库根 `.venv/bin/python`**（系统 `python3` 没有项目依赖）：

  ```bash
  cd /mnt/code/ai-goofish-monitor
  .venv/bin/python -m pytest tests/ -s          # 全量
  .venv/bin/python -m pytest tests/unit/test_utils.py::test_safe_get_nested_and_default   # 定向（实测 1 passed）
  .venv/bin/python -m pytest --cov=src          # 覆盖率
  ```

  注意：AGENTS.md:47 的定向示例 `tests/test_utils.py::test_safe_get` 两个字段都已过时（文件在 `tests/unit/`，函数名是 `test_safe_get_nested_and_default`），照抄会报 `not found`；以本文件为准。

- **u12 基线（2026-09-26 实测，命令 `.venv/bin/python -m pytest tests/ -s -q`）：**
  `collected 136 items` → **130 passed / 3 failed / 3 skipped，约 4s**。
  3 个失败是存量问题，**不是新增回归**，但新改动不得在它们之外新增失败：
  1. `tests/test_frontend_build_paths.py::test_frontend_build_output_path_is_consistent_across_configs` —— 实测挂因：`.dockerignore` 当前包含 `web-ui/dist`，而断言要求不含（`:31`）；不是平台差异，是配置漂移。
  2. `tests/unit/test_task_group.py::test_group_update_partial_apply` —— `NameError: name 'TaskGroupUpdate' is not defined`（测试第 76 行）。
  3. `tests/unit/test_utils.py::test_save_to_jsonl` —— 返回记录被附加 `_status`/`_note`/`_user_tags` 等展示字段，与写入的原始 record 不相等。
  Windows 侧基线为 127 passed / 6 failed（AGENTS.md:25）。
- 3 个 skipped 全部是 `tests/live/test_live_smoke.py` 的 live 冒烟；默认跳过，必须显式 `RUN_LIVE_TESTS=1` 才收集执行（`tests/live/conftest.py:25-32`），且需真实凭据与外部服务。上游 CI 口径为 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest`（用于排除本机插件干扰）。
- 提交前至少跑与改动相关的定向测试；涉及公共路径（`src/utils.py`、`src/services/`）改动跑全量并对照上面的数字。

---

## 3. 测试隔离铁律（不写运行态）

**测试禁止写 `data/app.sqlite3`、`state/`、`.env`、`logs/`、`jsonl/`、`images/`、`dist/`。** 这些是生产实例的运行态，AGENTS.md:81 明确"禁止提交/覆盖/重置"。可用手段（都有真实例证）：

- `tmp_path` + `monkeypatch.chdir(tmp_path)`：结果写入类测试的标准姿势（`tests/unit/test_utils.py:28-30`；`tests/integration/test_api_dashboard.py:19-22`；`tests/integration/test_api_results.py:16-18`）。
- 数据库路径注入：`tests/conftest.py:108-158` 的 `api_context` 把 `db_path` 指向 `tmp_path / "app.sqlite3"`，并用 `app.dependency_overrides` 覆盖服务（`:154-158`），绝不碰 `data/`。需要走环境变量时用 `APP_DATABASE_FILE`——读取点在 `src/infrastructure/persistence/sqlite_connection.py:146-147`；live 脚手架就是这么做的：`tests/live/_support.py:161-164` 把 `APP_DATABASE_FILE` 指向工作区 `data/live.sqlite3`，并把 `ACCOUNT_STATE_DIR` 指向工作区 `state/`。
- `.env` 隔离：`monkeypatch.setattr(env_manager, "env_file", tmp_path / ".env")`，配合先清环境变量（`tests/integration/test_api_settings.py:60-79`、`:110-131`）。
- 子进程/文件副作用隔离：`tests/unit/test_process_service.py:57-60` 把 `build_task_log_path` monkeypatch 到 `tmp_path`，避免真建 `logs/`。
- `tests/test_frontend_build_paths.py` 是**有意例外**：它只读仓库文件做配置一致性断言（`:10-14`），不产生写入，不算破例。
- 新测试如果需要"看起来真实"的路径，用 `tmp_path` 拼相对结构，不要断言仓库根的真实文件存在。
- **`importlib.reload` 会让类身份失效**：`tests/unit/test_scraper_browser_channel.py` 会 reload `src.scraper`；此后其它测试文件在**模块顶层** `from src.scraper import RiskControlError` 拿到的旧类，与 reload 后模块内抛出的新类不再是同一个对象，`pytest.raises`/`except` 会失配（单跑通过、全量跑失败）。新测试一律运行时取类：`importlib.import_module("src.scraper").RiskControlError`（例证：`tests/unit/test_scraper_risk_control.py` 的文件 docstring 与取类写法）。

---

## 4. 前端构建纪律

- 任何 `web-ui/` 改动后**必须** `cd web-ui && pnpm build`；否则 SPA 服务读到的 `dist/` 还是旧产物（AGENTS.md:24）。
- 包管理器用 **pnpm**，不要用 npm：`web-ui/package-lock.json` 是上游 npm 遗留（AGENTS.md:23）。
- 产物输出到仓库根 `dist/`（vite `outDir`），且 `dist/` 不入库（`.gitignore:11`）。
- `tests/test_frontend_build_paths.py:14-33` 是路径一致性钉子：同时断言 `web-ui/vite.config.ts` 的 `path.resolve(__dirname, '../dist')`、两个 Dockerfile 的 `COPY ... /dist`、`.dockerignore` 与 `start.sh`。改 Docker/前端构建路径时先跑它；它当前是存量失败（见第 2 节），修它属于独立任务，不要顺手改断言来"变绿"。

---

## 5. 提交规范（中文 Conventional Commits）

- 类型：`feat(...)` / `fix(...)` / `refactor(...)` / `chore(...)` / `docs(...)`，描述用中文（AGENTS.md:73）。近期真实样例：`fix(server): 监听地址支持 SERVER_HOST 环境变量覆盖`（4c0f1d0）、`chore(prompts): 行尾符 CRLF 规范化（内容不变）`（2ae6546）、`feat(web-ui): 结果卡片就地标注与屏蔽理由选择`（e35f223）。
- 拆分粒度：一个提交一个主题；后端与前端改动分提交（见 `git log --oneline` 中 `feat(backend): 商品标注存储与 API` 与紧随的 `feat(web-ui): ...` 是两次提交）。
- **两个承重提交按 commit message 认，不认 sha**：`fix(server): 监听地址支持 SERVER_HOST 环境变量覆盖` 与 prompts CRLF 规范化（`chore(prompts): 行尾符 CRLF 规范化（内容不变）`）。rebase/reset 后必须能按 message 找回，systemd 单元依赖它们（AGENTS.md:82）。`git log --oneline --all --grep="SERVER_HOST"` 与 `--grep="CRLF"` 可验证仍在。
- remote 口径：`origin` = fork `mybhitwh/ai-goofish-monitor`（推送目标）；GitHub 走全局代理，fetch/push 失败先查代理，**管道后显式查退出码，防吞错假绿**（AGENTS.md:83-84）。

---

## 6. 提交纪律（什么不入库）

- `.gitignore` 已覆盖：`.env`（`:4`）、`logs/`（`:7`）、`dist/`（`:11`）、`state/`（`:12`）、`data/`（`:17`）、`config.json`、`images/`、`jsonl/`、`price_history/`。
- 用 `git check-ignore -v logs/task-failure-guard.json data/app.sqlite3 state/acc1.json .env dist/index.html` 验证（五条全部命中）；新增运行态文件时先确认规则，别用 `git add -f` 绕过。
- **`prompts/` 是特例**：目录被忽略，但库内已有两个跟踪文件 `prompts/base_prompt.txt`、`prompts/macbook_criteria.txt`（`git ls-files prompts/` 实证）。新增提示词文件需要 `git add -f`（AGENTS.md:67）。
- 禁止提交的运行时状态：`data/`（`app.sqlite3` 业务库）、`state/`（闲鱼登录态）、`.env`（密钥）。**禁止为了"让 diff 干净"而 reset/checkout 这三个**。

---

## 7. 生产实例纪律（u12）

- 服务由 systemd `goofish.service` 托管；重启 `sudo systemctl restart goofish`（u12 已配 NOPASSWD sudo，但**本环境里涉及 sudo 的操作应由用户代跑或先征得同意**）。
- 改动后健康检查三件套（2026-09-26 实测通过）：
  ```bash
  systemctl is-active goofish              # active
  curl 127.0.0.1:8000/api/groups           # JSON / 200
  curl 127.0.0.1:8080/goofish/             # 200
  ```
- 任何会改动运行时状态的迁移/清理（删脚本、改表、清日志）**必须先备份**。先例：`data/backups/app.sqlite3.20260925-before-feedscan-cleanup` 是"改名前先留一份"的实际产物。备份文件名带上日期与目的。
- 被暂停/停用的真实监控任务是否恢复，由用户决定；AI 不得自行恢复或启停生产任务。
- 在仓库里跑任何会写盘的脚本（爬虫、清理、迁移）前，先确认 cwd 与 `APP_DATABASE_FILE` 指向的不是生产库；默认路径规则见 `database-guidelines.md`。

---

## 8. 代码审查清单（本仓库口径）

- **跨层一致性**：调用方向必须 API → services → domain → infrastructure，不跨层直连（AGENTS.md:8-10）；路由用 `src/api/dependencies.py` 注入，集成测试也照此 override。参考 `.trellis/spec/guides/cross-layer-thinking-guide.md`。
- **重复实现**：先搜 `src/utils.py`、`src/services/` 是否已有同类函数；沿用兼容包装是既有先例（`save_to_jsonl` → `save_result_record`，`src/utils.py:122-128`），但不要为新功能再包一层。参考 `.trellis/spec/guides/code-reuse-thinking-guide.md`。
- **错误吞没**：`except` 后要么打印、要么写证据文件、要么向上抛；静默 `pass` 必须带注释说明为什么不影响主流程。存量两处静默先例（`src/services/item_recheck_service.py:47-48` 证据写入、`src/failure_guard.py:127-133` 损坏文件隔离）**都缺独立用例**——新增代码不要沿用这个缺口。语义细节见 `error-handling.md`。
- **是否触碰运行态**：改动/测试是否读写 `data/`、`state/`、`.env`、`logs/`；测试必须用第 3 节手段隔离。
- **日志合规**：新日志是否含 cookies、密钥、大 payload；是否遵循前缀/截断/保留策略（见 `logging-guidelines.md`）。
- **前端改动**：是否已 `pnpm build`、`dist/` 与源码一致。
- **测试结果**：是否对照第 2 节基线；新增逻辑是否补了针对性用例（AGENTS.md:50）。

---

## 9. 反模式（看到即改）

- 用 npm 安装/构建前端；改完 `web-ui/` 不 rebuild 就提交。
- 测试里不 `chdir(tmp_path)` 直接跑会写库的逻辑，结果污染 `data/app.sqlite3`。
- 为了让测试通过去改/读真实 `.env`、`state/acc1.json`。
- 以"基线本来就有 3 个失败"为借口放过新失败，或直接改断言让存量失败变绿。
- 在 master 上直接提交未跑验证的改动；混提后端+前端+文档。
- rebase/reset 后不检查两个承重提交（按 message 找）。
- `git add -f` 提交 `logs/`、`data/` 下的调试产物。
- 未经用户同意启停生产任务、恢复暂停任务、手工删运行态目录。
- 迁移/清理前不备份 `data/app.sqlite3`。

---

## 10. 交叉引用

- 目录与命名：`directory-structure.md`
- 数据库文件、迁移与备份：`database-guidelines.md`
- 错误类型与传播：`error-handling.md`
- 日志、保留策略与禁止内容：`logging-guidelines.md`