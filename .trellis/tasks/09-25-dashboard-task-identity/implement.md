# 执行计划

> D1 = 迁移、D2 = 拒绝重复 keyword（2026-09-25 用户选定）。步骤 5 / 6 即按此编写，无待决分支。

## 执行方式（本会话适配）

本会话的可派发子代理类型只有 `general-purpose` / `Explore`（没有注册 `trellis-implement` / `trellis-check` 类型）。Phase 2 按工作流意图做等价适配：

- 实现：派发 `general-purpose`，提示词**首行**必须是 `Active task: .trellis/tasks/09-25-dashboard-task-identity`，并附 `.zcode/agents/trellis-implement.md` 的角色约束（只实现、不再派发 implement/check），要求它先读 `implement.jsonl` 里的 spec/research、再读 `prd.md` → `design.md` → `implement.md`。
- 复核：另派一个 `general-purpose`，提示词首行同样为 `Active task: ...`，附 `.zcode/agents/trellis-check.md` 的角色约束（只复核与修复、不再派发）。
- 无法使用 `trellis-research`：研究已在本任务 `research/` 落盘，属已完成步骤。

## 基线（实现前必须记录）

```
.venv/bin/python -m pytest tests/unit tests/integration -q
→ 128 passed, 2 failed（既有失败，与本任务无关，不得由本任务引入或修复）
  - tests/unit/test_task_group.py::test_group_update_partial_apply — NameError: name 'TaskGroupUpdate' is not defined
  - tests/unit/test_utils.py::test_save_to_jsonl — 记录里已带 _status/_note 等标注字段，用例期望过期
```

`tests/live` 需要真实凭据与外部服务（`pytestmark = pytest.mark.live`），离线验证不包含它。

## 步骤清单

### 1. 先写会失败的回归用例（红）

- 把 `research/repro_dashboard_identity.py` 的四个场景转成 pytest：同名不合并、无主文件不进任务、改 keyword 后删除无残留、外部写入只进 orphan。
- 补 `tests/integration/test_api_dashboard.py`：断言 `len(task_summaries) == len(tasks)`、`task_id` 非空、`summary.total_tasks` 与 `/api/tasks` 一致。
- 隔离要求：显式设置 `APP_DATABASE_FILE`（结果存储默认走 `data/app.sqlite3`；现有 dashboard 用例把 repository 指向 tmp 但结果存储仍走默认路径，是两个库，照抄会踩坑）。
- 验证：`.venv/bin/python -m pytest tests/integration/test_api_dashboard.py -q` 出现**预期失败**（证明用例有效）。
- 回滚点：仅测试文件，无风险。

### 2. 归属解析纯函数

- 新增 `resolve_stream_owners(tasks, filenames, metrics_by_file)`（位置见 `design.md` §2），实现 pass 1 确定性映射 + pass 2 历史回退 + 冲突记录 + orphan 判定。
- 新增 `tests/unit/test_result_stream_ownership.py`：确定性命中、keyword 仅归一化差异命中、名称回退命中、多候选取最小 id、无候选 → orphan、同名任务不互相覆盖。
- 验证：`.venv/bin/python -m pytest tests/unit/test_result_stream_ownership.py -q` 全绿。

### 3. 聚合重构

- `src/services/dashboard_service.py:37-53` 按 `design.md` §4 重写：直接从 tasks 生成摘要列表；`task_summaries` 不再以字符串为 key；`_build_fallback_summary`（`src/services/dashboard_payloads.py:93-111`）的结果只进 orphan，不再进摘要列表。
- `summarize_result_file` 需要能返回「未认领」信号（或由上层判定），保持活动生成逻辑不变。
- 验证：`.venv/bin/python -m pytest tests/integration/test_api_dashboard.py -q` 转绿。
- 回滚点：单一文件；旧实现留在 git 历史。

### 4. 计数权威化

- `_build_summary_metrics`（`src/services/dashboard_service.py:24-34`）增加 `total_tasks`、`orphan_files`；`result_files` / `scanned_items` 收紧为仅统计已归属文件。
- 前端 `web-ui/src/composables/useDashboard.ts:76` 改读 `summary.total_tasks`。
- 验证：`cd web-ui && npm run build`；端到端核对（见下）。

### 5. 生命周期：改 keyword 时迁移数据（D1 = 迁移）

- 新增 `migrate_result_stream(old_keyword, new_keyword)`（`src/services/result_storage_service.py`，单事务两表 UPDATE + 目标占用校验）；在 `src/api/routes/tasks.py` 的 `update_task` 接线：校验 → **备份数据库** → 迁移 → 更新任务字段；失败则不落新值。
- 迁移前备份命令（sqlite backup API，勿用裸 cp，需覆盖 WAL）：
  ```
  .venv/bin/python -c "import sqlite3; s=sqlite3.connect('data/app.sqlite3'); d=sqlite3.connect('data/backups/app.sqlite3.<时间戳>-before-migrate'); s.backup(d)"
  ```
- 删除路径无需改动：当前 keyword 就是唯一数据流，`src/api/routes/tasks.py:248-258` 的清理天然正确。
- 验证：新增用例覆盖 AC4 / AC5；`.venv/bin/python -m pytest tests/integration/test_api_tasks.py tests/integration/test_api_dashboard.py -q`。

### 6. 拒绝重复 keyword（D2）

- 校验点三处：`create_task`、`/generate` 生成路径、`update_task` 改 keyword 时；HTTP 400 + 可读文案（含占用者任务名）。
- 存量体检（只读）：输出重复 keyword 清单与无主文件清单，不自动改数据。
- 验证：新增用例覆盖 AC7；`.venv/bin/python -m pytest tests/integration/test_api_tasks.py -q`。

### 7. 前端（评审可裁）

- `GET /api/results/files`（`src/api/routes/results.py:56-59`）增加 `orphan_files` 字符串数组（`files` 形状不变）。
- 结果页对无主文件加标记与删除入口；删除时若该 keyword 已无任务使用，同步清理价格快照。
- 验证：`cd web-ui && npm run build`。

### 8. 全量验证（实现收尾，必须全做）

```bash
.venv/bin/python -m pytest tests/unit tests/integration -q     # 期望：基线 2 failed + 新增全绿
cd web-ui && npm run build                                     # vue-tsc + vite
```

端到端（服务在 127.0.0.1:8000 运行中；不在则先 `python -m src.app`）：

```bash
curl -s http://127.0.0.1:8000/api/tasks | .venv/bin/python -c "import sys,json;print(len(json.load(sys.stdin)))"
curl -s http://127.0.0.1:8000/api/dashboard/summary | .venv/bin/python -c "import sys,json;d=json.load(sys.stdin);print(d['summary']['total_tasks'], len(d['task_summaries']), len(d['orphan_files']))"
```

两者必须相等，且 `orphan_files` 为空（线上残留已清理）。

### 9. 收尾（Phase 2.2 → Phase 3）

- 派发 check 子代理做全量复核（对照 spec + prd/design/implement）。
- Phase 3.3：把「结果文件身份 = keyword 派生的确定性文件名」「任务身份不得用展示字符串做 key」写进 `.trellis/spec/backend/`（database-guidelines 或新增小节），并把「测试必须显式设置 APP_DATABASE_FILE」写进 quality-guidelines。
- Phase 3.4：分批提交（代码 → 文档/spec），按 `git log` 现有的 `fix(...)` / `feat(...)` 风格。

## 风险与回滚点

| 风险 | 位置 | 处置 |
|---|---|---|
| 聚合重写引入新的计数口径错误 | `src/services/dashboard_service.py` | 步骤 1 的用例先红后绿；端到端核对 |
| 历史文件回退匹配把同一文件认给两个任务 | `src/services/dashboard_payloads.py` | pass 2 只处理未认领文件 + 冲突取最小 id + 单测钉住 |
| keyword 迁移改写历史数据（不可逆） | `src/services/result_storage_service.py` | 迁移前备份；单事务；目标占用先校验 |
| 前端与后端口径再次漂移 | `useDashboard.ts` / `summary.total_tasks` | 计数只由后端给出，前端不得再推导 |
| 测试误写真实库 | 所有新增测试 | 显式 `APP_DATABASE_FILE` 指向 tmp |

## 完成判据

- AC1–AC9 全部有对应用例或端到端证据；`pytest` 无新增失败；前端构建通过。
- `prd.md` / `design.md` / 本文件的决策已收敛（D1 迁移 / D2 拒绝重复 keyword），无待决分支表述。
- 无主文件在概览不再冒充任务，且用户能在结果页看到并清理。