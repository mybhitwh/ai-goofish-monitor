# 修复概览任务计数与结果文件归属

## Goal

监控概览的「监测任务」卡片必须等于任务管理的任务条目数，且此后不会再因为数据形态变化而漂移：任务与结果文件的归属由 task id 建立，无法归属的结果文件不再冒充任务，任务改名 / 改关键词 / 被删除后不留下幽灵条目。

用户价值：概览的汇总数字可信；「已启用 N 个」与「监测任务 M」不再自相矛盾；换关键词不再等于丢掉历史数据。

## Background（已核实事实）

现象与证据（含线上数据、实测输出）见 `research/root-cause-and-live-evidence.md` 与 `research/repro-scenarios.md`，代码地图见 `research/identity-and-lifecycle-map.md`。

- 概览「监测任务」的值是前端数组长度：`web-ui/src/composables/useDashboard.ts:76` `totalTasks: taskSummaries.value.length`，卡片在 `web-ui/src/views/DashboardView.vue:62-69`；同一排的「活动任务」用的是后端 `summary.enabled_tasks`（直接数任务），所以幽灵条目会造成「监测任务 5 / 活动任务 4」这种页面内自相矛盾。
- 后端聚合两次用**展示字符串**做身份：`src/services/dashboard_service.py:39-41` 用 `task_name` 建字典，`:48` 又用 `summary["task_name"]` 写入。
- 结果文件匹配任务用的是两轮字符串猜测（先 keyword、再记录里的「任务名称」），猜不中就造一条 `task_id=None` 的摘要并计入统计：`src/services/dashboard_payloads.py:114-126`、`:238-240`、`:93-111`。
- 而结果文件的真实身份是确定性的：`src/infrastructure/persistence/storage_names.py:11-12` `build_result_filename(keyword) = keyword.replace(' ', '_') + "_full_data.jsonl"`，删除路径也按它推导（`src/api/routes/tasks.py:248-258`），只有聚合没用它。
- 生命周期缺口两处：改 keyword 时不做任何结果数据处置（`src/api/routes/tasks.py` 的 `update_task`）；删除任务只清理**当前** keyword 的文件，改过 keyword 就清错目标（`src/api/routes/tasks.py:248-258`）。
- 数据层没有任何唯一约束，同名任务、同 keyword 任务都能创建（`tasks` 表只有非唯一索引 `idx_tasks_name`）。
- 线上现状（清理前）：4 个任务、5 个结果文件，多出的 `feed_scan_full_data.jsonl` 是已移除工具遗留的无主数据。该 5 行已按用户要求清理完毕（备份在 `data/backups/app.sqlite3.20260925-before-feedscan-cleanup`），本任务修的是**产生它的代码路径**，不是这一次的数据。
- 既有测试缺口：`tests/integration/test_api_dashboard.py` 只按 `task_name` 查找条目，从不断言条数与 `task_id`，因此同名合并与幽灵条目都能通过。

## Requirements

- **R1 身份以 task id 为准**：概览摘要与任务一一对应；两个同名任务各自成条、各自累计指标，不互相覆盖。
- **R2 归属走确定性映射**：任务 → 结果文件名用 `build_result_filename(task.keyword)` 解析；宽松匹配（keyword 归一化、任务名称回退）只能作为历史数据的第二轮回退，且必须保证「一个结果文件只被一个任务认领」。
- **R3 无主结果文件单独出口**：无法归属的文件进入 `orphan_files`，携带 `filename`、`total_items`、`latest_crawl_time`、记录内的任务名与关键字；它们不进 `task_summaries`，也不计入任务的扫描 / 推荐累计。
- **R4 计数权威化**：后端 `summary` 直接给出任务总数，前端不再用数组长度推导，避免口径再次漂移。
- **R5 生命周期补齐**：改 keyword 时把该任务既有数据迁移到新 keyword 的结果文件名下（见 Decisions D1），保持「一个任务一份数据流」；删除任务时清理该任务的全部数据流。
- **R6 禁止重复 keyword**：创建、AI/关键词生成、修改三条路径都拒绝与其它任务重复的 keyword（HTTP 400 + 可读文案，含占用者任务名）；对存量数据提供只读的重复 keyword 体检输出，不自动改数据。
- **R7 回归测试**：把 `research/repro_dashboard_identity.py` 覆盖的场景（同名合并、名字+keyword 同改、改 keyword 后删除、非任务数据）转成 pytest 用例，并钉住不变量：正常数据下 `task_id` 非空条目数 == 任务数；测试不得触碰仓库的 `data/app.sqlite3`（显式设置 `APP_DATABASE_FILE`）。
- **R8 不引入新失败**：`pytest tests/unit tests/integration -q` 的结果与基线一致（基线：128 passed，2 个既有失败 `tests/unit/test_task_group.py::test_group_update_partial_apply`、`tests/unit/test_utils.py::test_save_to_jsonl`，与本任务无关）；`cd web-ui && npm run build` 通过。

## Acceptance Criteria

| # | 可观察结果 | 对应需求 |
|---|---|---|
| AC1 | 构造两个同名任务（keyword 不同、各有独立结果文件）：概览返回 2 条摘要，`total_tasks` 为 2，两条目各自的 `total_items` 分别等于各自文件条数（1 与 2），无覆盖丢失 | R1 |
| AC2 | 构造一个无主结果文件：`total_tasks` 不含它；`orphan_files` 含该 `filename` 及条数 / 最新时间 / 记录内任务名与关键字；`result_files`、`scanned_items` 只统计已认领文件 | R3 |
| AC3 | 同一数据集下 `GET /api/dashboard/summary` 的 `summary.total_tasks` == `GET /api/tasks` 的返回条数 | R4 |
| AC4 | 单独修改任务 keyword（不改名）后：该任务的既有数据已迁移到新 keyword 的结果文件名下（`result_items.result_filename` 全部改指新文件，对应价格快照同步），概览中该任务指标连续、无 `task_id` 为空的条目，计数不变 | R5 |
| AC5 | 先改 keyword、再删除任务：该任务关联的所有 `result_filename` 在 `result_items` 中归零（对应价格快照一并处理），概览不残留任何无主条目 | R5 |
| AC6 | 模拟外部工具写入的结果文件（无对应任务）：永不进入 `task_summaries`，只出现在 `orphan_files` | R3 |
| AC7 | 与其它任务重复的 keyword 在创建 / 生成 / 更新三条路径均被拒绝（HTTP 400，错误信息含占用者任务名）；任何数据集下 `total_tasks` 与任务数一致 | R6 |
| AC8 | 新增用例覆盖上述场景并全绿；离线测试结果与基线一致 | R7 / R8 |
| AC9 | 前端 `vue-tsc -b && vite build` 通过，概览与结果页显示的数字与后端一致 | R4 / R8 |

## Decisions

- **D1 = 迁移（已定，2026-09-25 由用户选定）**：任务改变 keyword 时，把旧 keyword 的结果数据迁移到新 keyword 的文件名下，价格快照同步迁移，保持「一个任务一份数据流」。该不变量是代码库其余部分（爬虫写入、任务删除、价格历史、引导导入）已经假设的，不引入第二套归属概念。代价：改写历史数据（迁移前自动备份，不可自动回滚，见 `design.md` §5.1）。
- **D2 = 拒绝重复 keyword（由 D1 派生）**：D1 方案下两个任务共用 keyword 会争同一份结果文件、迁移也会撞车，因此三条写入路径都必须拒绝重复 keyword。未采纳的替代方案（共享文件 + 聚合去重）留档于 `design.md` §5.2。

## In Scope

- 后端：概览聚合的身份与归属重构、`orphan_files` 输出、`summary.total_tasks`、改 keyword 时的数据迁移与删除任务的数据处置、三处路径的重复 keyword 校验、存量重复与无主数据的只读体检。
- 前端：概览计数改为读后端权威字段；结果页把无主结果显示出来并提供删除入口（**可在评审时裁掉**）。
- 测试：R7 的回归用例 + 现有 dashboard 用例补上计数与 `task_id` 断言。
- 规格沉淀：把「结果文件身份 = keyword 派生的确定性文件名」「任务身份不得用展示字符串」写回 `.trellis/spec/backend/`（Phase 3.3）。

## Out of Scope

- 无主文件的批量自动清理策略（本次只给可见性与单文件删除）。
- 结果文件名（`replace(' ', '_')`）与价格快照 slug（`normalize_keyword_slug`）两套归一化的统一：独立隐患，已在 `research/identity-and-lifecycle-map.md` 第 1 节记录，另开任务。
- 概览「已扫描商品 / 已发现推荐」的业务口径调整（只保证它们不再把无主数据算进去）。
- `00-bootstrap-guidelines` 任务负责的 spec 全面填充（本任务只补与自身相关的条目）。
- 线上 `data/app.sqlite3` 的任何进一步数据改动（feed_scan 残留已清理，其余体检只读）。

## Open Questions

无阻塞待决项。D1 / D2 已定（见 Decisions）；本计划待用户评审批准后进入实现（Phase 1.4 `task.py start`）。