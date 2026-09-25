# 任务 ↔ 结果文件：身份与生命周期地图

本文件是 Phase 2 实现者的代码地图。所有锚点均在本仓库核对过（2026-09-25，master 15d229f）。

## 1. 结果文件名的确定性规则

| 项 | 位置 | 规则 |
|---|---|---|
| 正向（写文件 / 删除时用） | `src/infrastructure/persistence/storage_names.py:11-12` | `build_result_filename(keyword) = keyword.replace(' ', '_') + "_full_data.jsonl"`（后缀常量 `:8`） |
| 反向（读旧数据时用） | `src/services/result_file_service.py:20` → `normalize_keyword_from_filename` | 仅剥掉 `_full_data.jsonl` 后缀 |
| 价格历史 slug | `src/infrastructure/persistence/storage_names.py:19-23` `normalize_keyword_slug` | **不同的一套归一化**：lower + 过滤非字母数字 |
| 结果记录表 key | `result_items.result_filename` | 由 `build_result_filename` 产生 |

**关键事实**：结果文件的归属由 keyword 决定，是确定性双射；但两套归一化（结果文件 vs 价格快照）不一致。`iPad Air M4` → 文件 `iPad_Air_M4_full_data.jsonl`，价格 slug `ipad_air_m4`；大小写或特殊字符不同的 keyword 会落到不同文件、但可能落到同一价格 slug（或反之），这是独立隐患。

## 2. 概览聚合的读取路径

```
GET /api/dashboard/summary
  → src/api/routes/dashboard.py:14-22   task_service.get_all_tasks()
  → src/services/dashboard_service.py:37-53  build_dashboard_snapshot(tasks)
       :38  task_lookup = {normalize_text(task.keyword): task}      # keyword → task
       :39-41 task_summaries = {task.task_name: build_empty_summary(task)}
       :45-48 for filename in await list_result_filenames():
                  summary = summarize_result_file(filename, task_lookup)
                  task_summaries[summary["task_name"]] = summary     # ← 身份用展示字符串
       :53  summary_list = sorted(task_summaries.values(), ...)
   → src/services/dashboard_service.py:24-34 _build_summary_metrics
       enabled_tasks / running_tasks  ← 直接数 tasks（口径正确）
       result_files / scanned_items / recommended_* ← 累加 summary_list（含幽灵条目）
```

`task_lookup` 的取值来源：`src/services/result_storage_service.py:223-239` 从 `result_items` 按 `result_filename` 分组，按 `MAX(crawl_time)` 倒序。

## 3. 文件 → 任务的匹配启发式（两轮猜测）

`src/services/dashboard_payloads.py:114-126` `_resolve_task`：

1. 先用最新记录里的「搜索关键字」查 `task_lookup`（即与任务当前 keyword 精确匹配，小写去空格）；
2. 失败则用最新记录里的「任务名称」在所有任务里找同名任务；
3. 都失败 → `:93-111` `_build_fallback_summary` 造一条 `task_id=None` 的摘要；
4. `:238-240` 该摘要照常写入 `task_summaries`，于是成为一个「幽灵任务」。

**注意**：第三步产生的条目会被第 2 节的 `result_files` / `scanned_items` 一并计入。`build_result_filename` 的双射关系在这条路径上完全没被使用——删除路径（第 4 节）反而依赖它。

## 4. 写入与销毁路径

| 场景 | 位置 | 行为 |
|---|---|---|
| 爬虫写结果 | `spider_v2.py` → `result_storage_service` | 按 keyword 决定 `result_filename` 落库 |
| 删除任务 | `src/api/routes/tasks.py:232-268`，清理逻辑 `:248-258` | 按**删除时刻的 keyword** 删 `result_items` + `price_snapshots`；keyword 历史上变过 → 旧文件残留 |
| 修改任务（含改 keyword） | `src/api/routes/tasks.py` 的 `update_task` | **完全没有结果文件处理**；旧 keyword 数据原地留下 |
| 删除单个结果文件 | `src/services/result_storage_service.py:259-267` `_delete_result_file_records_sync` | 已有能力，UI/清理由它承担 |
| 删除价格快照 | `src/services/price_history_service.py:194-202` | 按 `keyword_slug` 删 |

## 5. 数据约束现状

- `tasks` 表**没有** `task_name` / `keyword` 的唯一约束，只有非唯一索引 `idx_tasks_name`、`idx_tasks_group`（`data/app.sqlite3` 实测 schema）。
- 创建/更新路径也没有重复校验（`src/api/routes/tasks.py` 全文无重复校验）。
- 因此同名任务、同 keyword 任务都能建出来；同 keyword 意味着两个任务共用同一个结果文件与同一份去重状态。

## 6. 前端计数口径

- `web-ui/src/composables/useDashboard.ts:76` `totalTasks: taskSummaries.value.length`
- `web-ui/src/views/DashboardView.vue:62-69` 「监控任务」卡片直接用 `stats.totalTasks`
- 同一卡片组里 `enabledTasks` 来自后端 `summary.enabled_tasks`（数 tasks），所以幽灵条目存在时会出现「监控任务 5 / 已启用 4」这种自相矛盾。

## 7. 引导导入的历史包袱

`src/infrastructure/persistence/sqlite_bootstrap.py:116-142` `_import_results_if_needed`：仅在 `result_items` 为空时把 `jsonl/*.jsonl` 导入一次，并在 `:142` **无条件**标记完成（目录不存在时 `:124-127` 也会标记完成）。含义：

- 引导只跑一次，事后往 `jsonl/` 丢文件不会再被导入；
- 被引导导入的旧文件，其记录里的「任务名称」/「搜索关键字」可能对应已不存在的任务，天然产生无主文件（本次 feed_scan 之外的第二个来源）。