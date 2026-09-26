# 实施计划：屏蔽状态商品级化

前置：`design.md`（方案 A）已评审通过、`prd.md` Decisions（2026-09-26）已定稿、任务状态 `in_progress`（`task.py start`）后才动手。改动集中在后端（`src/`）与测试（`tests/`），前端原则上不动。

改动面：`src/infrastructure/persistence/{sqlite_connection.py,sqlite_bootstrap.py}`、`src/services/result_storage_service.py`、`src/services/result_hidden_mark_service.py`(新)、`src/api/routes/results.py`、`tests/`。

## 阶段 0：动手前（门禁）

- [ ] 读 `.trellis/spec/backend/{database-guidelines.md,error-handling.md}` 与 `.trellis/spec/guides/cross-layer-thinking-guide.md`（确认分层：路由 → 服务 → 基础设施，不在路由里写 SQL）。
- [x] 评审结论已定（2026-09-26，见 prd.md Decisions）：屏蔽语义=商品级；expired 保持文件级；存量分裂数据收敛为「已屏蔽」；迁移前备份生产库。
- [ ] **备份生产库**：`cp data/app.sqlite3 data/app.sqlite3.bak-$(date +%Y%m%d-%H%M)`，确认文件大小正常；`data/` 不提交（`.gitignore` 已忽略）。
- [ ] 现状基线留痕：`.venv/bin/python -m pytest tests/ -s`（记录 136/130/3/3 是否仍成立）。
- [ ] 现状缺陷快照（修复后用同一 SQL 对比）：
  ```
  .venv/bin/python - <<'PY'
  import sqlite3; c=sqlite3.connect('file:data/app.sqlite3?mode=ro',uri=True)
  print(c.execute("""SELECT COUNT(*) FROM result_items a JOIN result_items b
    ON a.link_unique_key=b.link_unique_key AND a.result_filename<>b.result_filename
    WHERE a.status='hidden' AND b.status='active'""").fetchone())
  PY
  ```
- 回滚点：本阶段无代码改动。

## 阶段 1：数据层（标记表 + 标记服务）

- [ ] `sqlite_connection.py`：`SCHEMA_STATEMENTS` 加 `CREATE TABLE IF NOT EXISTS item_hidden_marks(link_unique_key TEXT PRIMARY KEY, updated_at TEXT NOT NULL)`。
- [ ] 新建 `src/services/result_hidden_mark_service.py`（仿 `item_annotation_service.py` 的结构与 docstring 风格）：
  - `load_hidden_mark_keys_from_conn(conn, keys) -> set[str]`（去重 + 500 分块 `IN`）；
  - `load_hidden_mark_keys_sync(keys) -> set[str]`；
  - `set_item_hidden_sync(link_unique_key, hidden: bool) -> None`（UPSERT / DELETE + `updated_at`）。
- [ ] 单测 `tests/unit/test_result_hidden_marks.py`：upsert 后再 upsert 幂等；delete 后查不到；批量查询分块边界（>500 keys）正确。
- 门禁：`.venv/bin/python -m pytest tests/unit/test_result_hidden_marks.py -s` 全绿。
- 回滚点：删除新文件 + 移除 SCHEMA 语句（无数据影响）。

## 阶段 2：读取遮蔽（唯一汇聚点）

- [ ] `result_storage_service.py::_load_filtered_records_from_conn`：在取 annotations 处一并对本文件当页 keys 调 `load_hidden_mark_keys_from_conn`。
- [ ] `_decorate_record_visibility(..., globally_hidden: bool)` 按 `design.md §3.2` 改 `_hidden_reason` 优先级与 `_status`（`globally_hidden → 'hidden'`）。
- [ ] `include_hidden` / 标签 / `has_note` 过滤顺序不变（`_is_record_visible` 语义不变）。
- [ ] 单测：有标记 + 行 `status='active'` → 默认列表不返回；`include_hidden=true` → 返回且 `_status='hidden'`、`_hidden_reason='manual'`；行级 `expired` 仍为 `_hidden_reason='expired'`。
- 门禁：`.venv/bin/python -m pytest tests/unit -s` 全绿（无新增失败）。
- 回滚点：本阶段可独立 revert（标记表为空时行为与现状完全一致）。

## 阶段 3：写入路径（PATCH 语义）

- [ ] `result_storage_service.py`：新增商品级写入函数（解析 key → 标记 upsert/delete → `UPDATE result_items SET status=? WHERE link_unique_key=?`，同一连接/事务）；保留现有文件级 `update_item_status` 供 `expired` 与既有调用使用。
- [ ] `src/api/routes/results.py::patch_item_status`：`hidden`/`active` 走商品级；`expired` 走文件级；404 语义、响应体不变。
- [ ] 确认 `item_recheck_service.py:299` 的 `expired` 写入**未被**商品级化（保持文件级）。
- [ ] 集成测试 `tests/integration/test_api_item_status.py`（复用 `test_api_annotations.py` 的 helper 模式，造两个文件含同一 `item_id`）：
  - PATCH A hidden → `GET B` 默认不含该商品；`GET B?include_hidden=true` 中 `_status='hidden'`、`_hidden_reason='manual'`；
  - PATCH A active → `GET B` 恢复可见；
  - PATCH A expired → 仅 A 隐藏，B 不受影响；
  - PATCH 不存在商品 → 404（不变）。
- 门禁：`pnpm` 无关；`.venv/bin/python -m pytest tests/integration/test_api_item_status.py tests/integration/test_api_annotations.py tests/integration/test_api_results.py -s` 全绿。
- 回滚点：阶段 2 与 3 分开提交，写入路径 revert 后读取遮蔽仍在（无害）。

## 阶段 4：存量收敛（幂等迁移 + 未来文件保护）

- [ ] `sqlite_connection.py`：`converge_item_hidden_marks(conn)`（`design.md §3.4` 三步）+ `_migrate_item_hidden_marks(conn)`（marker `migration:item_hidden_marks`，挂在 `init_schema`）。
- [ ] 单测：直接调 `converge_item_hidden_marks` —— 造 `hidden(A)+active(B)` → 收敛后皆 hidden；重复调用结果不变；`expired` 行不受影响。
- [ ] AC4 用例：屏蔽后向**新文件**插入同商品 active 行 → 该文件默认列表不返回该商品（读取遮蔽生效）。
- [ ] 本地用生产库**副本**演练迁移：`cp data/app.sqlite3 /tmp/app-converge.sqlite3` 后指向副本执行 bootstrap，核对收敛计数 → 0（不碰生产库）。
- 门禁：`.venv/bin/python -m pytest tests/ -s` 不低于基线（136 collected / 130 passed / 3 failed / 3 skipped，3 个失败为存量，见 `prd.md` AC6）。
- 回滚点：迁移第 3 步是数据写入，回滚需恢复备份库（`design.md §3.7`）。

## 阶段 5：整体验收与上线

- [ ] 全量测试 + 只读核对 SQL（AC1/AC2/AC3）。
- [ ] 前端未改动则跳过 `pnpm build`；若采纳「合并视图可选清理」则 `cd web-ui && pnpm build` 必须执行并同步 `dist/`。
- [ ] 提交拆分建议：`feat(results): 屏蔽状态商品级化（标记表 + 读取遮蔽）` → `feat(results): PATCH 状态接口商品级写入` → `chore(db): 收敛跨文件屏蔽状态（幂等迁移）`。
- [ ] 上线：**先备份** `data/app.sqlite3`，再请用户代跑 `sudo systemctl restart goofish`（u12-guard 拦 sudo），随后三件套健康检查：`systemctl is-active goofish`；`curl 127.0.0.1:8000/api/groups`；`curl 127.0.0.1:8080/goofish/`。
- [ ] 生产核验（GUI + 只读 SQL，AC7 留痕）：在组「iPad Air 8」合并视图屏蔽一个跨 4 文件的商品 → 4 个单任务视图与合并视图都不再出现；取消屏蔽 → 全部恢复；记录 SQL 输出到 `check.jsonl` 或完成说明。
- [ ] 观察窗：重启后确认爬取仍正常写库（`SELECT COUNT(*) FROM result_items` 增长）、`expired` 巡检仍工作（`item_recheck_service` 无异常日志）。

## 完成定义（DoD）

- `prd.md` 的 AC1-AC7 全部有据可查（测试输出 / SQL 输出 / GUI 截图三选一，按 AC 要求）。
- 无新增测试失败；新增用例覆盖 AC1/AC2/AC4 与 `expired` 不回归。
- `design.md §3.5` 的四个决策点在代码注释或提交信息里可追溯。
- 生产库备份文件存在且已核验可打开。