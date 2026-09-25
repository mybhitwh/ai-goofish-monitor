# 根因与现场证据（含已执行的数据清理）

## 1. 用户报告的现象

概览页「监控任务」显示 5，任务管理页只有 4 个任务。

## 2. 现场数据（清理前，data/app.sqlite3）

`tasks` 表 4 行，`result_items` 里却有 5 个不同的 `result_filename`：

| result_filename | task_name（记录内） | keyword | 归属 |
|---|---|---|---|
| `iPad_Air_M4_full_data.jsonl` | iPad Air M4 256G 全国包邮 | iPad Air M4 | 任务 id=0 |
| `iPad_Air_full_data.jsonl` | iPad Air M4 256G 上海包邮或自取 | iPad Air | 任务 id=1 |
| `iPad_Air_8_full_data.jsonl` | iPad Air M4 256G 全国包邮·Air8写法 | iPad Air 8 | 任务 id=2 |
| `iPad_Air_11_256_full_data.jsonl` | iPad Air M4 256G 全国包邮·11寸写法 | iPad Air 11 256 | 任务 id=3 |
| `feed_scan_full_data.jsonl` | 推荐流信息流发现 | feed_scan | **无任务**（已移除的 feed_scan 工具遗留 5 条） |

用真实代码跑一遍聚合（`build_dashboard_snapshot` + `SqliteTaskRepository`）得到：

```
任务管理列表条数: 4
概览 task_summaries 条数: 5
  - task_id=0    iPad Air M4 256G 全国包邮        | iPad_Air_M4_full_data.jsonl
  - task_id=3    …·11寸写法                      | iPad_Air_11_256_full_data.jsonl
  - task_id=1    …上海包邮或自取                  | iPad_Air_full_data.jsonl
  - task_id=2    …·Air8写法                      | iPad_Air_8_full_data.jsonl
  - task_id=None 推荐流信息流发现 (feed_scan)      | feed_scan_full_data.jsonl   ← 幽灵
```

结论：概览的「监控任务」数是 `task_summaries.length`（`web-ui/src/composables/useDashboard.ts:76`），而该数组把无主结果文件也当成任务。因果链见 `identity-and-lifecycle-map.md` 第 2、3 节。

## 3. 已执行的数据清理（本任务之外，已完成）

用户要求「只清理数据」，已完成，代码零改动（`git status` 干净）：

- 备份：`data/backups/app.sqlite3.20260925-before-feedscan-cleanup`（sqlite backup API，含 WAL 中未落盘内容，保留全部 5 行，可回退）。
- 删除：`delete_result_file_records('feed_scan_full_data.jsonl')` → 5 行；`delete_price_snapshots('feed_scan')` → 0 行（该 keyword 从无价格快照）。
- 其他位置核实为空：`item_annotations`、`result_blacklist_rules` 均 0 行；文件系统无 feed_scan 的 jsonl / 日志 / 图片。
- 端到端验证（对运行中的服务）：`/api/tasks` 4 条；`/api/dashboard/summary` 的 `task_summaries` 4 条且 `task_id` 全部非空；`result_files` 由 5 降为 4。

**清理只消除了这一次的表象**：无主文件由外部工具产生，代码路径仍会继续制造（见 `repro-scenarios.md` 场景 D/E）。

## 4. 未受影响的口径

`enabled_tasks` / `running_tasks` 由 tasks 直接统计（`src/services/dashboard_service.py:26-27`），因此幽灵条目存在时页面会自相矛盾：卡片同时显示「监控任务 5」「已启用 4」。这是判断该缺陷最直观的现场信号。

## 5. 测试缺口

`tests/integration/test_api_dashboard.py` 全部用 `next(item for item in payload["task_summaries"] if item["task_name"] == ...)` 查找条目，**从未断言条数**，也没断言 `task_id is not None`。所以「同名合并」与「幽灵条目」都逃过了现有用例。

另注：该测试把 repository 的 `db_path` 指到 `tmp/app.sqlite3`，而结果存储走默认路径 `data/app.sqlite3`（`DEFAULT_DATABASE_PATH`，相对 cwd），实际是**两个库**——写测试时要显式设置 `APP_DATABASE_FILE`，否则会踩到本仓库的第二个陷阱（引导导入只跑一次，见 `identity-and-lifecycle-map.md` 第 7 节）。