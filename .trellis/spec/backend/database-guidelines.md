# 数据库指南（SQLite）

> 本文件描述本仓库**实际**的数据层做法，不是理想设计。已知技术债如实标注。
> 读者：后续 AI 子代理与新同事。最后核对：2026-09-26（master `d3b58a5`，并用只读连接核对生产库 `data/app.sqlite3`）。
> 目录与模块划分见 `directory-structure.md`；测试基线与构建命令见 `quality-guidelines.md`。

---

## 1. 唯一生产库与安全边界

- 唯一业务库：`data/app.sqlite3`。默认路径常量 `DEFAULT_DATABASE_PATH` 在 `src/infrastructure/persistence/storage_names.py:7`；`get_database_path()` 允许环境变量 `APP_DATABASE_FILE` 覆盖（`src/infrastructure/persistence/sqlite_connection.py:146-147`）。
- 该文件是**运行态**，`.gitignore` 已忽略 `data/`。禁止提交、覆盖、重置、删除；生产实例（u12）由 systemd `goofish.service` 托管使用。
- 备份放 `data/backups/`，仓库内已有真实备份：`data/backups/app.sqlite3.20260925-before-feedscan-cleanup`。
- 旧文件引导（legacy bootstrap）：`src/infrastructure/persistence/sqlite_bootstrap.py` 在首次启动时把 `config.json`（任务）、`jsonl/*.jsonl`（结果）、`price_history/*_history.jsonl`（价格快照）一次性导入 SQLite，并写 `bootstrap:legacy_tasks` / `bootstrap:legacy_results` / `bootstrap:legacy_price_snapshots` 标记（`sqlite_bootstrap.py:23-25`、`:28-41`），之后不再重复导入。导入语句用 `INSERT OR IGNORE`，历史 jsonl 与库冲突时以库为准（`sqlite_bootstrap.py:198`、`:230`）。
- 只读核对生产库（不要开写入连接）：

```bash
.venv/bin/python -c "import sqlite3;c=sqlite3.connect('file:data/app.sqlite3?mode=ro',uri=True);print(list(c.execute('SELECT key,value FROM app_metadata ORDER BY key')))"
```

## 2. 连接、事务与 PRAGMA

- 统一入口 `sqlite_connection()` contextmanager（`sqlite_connection.py:208-220`）：建父目录 → `sqlite3.connect` → `row_factory = sqlite3.Row` → `finally: conn.close()`。
- PRAGMA（`sqlite_connection.py:154-157`）：`journal_mode=WAL`、`foreign_keys=ON`、`busy_timeout=5000`（常量 `BUSY_TIMEOUT_MS`）。
- **没有自动提交**：contextmanager 不 commit；每个写函数自己 `conn.commit()`（`result_storage_service.py:208`、`price_history_service.py:155`）。漏 commit 的写入会随连接关闭一起丢失。
- 线程模型：服务层用 `asyncio.to_thread` 把同步 sqlite 调用移出事件循环（`result_storage_service.py:164-165`）；启动引导用 `BOOTSTRAP_LOCK` 线程锁串行化（`sqlite_bootstrap.py:19,35`）。
- 启动时初始化：API 进程在 lifespan 里调 `bootstrap_sqlite_storage()`（`src/app.py:75-79`）；爬虫侧各服务在每次读写前自行调用（幂等）。

## 3. Schema 与迁移

- 全部 schema 语句集中在 `SCHEMA_STATEMENTS`（`sqlite_connection.py:17-143`），一律 `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`；`init_schema()` 按序执行后跑迁移函数再 commit（`sqlite_connection.py:160-165`）。
- **迁移约定**（以现有两个迁移为准，`sqlite_connection.py:168-205`）：
  1. `app_metadata` 是 marker 表（`key TEXT PRIMARY KEY, value TEXT NOT NULL`，`:18-23`）。迁移函数先查 `migration:<name>`，存在即 `return`；
  2. 加列前再用 `PRAGMA table_info(<table>)` 判一次（marker + 列检查双保险）；
  3. 执行 DDL（`ALTER TABLE ... ADD COLUMN ... NOT NULL DEFAULT ...`）并补索引；
  4. 写 `INSERT OR REPLACE INTO app_metadata(key, value) VALUES ('migration:<name>', 'done')`。
- 现有 marker：`migration:result_items_status`（加 `result_items.status` 并补 `idx_results_filename_status_crawl`）、`migration:tasks_group_id`（加 `tasks.group_id` 并补 `idx_tasks_group`）。生产库 `app_metadata` 实测 5 条：3 个 `bootstrap:*` + 2 个 `migration:*`。

### 迁移纪律（上线前必读）

- 迁移必须**幂等**：重复执行结果不变；DDL 与 marker 在同一连接、同一次 `init_schema` 提交，失败不留半套。
- **上线前必须备份**，且 WAL 模式下不要裸 `cp`（裸 cp 可能漏掉 `-wal` 中的已提交数据）。仓库既有任务文档的口径是「sqlite backup API，勿用裸 cp，需覆盖 WAL」：

```bash
.venv/bin/python -c "import sqlite3; s=sqlite3.connect('data/app.sqlite3'); d=sqlite3.connect('data/backups/app.sqlite3.<时间戳>-before-migrate'); s.backup(d)"
# 等价 CLI（同样覆盖 WAL）：sqlite3 data/app.sqlite3 ".backup 'data/backups/app.sqlite3.<时间戳>'"
```

- **回滚 ≠ 数据回滚**：`git revert` 只回滚 schema 与代码逻辑，已经写进库的数据不会回来。真实先例是「跨文件屏蔽收敛」（任务 `09-25-fix-blocked-item-reappear-merged-view`，评审结论已定（2026-09-26，见该任务 `prd.md` 的 Decisions；状态仍为 planning、尚未实施））：计划新增 `item_hidden_marks` 表并执行 `migration:item_hidden_marks`，把「A 文件 hidden、B 文件 active」的商品统一收敛为 hidden。这类迁移**改写历史数据**，撤销代码后原状态只能从备份库恢复。同类迁移的固定动作：备份 → 在生产库副本上演练（如 `cp data/app.sqlite3 /tmp/app-converge.sqlite3` 后指向副本执行 bootstrap）→ 打印受影响清单 → 用只读 SQL 核对收敛计数。
- 迁移后用只读 SQL 核对，例如跨文件屏蔽分裂计数（迁移后应为 0）：

```sql
SELECT COUNT(*) FROM result_items a JOIN result_items b
  ON a.link_unique_key=b.link_unique_key AND a.result_filename<>b.result_filename
 WHERE a.status='hidden' AND b.status='active';
```

## 4. 表与键（2026-09-26 生产库实测）

| 表 | 关键约束 | 用途 / 主要写入方 |
| --- | --- | --- |
| `app_metadata` | `key` 主键，`value NOT NULL`（`:18-23`） | 迁移与 bootstrap marker；无其他业务语义 |
| `tasks` | `id INTEGER PRIMARY KEY`；`group_id` 可空（`:26-48`） | 任务定义；`SqliteTaskRepository.save` 用 `INSERT OR REPLACE`（`sqlite_task_repository.py:88-107`） |
| `result_items` | `UNIQUE(result_filename, link_unique_key)`（`:70`）；`is_recommended INTEGER NOT NULL`（`:65`）；`status TEXT NOT NULL DEFAULT 'active'`（`:68`） | 商品结果明细；`save_result_record`（`INSERT OR IGNORE`）、`update_item_status`（显式 `UPDATE`）、复核写 `expired` |
| `price_snapshots` | `UNIQUE(keyword_slug, run_id, item_id)`（`:91`） | 每次扫描/复核的价格观测，append-only（`price_history_service.py:129-155`） |
| `result_blacklist_rules` | `result_filename TEXT PRIMARY KEY`（`:96`） | 按结果文件的黑名单词，UPSERT（`result_storage_service.py:472-482`） |
| `item_annotations` | `link_unique_key TEXT PRIMARY KEY`（`:103`） | **跨任务全局**的备注/标签（`item_annotation_service.py:4-6,101-112`） |
| `task_groups` | `id INTEGER PRIMARY KEY`（`:110-117`） | 任务组（`sqlite_task_group_repository.py:52-92`） |

- 索引 9 个，全部 `IF NOT EXISTS`。结果类复合索引以 `result_filename` 打头（`idx_results_filename_crawl/publish/price/recommended/status_crawl`），快照类以 `keyword_slug` 打头（`idx_snapshots_keyword_time`、`idx_snapshots_keyword_item_time`），另有 `idx_tasks_name`、`idx_tasks_group`。查询 WHERE 以 `result_filename = ?` 打头（`result_storage_service.py:57`）与索引前缀一致。
- 排序表达式 `_sort_expression`（`result_storage_service.py:70-73`）在最前面加 `CASE WHEN status='active' THEN 0 ELSE 1 END`，普通索引无法覆盖这一层；这是为「可见商品优先」付出的排序成本（已知代价，非事故）。

## 5. 读写模式（实际做法）

### 写入

- `INSERT OR IGNORE` 是默认写入方式：`result_items`（`result_storage_service.py:181-207`）、`price_snapshots`（`price_history_service.py:129-155`）、bootstrap（`sqlite_bootstrap.py:198,230`）。**语义是「已存在行保持原样」**：同一 `(result_filename, link_unique_key)` 再次入库时，价格、标题、`raw_json` 都不会刷新。增量数据靠快照表（价格）或天然新行（新商品）承载。
- 需要更新的场景必须显式 `UPDATE` 或 UPSERT，不能把 `INSERT OR IGNORE` 当更新用：
  - 可见性：`update_item_status` 用 `UPDATE result_items SET status=? WHERE result_filename=? AND item_id=?`（`result_storage_service.py:442-450`）；
  - 黑名单：`ON CONFLICT(result_filename) DO UPDATE`（`:472-482`）；
  - 标注：`ON CONFLICT(link_unique_key) DO UPDATE`（`item_annotation_service.py:101-112`）。
- `tasks` / `task_groups` 用 `INSERT OR REPLACE`（`sqlite_task_repository.py:90`、`sqlite_task_group_repository.py:64`）。注意 REPLACE 是「删旧行 + 插新行」，调用方必须提供整行字段（仓库的 `_task_values` 负责补全，`sqlite_task_repository.py:123-132`）。
- 布尔列一律存 INTEGER：仓储层 `int(bool)`（`sqlite_task_repository.py:125-129`），结果层 `1 if analysis.get("is_recommended") else 0`（`result_storage_service.py:202`）。`is_recommended INTEGER NOT NULL` 意味着写入必须显式给 0/1，不能依赖默认值。
- JSON 列命名 `*_json`，写入用 `json.dumps(..., ensure_ascii=False)`（`keyword_rules_json`、`tags_json`、`blacklist_keywords_json`、`raw_json`）。

### 读取

- `_load_filtered_records_from_conn`（`result_storage_service.py:115-161`）是结果查询的唯一汇聚点，模式是「**SQL 取全量 → Python 侧过滤/装饰/切片**」：
  - SQL 只做 `result_filename`（+ 推荐筛选）与排序；黑名单命中、可见性、标注合并、标签筛选、`has_note` 全在 Python 侧；
  - 分页在 Python 完成：`_query_result_records_sync` 读全量后 `records[offset:offset+limit]`（`:311-325`）。后果：结果文件越大每页越慢（已知技术债，未做 SQL 分页）。
- 返回给上层的记录会追加 `_` 前缀装饰字段，**只存在于内存 dict，不落库**：`_status`、`_matched_blacklist_keywords`、`_hidden_reason`（取值 `expired` / `manual` / `rule`）、`_effective_hidden`（`:94-108`）、`_note`、`_user_tags`（`:153-155`）。
- 排序走 `SORT_COLUMN_MAP` 白名单（`:22-27`），未知 `sort_by` 回落到 `crawl_time`，避免把用户输入拼进 ORDER BY。
- 批量按 key 查询要分块：`_load_annotation_map_from_conn` 以 500 个 key 一批拼 `IN (...)`（`item_annotation_service.py:38-48`），规避 SQLite 变量数上限。`get_link_unique_keys_by_item_id` 按 `(result_filename, item_id)` 单查、无需分块，返回列表是对同文件重复行的兜底（`result_storage_service.py:508-520`）。
- 跨表查询示例：复核服务的 stale 扫描 JOIN `result_items` 与 `price_snapshots`（`item_recheck_service.py:64-79`），并用同格式的 `snapshot_time`（ISO `T` 分隔）做字符串比较。

## 6. 命名与身份

- 表名、列名一律 snake_case；时间列 TEXT 存 ISO 字符串；`snapshot_day` 这类派生列直接落库以避免前端重复解析。
- 结果流的身份是**文件名**，不是展示名：`build_result_filename(keyword) = keyword.replace(' ', '_') + '_full_data.jsonl'`（`storage_names.py:11-12`，后缀常量在 `:8`）。价格快照用 `normalize_keyword_slug` 归一化（`:19-24`）。
- `app_metadata` 的 key 分两族：`migration:*`、`bootstrap:*`。
- 对外装饰字段用 `_` 前缀（见上节），与原始 JSON 字段区分。

## 7. 测试隔离（硬要求）

- 默认库路径是相对路径 `data/app.sqlite3`，所以测试的主要隔离手段是 **`monkeypatch.chdir(tmp_path)`**：`tests/integration/test_api_results.py:17`、`tests/integration/test_api_annotations.py:42`、`tests/unit/test_price_history_service.py:10`、`tests/unit/test_utils.py:29`。
- 显式指定库用 **`APP_DATABASE_FILE`**：真实流量冒烟把库指到 workspace 副本（`tests/live/_support.py:163`）；集成测试也可以显式传 `db_path=tmp_path/...`（`tests/conftest.py:108-158` 的 `api_context`）。
- legacy jsonl 引导的测试例证：`tests/integration/test_api_annotations.py:41-50` 先 `chdir(tmp_path)`、在 `tmp_path/jsonl/` 写 `demo_full_data.jsonl`，随后 `GET /api/results/demo_full_data.jsonl` 触发 bootstrap 读库。`tests/integration/test_api_results.py:15-50` 同模式。
- **测试与脚本一律不得写生产库**。已知坑（来自 `09-25-dashboard-task-identity` 任务记录）：只把 repository 指向 tmp_path 不够，结果存储仍走默认相对路径，等于两个库；要么 `chdir`，要么显式 `APP_DATABASE_FILE`。
- 验证测试没碰生产库：

```bash
before=$(sha256sum data/app.sqlite3 data/app.sqlite3-wal 2>/dev/null | sha256sum)
.venv/bin/python -m pytest tests/integration/test_api_results.py -q
after=$(sha256sum data/app.sqlite3 data/app.sqlite3-wal 2>/dev/null | sha256sum)
[ "$before" = "$after" ] && echo "生产库未变化"
```

## 8. 反模式（本仓库真实踩过）

1. **把 `INSERT OR IGNORE` 当更新**：期望重爬刷新 `result_items` 的价格/标题，结果行纹丝不动。要更新必须显式 `UPDATE`/UPSERT；价格追踪的正确承载是 `price_snapshots`。
2. **用展示字符串当身份 key**：`dashboard_service.build_dashboard_snapshot` 用 `{normalize_text(task.keyword): task}` 和 `{task.task_name: summary}` 建索引（`dashboard_service.py:38-41`），`_resolve_task` 还会按 `任务名称` 字符串回退（`dashboard_payloads.py:114-126`）——同名任务互相覆盖、改名/删除后出现幽灵条目、无主结果文件被计成任务。已立任务 `09-25-dashboard-task-identity`（planning）改为按 `Task.id` 建身份。规则：dict key、去重、归属判定一律用主键，展示字段只用于展示。
3. **把可见性列当业务判定用**：`result_items.status` 的合法值只有 `active/hidden/expired`（`result_storage_service.py:436`），它表达「展示与否」；「推荐/不推荐」是 `raw_json.ai_analysis.is_recommended` + `analysis_source`。不要再把「已售/风控失败/AI 不推荐」等判定塞进 `status`。唯一先例是复核服务把售出/下架写成 `expired`（`item_recheck_service.py:295-301`），且刻意保持**文件级**语义，不被商品级屏蔽收敛（见任务决策）。
4. **跨文件语义混用存储键**：屏蔽曾按结果文件存，而合并视图按商品聚合展示，导致同一商品在别的任务文件里「复活」（任务 `09-25-fix-blocked-item-reappear-merged-view`）。规则：只要展示层跨任务聚合，存储键就必须跨任务一致——`item_annotations` 按 `link_unique_key` 全局存是正例。
5. **WAL 库裸 `cp` 备份**：备份必须覆盖 `-wal`，用 sqlite backup API 或 `.backup` 命令。

## 9. 常用验证命令

```bash
# 所有写入语句一览（确认新写入走了哪条路径）
grep -rn "INSERT OR IGNORE\|INSERT OR REPLACE\|ON CONFLICT\|UPDATE result_items" src/

# 迁移 marker 与生产 schema（只读）
.venv/bin/python -c "import sqlite3;c=sqlite3.connect('file:data/app.sqlite3?mode=ro',uri=True);print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\")])"

# 数据层定向测试
.venv/bin/python -m pytest tests/integration/test_api_results.py tests/integration/test_api_annotations.py tests/unit/test_price_history_service.py -q
```