# 技术设计：任务身份与结果文件归属

> 状态：D1 未决，本文件按推荐方案（D1-1）成稿；D1 决策后「生命周期」一节收敛为单分支。

## 1. 设计目标与不变量

修复后系统必须满足（可被测试直接钉住）：

- **I1** `task_summaries` 与任务一一对应（`len(task_summaries) == len(tasks)`），每条 `task_id` 非空。
- **I2** 一个结果文件最多归属一个任务；无法归属的文件只出现在 `orphan_files`，不参与任务计数与任务口径的累计。
- **I3** `summary.total_tasks` 由后端给出，等于任务数；前端不再从数组长度推导。
- **I4** 任务被删除后，其全部数据流随之消失（不留无主残留——除非用户选择了 D1-3 的最小策略，见 §5）。

## 2. 身份与归属模型

**身份**：任务用 `Task.id`（整数主键）。`task_name`、`keyword` 降级为展示 / 匹配用的字符串，任何 dict key、任何去重都不得使用它们。这一条直接消除同名合并（`research/repro-scenarios.md` 场景 A）。

**归属**：结果文件的身份是 `build_result_filename(keyword)`（`src/infrastructure/persistence/storage_names.py:11-12`），与写入路径、删除路径（`src/api/routes/tasks.py:257`）同一规则。聚合改为使用这个确定性映射，而不是 `dashboard_payloads._resolve_task` 的两轮字符串猜测。

**归属解析（新函数，建议落 `src/services/dashboard_payloads.py` 或新模块 `result_stream_service.py`）**：

```
resolve_stream_owners(tasks, filenames, metrics_by_file) -> (owner_by_file, conflicts)
  pass 1（确定性）:
      by_filename = {build_result_filename(t.keyword): t for t in tasks}
      若同一 keyword 出现两次 → 记录冲突（见 §6），取 id 最小者
      file 命中 by_filename → 归属该任务
  pass 2（历史数据回退，仅处理 pass 1 未认领的文件）:
      a) 记录内「搜索关键字」归一化后 == 某任务 keyword 归一化值 → 归属（归一化：strip + lower + 空格→下划线，与文件名规则一致）
      b) 再退：记录内「任务名称」== 某任务 task_name → 归属
      多个候选 → 取 id 最小者并记 conflict（写日志，不静默）
  未被任何 pass 认领 → orphan
```

**为什么保留 pass 2**：现有线上数据里存在「改过 keyword 但名字没变」的文件，它们目前靠名称回退挂在任务上（`research/repro-scenarios.md` 场景 B）。去掉回退会在升级瞬间把这类数据打成无主。回退只在没有确定性归属时兜底，且保证一个文件只被认领一次。

## 3. 接口契约（`GET /api/dashboard/summary`）

```jsonc
{
  "summary": {
    "total_tasks": 4,          // 新增：权威任务数 = len(tasks)
    "enabled_tasks": 4,
    "running_tasks": 1,
    "result_files": 4,         // 语义收紧：仅统计已归属文件
    "orphan_files": 0,         // 新增：无主文件数
    "scanned_items": 234,      // 语义收紧：仅统计已归属文件
    "recommended_items": 51,
    "ai_recommended_items": 51,
    "keyword_recommended_items": 0,
    "last_updated_at": "..."
  },
  "task_summaries": [
    { "task_id": 0, "task_name": "...", "keyword": "...", "filename": "iPad_Air_M4_full_data.jsonl",
      "enabled": true, "is_running": true, "cron": "...", "region": "...",
      "total_items": 77, "recommended_items": 12, "latest_crawl_time": "...", "...": "其余字段不变" }
  ],
  "orphan_files": [            // 新增
    { "filename": "feed_scan_full_data.jsonl", "task_name": "推荐流信息流发现", "keyword": "feed_scan",
      "total_items": 5, "latest_crawl_time": "2026-09-25T20:33:55", "reason": "no_owner" }
  ],
  "recent_activities": [ "...形状不变..." ],
  "focus_file": "iPad_Air_full_data.jsonl"
}
```

- `task_summaries` 里 `task_name` / `keyword` 取**任务当前值**（不再取记录里的历史值），避免改 keyword 后卡片显示旧关键词。
- `recent_activities` 形状不变：无主文件产生的活动仍然保留（活动是日志，不参与计数），标题沿用记录里的任务名。
- 向后兼容：只在 `summary` 增加字段、在顶层增加 `orphan_files`，不改动既有字段名与类型；`focus_file` 语义不变（仍取有文件的最新任务）。

**`GET /api/results/files`**（`src/api/routes/results.py:56-59`）：保持 `files` 为字符串数组，新增同形状的 `orphan_files` 字符串数组。前端 `resultsApi.getResultFiles()` 现有 `data.files || []` 不受影响。

## 4. 聚合实现要点（`build_dashboard_snapshot` 重写）

```
tasks = task_service.get_all_tasks()
filenames = await list_result_filenames()                 # 已按最新爬取时间倒序
metrics = {f: await load_result_summary(f) for f in filenames}   # 保持现有实现；见性能注记
owner_by_file, conflicts = resolve_stream_owners(tasks, filenames, metrics)
summaries = [ 由 tasks 直接生成，逐任务挂上自己那份 metrics（无则 filename=None + 零值） ]
orphans   = [ 未被认领的 filename + 其 metrics 摘要 + 记录内任务名/关键字 ]
summary   = { total_tasks=len(tasks), result_files=已认领数, orphan_files=len(orphans),
              scanned/recommended=已认领文件累加, enabled/running 直接数 tasks }
```

- **不再需要以字符串为 key 的字典**：直接从 `tasks` 生成列表，`I1` 由结构保证，而不是靠约定。
- 活动生成：`build_task_state_activities(tasks)` 保持不变；扫描 / 推荐活动沿用 `summarize_result_file` 的输出，但对无主文件单独取用（`_build_fallback_summary` 不再写入摘要列表，改写入 orphan 项）。
- 性能注记：`load_result_summary` 会把该文件全部可见记录读进内存（`src/services/result_storage_service.py:395-431`）。现有实现已经对每个文件都这样做，本设计不加重；无主文件若数量增长，可改用一条 SQL 聚合（`COUNT` / `MAX(crawl_time)` / 推荐计数）替代全量读取——列为可选优化，不作为验收项。

## 5. D1 决策分支：改 keyword 时既有数据怎么办

三个方案共享 §2–§4 的全部工作，只在「改 keyword / 删除任务」这两处分叉。

### D1-1 迁移（推荐）

- 新增 `migrate_result_stream(old_keyword, new_keyword) -> int`（`src/services/result_storage_service.py`）：
  单事务内 `UPDATE result_items SET result_filename = build_result_filename(new) WHERE result_filename = build_result_filename(old)`；价格快照同步 `UPDATE price_snapshots SET keyword_slug/keyword = 新值 WHERE keyword_slug = 旧 slug`。记录里的「搜索关键字 / 任务名称」保持历史原值不改写（归属已由文件名决定，payload 是历史事实）。
- 调用点：`PATCH /api/tasks/{id}`（`src/api/routes/tasks.py` 的 `update_task`）检测到 keyword 变化时：先校验新 keyword 未被其它任务占用 → 迁移 → 更新任务字段。任一步失败返回错误，任务字段不落新值（尽量原子；sqlite 单事务保证数据侧原子）。
- 目标文件已存在（旧 keyword 与新 keyword 撞车）→ 合并（直接改归属，行共存于同一文件）。
- 删除路径**不需要改动**：当前 keyword 就是唯一数据流，`src/api/routes/tasks.py:248-258` 的清理天然正确。
- 代价：改写历史数据，不可自动回滚（迁移前自动备份数据库，复用本任务已用过的 sqlite backup API 做法）；一次操作两张表。
- 收益：保持「一个任务一份数据流」这个代码库其它部分（写入、删除、价格历史、引导导入）已经假设的不变量，不引入第二套归属概念。

### D1-2 保留归属链（不迁移）

- 任务新增历史 keyword 记录（`tasks.result_keywords_json` 字段或 `task_result_streams` 表，二选一；倾向新表，避免再往 `tasks` 堆 JSON 列）。
- 概览按任务聚合多个文件：`filename` 取最新那个用于 focus 与跳转，`total_items` 等指标跨文件累加。
- 删除任务遍历该列表逐个清理。
- 需要补充规则：旧 keyword 后来被别的任务复用时，归属链必须断开（否则一个文件两个主人）。
- 代价：schema 变更 + 聚合与删除都变复杂 + 新增歧义规则；收益：零数据改写，可随时回退代码。

### D1-3 最小修复

- 不改写、不追溯：改 keyword 后旧文件直接进入 `orphan_files`，任务从新 keyword 重新开始；删除任务只清当前 keyword。
- 代价：任务历史趋势断档（概览卡片换 keyword 后归零），用户需手动清理无主文件；收益：改动最少、风险最低。
- 与 I4 的冲突：此时删除任务仍可能留下无主数据，属于**已知且接受**的行为，需在 PRD 的 Out of Scope 里写明。

## 6. D2 重复 keyword

- D1-1 下必须拒绝：两个任务用同一 keyword 会共用同一结果文件，迁移也会撞车。校验点：`POST /api/tasks/`（`create_task`）、`POST /api/tasks/generate`（生成路径最终也落到 `build_task_create` → `create_task`）、`PATCH /api/tasks/{id}`（keyword 变化时）。错误信息需可读（如「关键词 `iPad Air M4` 已被任务「…」使用，请改用不同的关键词或先修改该任务」），HTTP 400。
- 存量数据：提供只读体检输出（重复 keyword 清单），不自动改数据；由用户决定处置。
- 若用户坚持允许共享：聚合层按「文件」去重计数（同一文件只累加一次），并在条目上加 `shared_by` 标注；此为 D1-2/3 才可行的降级路径。

## 7. 前端改动

- `web-ui/src/composables/useDashboard.ts`：`totalTasks` 改为读 `summary.total_tasks`（`?? 0`）；新增 `orphanFiles` 计算属性。
- `web-ui/src/views/DashboardView.vue`：卡片值随之变化；评审保留项——加一块「无主数据」提示（仅有数据时显示，点击进结果页）。
- 结果页：文件列表项对无主文件加标记 + 删除入口（复用 `DELETE /api/results/files/{filename}`）。删除时若该 keyword 已无任务使用，同步清理价格快照（与任务删除路径同规则，`src/services/price_history_service.py:194-202`）。
- 概览列表没有把 `task_summaries` 当 key 渲染（只用 `filename` 找 focus、用 `task_name` 展示），因此字典 key 从 name 换到 id 不产生前端破坏。

## 8. 兼容性、迁移与体检

- 契约向后兼容（§3）；无 schema 变更（D1-1/D1-3）；D1-2 需要迁移存量任务的 keyword 列表。
- 升级后首次打开概览即可自愈：以前计入任务的幽灵条目变为 `orphan_files`，计数立刻与任务管理一致——线上 feed_scan 那类数据无需再手工清理计数。
- 存量体检（只读，建议做成一次性脚本或 API）：重复 keyword 清单、无主文件清单及其条数。不自动修改。

## 9. 权衡与不做的事

- 不做「无主文件自动删除」：数据是否仍有价值只有用户知道。
- 不统一结果文件名与价格快照 slug 两套归一化（独立隐患，已记入 `research/identity-and-lifecycle-map.md` §1）。
- 不为概览引入缓存或异步聚合：当前数据量下没必要，避免掩盖 I1 的验证。

## 10. 上线与回滚

- 纯代码回滚即可恢复旧行为（无 schema 变更时）；D1-1 的迁移操作是唯一不可逆动作，执行前自动备份 `data/app.sqlite3`（sqlite backup API，参考 `data/backups/app.sqlite3.20260925-before-feedscan-cleanup`）。
- 实现顺序建议让回滚粒度最细：测试 → 归属解析 → 聚合重构 → 计数权威化 → 生命周期 → 前端 → spec 沉淀（见 `implement.md`）。