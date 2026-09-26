# 技术设计：屏蔽状态商品级化（消除合并视图复现）

前置阅读：`prd.md`（现象、需求 R1-R6、验收 AC1-AC7）。本文给出根因的证据、候选方案对比、推荐方案的契约与实现边界、存量数据收敛、兼容性与回滚。

## 1. 根因（含证据）

### 1.1 存储维度错配

| 维度 | 备注 / 标签 | 屏蔽状态（现状） |
| --- | --- | --- |
| 存储 | `item_annotations(link_unique_key PRIMARY KEY, note, tags_json)`（`src/services/item_annotation_service.py`） | `result_items.status`，唯一键 `UNIQUE(result_filename, link_unique_key)` |
| 作用域 | **商品级**（跨任务、跨文件、重爬不丢；模块注释明确声明） | **文件级**（=任务级） |
| 写入 | `PATCH /{filename}/items/{item_id}/annotation` → 按 key upsert | `PATCH /{filename}/items/{item_id}/status` → `UPDATE ... WHERE result_filename=? AND item_id=?`（`result_storage_service.py:442-450`） |
| 读取 | `_load_annotation_map_from_conn` 按 key 批量取，无文件边界 | `_load_filtered_records_from_conn` 逐行按 `row["status"]` 过滤（`result_storage_service.py:115-161`） |

结果：标签在别的任务里看得到，屏蔽在别的任务里「消失」——同一商品在 4 个任务文件里可以有 4 个互相独立的状态。

### 1.2 合并视图把「多份状态」压成「一张卡」

`web-ui/src/composables/useResults.ts`：

- `fetchResults()`（`:228-258`）：合并视图（`__group_{id}__` 或 `__all__`）并行拉取目标文件，每条打 `_source_file` 标记后做 `mergeAndDedupe`（`:193-211`，按 `商品ID` 去重）。
- 服务端在 `include_hidden=false` 时已把「本文件里 hidden 的行」剔除，所以去重候选里剩下的都是各文件的 active 副本。
- 屏蔽动作（`fe216a5` 引入）按 `item._source_file` 只写一个文件：`toggleItemBlock`（`:391-405`）、`blockItem`（`:441-455`）。
- 本文件那份被置 hidden → 下次拉取时它从候选里消失 → 同商品的另一文件 active 副本成为唯一候选 → **上屏的那张卡就是它**。用户看到的就是「屏蔽过的商品又出现了」。

### 1.3 生产库实证（2026-09-25，只读）

- 用户截图卡片 `item_id=1086388785081`：`iPad_Air_full_data.jsonl`=hidden、`iPad_Air_M4_full_data.jsonl`=hidden、`iPad_Air_11_256_full_data.jsonl`=**active**；卡片链接自带 `referPageArgs=iPad Air 11 256`，与 active 副本所在文件一致。
- 该商品跨 3 个文件，`link_unique_key` 三处完全相同（`https://www.goofish.com/item?id=1086388785081`）→ **商品级键现成可用**。
- 全局：跨 ≥2 文件重复的 `item_id` 10+ 个（最多 4 文件）；`hidden(A) + active(B)` 的 (商品,文件对) 20+ 条；`hidden` 145 行 / `active` 129 行。
- 任务组 `iPad Air 8`（id=0）正是这 4 个任务所在的组，结果页默认选中它 → 用户日常就在这个合并视图里操作。

### 1.4 为什么这次不能只改前端

即使前端把「同商品其它副本」都写一遍（方案 B/C），只要**新出现一个结果文件**（新任务、换关键词写法 —— 用户正在用的扩面手法：同一组里已有 `iPad Air M4` / `iPad Air` / `iPad Air 8` / `iPad Air 11 256` 四种写法），新文件里的行初始就是 `active`，没有任何机制知道「这个商品早就被屏蔽了」。因此修复必须让「屏蔽」成为**商品级、可被任意文件在读取时感知**的状态。

## 2. 方案对比

| 方案 | 做法 | 能过 R1/R2 | 能过 R4（新文件） | 代价 / 风险 | 结论 |
| --- | --- | --- | --- | --- | --- |
| **A（推荐）** | 新增商品级「屏蔽标记」表，读取时遮蔽；写入同步已有副本 | ✅ | ✅ 读取时遮蔽，与文件何时产生无关 | 一张新表 + 一次幂等迁移 + 读路径一处改动 | 采纳 |
| E | 写入扇出（`UPDATE ... WHERE link_unique_key=?`），不加表 | ✅ | ❌ 新文件仍 active | 最小改动，但留一半洞 | 备选（若评审只要立刻止血） |
| F | 写入扇出 + 插入时继承（`INSERT` 时若该 key 有 hidden 行则插入为 hidden） | ✅ | ✅（依赖每个插入点都遵守） | 两条插入路径（`save_result_record`、`sqlite_bootstrap._insert_result_record`）都要改；将来新增插入点易漏 | 可作为 A 的二道防线，不建议与 A 同做 |
| B | 前端扇出 N 次 PATCH（按同商品的所有来源文件） | ✅ | ❌ | 需要保留每个 item 的全部来源文件；N 次请求；单任务视图屏蔽仍漏 | 拒绝 |
| C | 纯显示层：合并时 `include_hidden=true` 拉取、去重时 hidden 优先、再把任一副本 hidden 的商品整体丢弃 | ⚠️ 仅显示 | ❌ | 数据继续分裂；取消屏蔽语义含糊；单任务视图/insights/导出口径不一致 | 拒绝 |

推荐 **A**：把「是否被用户屏蔽」提升为商品级事实，并在**唯一读取汇聚点**（`_decorate_record_visibility`）生效，这样列表 / 导出 / insights / recheck 可见性全部自动一致，且不受未来新增文件影响。

## 3. 推荐方案 A 的契约与实现边界

### 3.1 数据模型

```sql
CREATE TABLE IF NOT EXISTS item_hidden_marks (
    link_unique_key TEXT PRIMARY KEY,
    updated_at      TEXT NOT NULL
);
```

- 语义：**存在即被用户手动屏蔽**；取消屏蔽 = 删除该行。不做「显式 active」标记，避免三态歧义。
- 不变量（写入路径维护）：手动屏蔽 ⇒ 标记存在 ∧ 该 key 的**已存在**行 `status='hidden'`；取消屏蔽 ⇒ 标记不存在 ∧ 该 key 的行 `status='active'`（`expired` 行一并置 active，理由见 §3.5）。
- 键：`link_unique_key`（与 `item_annotations` 一致，跨文件稳定；生产库已核实同一商品各文件同键）。**不使用 `item_id` 作为首键**：`item_id` 可能为空（走 `hash:` 兜底键的记录），而 `link_unique_key` 恒非空。

### 3.2 读取遮蔽（唯一汇聚点）

`src/services/result_storage_service.py`：

- 新增 `_load_hidden_mark_keys_from_conn(conn, link_unique_keys) -> set[str]`（分块 `IN`，与 `_load_annotation_map_from_conn` 同款）。
- `_load_filtered_records_from_conn`（`:115-161`）在取 annotations 的同一处取 marks，传给 `_decorate_record_visibility`。
- `_decorate_record_visibility(record, status, blacklist_keywords, globally_hidden: bool)`（`:94-108`）改成：

```
hidden_reason:
  status == 'expired'          -> 'expired'      （行级，不变）
  globally_hidden              -> 'manual'       （新增）
  status not in (None,'active')-> 'manual'       （行级，兼容存量/同步写）
  matched blacklist keywords   -> 'rule'         （不变）
_status:
  globally_hidden ? 'hidden' : (status or 'active')      ← 关键
```

`_status` 必须是 `hidden`（而不是行里的 `active`）：前端「取消屏蔽」判据是 `item._status === 'hidden'`（`useResults.ts:398`、`ResultCard.vue`），否则已屏蔽卡片上的按钮会再次执行「屏蔽」。这是本设计里最容易踩的一处。

- `_sort_expression`（`:70-73`）的 `CASE WHEN status='active' THEN 0 ELSE 1 END` 保持不变；因写入同步把已存在行也置 hidden，`include_hidden=true` 下列表里隐藏项仍靠后（与现状一致）。

### 3.3 写入路径

`PATCH /api/results/{filename}/items/{item_id}/status`（`src/api/routes/results.py:220-228`）：

- `status ∈ {hidden, active}`：
  1. 用既有 `get_link_unique_keys_by_item_id(filename, item_id)`（`result_storage_service.py:508`）解析该文件里这条商品的 key；为空 → 保持 404 语义不变；
  2. `hidden` → UPSERT `item_hidden_marks`；`active` → DELETE；
  3. `UPDATE result_items SET status=? WHERE link_unique_key=?`（同步所有已存在文件的副本，含当前文件）；
  4. 返回体 `{"message","status"}` 不变。
- `status == 'expired'`：**保持文件级**（`UPDATE ... WHERE result_filename=? AND item_id=?`），不写标记、不扇出。`item_recheck_service.item:299` 的自动过期行为完全不变。
- 服务层拆分，避免语义混淆：
  - `set_item_hidden_state(item_id_or_key …, hidden: bool)`（商品级，供路由用）
  - `update_item_status(filename, item_id, status)`（文件级，保留给 `expired` 与既有调用）
- 并发：SQLite 串行化 + `busy_timeout` 已有；三条语句放在同一连接/事务里提交。

### 3.4 存量收敛（幂等迁移）

`src/infrastructure/persistence/sqlite_connection.py` 新增 `_migrate_item_hidden_marks(conn)`，与 `_migrate_result_items_status`（`:168-184`）同构，在 `init_schema` 中调用，用 `app_metadata` 键 `migration:item_hidden_marks` 保证只跑一次：

1. 建表（`SCHEMA_STATEMENTS` 里加，`CREATE TABLE IF NOT EXISTS` 双保险）；
2. `INSERT INTO item_hidden_marks(link_unique_key, updated_at) SELECT DISTINCT link_unique_key, ? FROM result_items WHERE status='hidden'`（尊重用户已有屏蔽动作，隐藏优先）；
3. `UPDATE result_items SET status='hidden' WHERE link_unique_key IN (SELECT link_unique_key FROM item_hidden_marks)`（把 `hidden(A) + active(B)` 的 B 收敛，满足 AC3）；
4. 写 marker。

收敛逻辑抽成可调用函数（如 `converge_item_hidden_marks(conn)`）以便单测直接调用，不依赖「清 marker 再 bootstrap」的取巧手法。

### 3.5 明确决策（评审可推翻）

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| 取消屏蔽是否清掉其它文件的 `expired` 行 | 是（一并置 active） | 与现状单文件行为一致（前端取消屏蔽就是 PATCH active）；若商品确实已下架，recheck 会再次标记 expired，不会造成长期错误状态 |
| `expired` 是否商品级 | 否，保持文件级 | 缩小本任务影响面；recheck 是任务视角的巡检 |
| 黑名单规则是否商品级 | 否 | 关键词规则本来就是「任务/结果文件」维度的筛选工具，与手动屏蔽语义不同 |
| 前端是否改动 | 原则上不改 | 修复在后端；`_source_file` 路由保留（写入仍需要一个存在的文件行，且 404 语义不变） |

### 3.6 影响面

- 受益：结果页各视图（合并 / 单任务）、导出、`/{filename}/insights`（`load_visible_result_item_ids`）、`has_note`/标签筛选等所有经 `_load_filtered_records_from_conn` 的读取路径自动一致。
- 性能：每次列表读取多一条分块 `IN` 查询（该文件当页 keys），量级与 annotations 查询相同。
- 数据：`data/app.sqlite3`（生产运行态，**不提交**）；迁移幂等，可重复启动。
- 未覆盖：爬取/通知链路仍不读 `status`（被屏蔽商品可能被另一个任务重新分析并推送）——见 `prd.md` Non-Goals，另行立项。

### 3.7 上线与回滚

- 上线：改动合并后重启生产实例（`sudo systemctl restart goofish`，u12 由用户代跑）即触发 `init_schema` 迁移；**迁移前先备份**：`cp data/app.sqlite3 data/app.sqlite3.bak-<日期>`（或用 sqlite3 `.backup`），备份文件不入库。
- 回滚代码：`git revert` 即可，旧代码不认识标记表（多余表无害）。
- ⚠️ 回滚**不等于**数据回滚：迁移第 3 步把跨文件 active 副本改成了 hidden，代码回滚后这些行仍是 hidden。若要完全还原，需恢复备份库。因此**先备份、再上线**是硬要求。
- 前端无改动则无需 `pnpm build`；若采纳前端可选清理，则必须重建 `dist/`。

## 4. 验证计划

| 层级 | 用例 | 断言 |
| --- | --- | --- |
| 单测 | 标记 upsert/delete + 读取遮蔽（直接调 `_load_filtered_records_from_conn`） | 有标记时 `include_hidden=false` 不返回；`include_hidden=true` 返回且 `_status='hidden'`、`_hidden_reason='manual'` |
| 单测 | `converge_item_hidden_marks` | 造 `hidden(A)+active(B)` → 收敛后两文件皆 hidden；重复执行结果不变（幂等） |
| 集成 | 新增 `tests/integration/test_api_item_status.py`（复用 `test_api_annotations.py` 的 `_make_record` / `_write_jsonl` / `_make_client` 模式，造两个文件含同一 item） | `PATCH A/status hidden` → `GET B`（默认）不含该商品；`GET B?include_hidden=true` 中 `_status='hidden'`；`PATCH A/status active` → `GET B` 恢复可见 |
| 集成 | `expired` 不被商品级化 | `PATCH A/status expired` 只影响 A，B 仍可见 |
| 集成 | 未来文件（AC4） | 屏蔽后再向新文件 `INSERT` 同商品 active 行 → `GET 新文件`（默认）不返回该商品 |
| 基线 | `.venv/bin/python -m pytest tests/ -s` | ≥ 基线：136 collected / 130 passed / 3 failed / 3 skipped（3 个失败为存量：`test_frontend_build_paths`、`test_task_group.py::test_group_update_partial_apply`、`test_save_to_jsonl`） |
| 生产 | 只读 SQL 核对（AC1/AC3） | 目标商品各文件状态一致；`hidden(A)+active(B)` 计数为 0 |

## 5. 剩余风险

1. `_status` 遮蔽语义改动若遗漏，会出现「已屏蔽卡片点了还是屏蔽」（不报错、只是行为错）→ 用集成用例锁住。
2. 迁移第 3 步是一次性数据写入，靠备份兜底（§3.7）。
3. `hash:` 兜底键（`link` 与 `item_id` 都缺失）记录天然无法跨文件识别同一商品；当前生产库此类为 0，且合并视图自身也认不出（去重键同样缺失），故不处理，仅在代码注释里标明边界。