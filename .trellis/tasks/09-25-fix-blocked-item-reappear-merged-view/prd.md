# 结果页屏蔽失效：合并视图下已屏蔽商品复现

## Goal

结果页「任务组合并视图」里屏蔽过的商品会再次出现，用户被迫对同一商品反复屏蔽。目标：**一次屏蔽对同一商品在所有任务、所有结果视图下都生效**，且屏蔽 / 取消屏蔽后「数据库状态、各视图显示、筛选语义」三者一致；不因后续新增任务或换关键词写法而失效。

## 现象与复现

环境（2026-09-25 u12 生产库只读核对）：

- 任务组 `iPad Air 8`（`task_groups.id=0`）含 4 个任务，keyword 分别为 `iPad Air M4` / `iPad Air` / `iPad Air 8` / `iPad Air 11 256` → 4 个结果文件。
- 结果页默认进入该组的合并视图（`__group_0__`），4 个文件被并行拉取、按商品ID去重后合并展示。

复现步骤：

1. 结果页选择任务组「iPad Air 8」合并视图；
2. 对某商品点「屏蔽」（或「屏蔽 + 记录理由」），卡片消失；
3. 刷新结果、切换视图再切回、或等下一次爬取后 —— 同一商品（同标题、同价格、同链接）重新出现；
4. 再屏蔽一次，换成另一个任务的文件为「活跃副本」，如此往复，直到该商品在所有文件里都被屏蔽。

生产库实测（用户截图中的卡片）：

| item_id | 结果文件 | status | 该文件最新爬取时间 |
| --- | --- | --- | --- |
| 1086388785081 | `iPad_Air_full_data.jsonl` | hidden | 2026-09-25T21:09:53 |
| 1086388785081 | `iPad_Air_M4_full_data.jsonl` | hidden | 2026-09-25T21:09:52 |
| 1086388785081 | `iPad_Air_11_256_full_data.jsonl` | **active** | 2026-09-25T21:09:44 |

再出现的那张卡片自身链接携带 `referPageArgs=iPad Air 11 256`，正是上面仍为 active 的那个文件 —— 说明「复现的那张卡」就是另一个任务文件的活跃副本。

同类数据规模（同一库）：同一 `item_id` 出现在 ≥2 个结果文件的商品有 10+ 个（最多 4 个文件）；`hidden` 在一个文件、`active` 在另一个文件的 (商品, 文件对) 有 20+ 条。全部 `hidden` 行 145 条，`active` 行 129 条。

## Root Cause（结论）

**「屏蔽」按结果文件（=任务）存储，而合并视图按商品聚合展示。** 用户一次屏蔽只写进「当前展示副本所属的那一个文件」；同一商品在其它任务文件里的副本仍为 `active`，合并去重（按商品ID）后由这些活跃副本重新上屏。这与既有「备注 / 标签」的存储契约相反 —— 标注按 `link_unique_key` 全局存储（`item_annotation_service` 模块注释明确写了「与某次爬取的结果文件无关，重爬不丢」），所以标签不丢、屏蔽却丢。

代码链路（`fe216a5 feat(results): 合并视图支持隐藏/恢复商品` 引入合并视图下的屏蔽）：

1. 写入按文件：`PATCH /api/results/{filename}/items/{item_id}/status` → `src/services/result_storage_service.py:442` `UPDATE result_items SET status=? WHERE result_filename=? AND item_id=?`（唯一键为 `UNIQUE(result_filename, link_unique_key)`）。
2. 读取按文件过滤：`_decorate_record_visibility` / `_is_record_visible`（`result_storage_service.py:94-113`），`include_hidden=false` 时丢弃本文件的隐藏行。
3. 合并视图跨文件聚合 + 按商品ID去重：`web-ui/src/composables/useResults.ts:193-258`（`mergeAndDedupe` + `tagSourceFile`）。
4. 屏蔽动作按 `item._source_file` 只路由到一个文件：`useResults.ts:391-405`（`toggleItemBlock`）、`:441-455`（`blockItem`）。

## Requirements

### R1 (P0) 屏蔽对商品全局生效

对同一商品（合并视图认定的同一张卡）执行屏蔽后，该商品在所有结果文件、所有视图（组内合并 / 单任务 / 历史全量合并）下的展示都不再出现（`include_hidden=false` 时），不依赖用户重复点击。

### R2 (P0) 取消屏蔽同样商品全局生效

取消屏蔽后，该商品在各文件、各视图恢复可见；不应出现「A 文件已恢复、B 文件仍隐藏」的分裂状态。

### R3 (P0) 屏蔽语义决定后，存量数据要一致

现存「A 文件 hidden、B 文件 active」的商品必须收敛到同一状态（收敛到「屏蔽」，即尊重用户已有的屏蔽动作），收敛过程幂等、可重复执行、失败不损坏数据。

### R4 (P0) 失效边界：后续新增任务 / 新关键词写法的结果文件

新增一个任务（keyword 换写法）会让同一商品多出一个新的结果文件；此前已屏蔽的商品在新文件里不得重新出现。这是当前实现无法满足、也是修复方案必须覆盖的场景（用户正是在用「换关键词写法开多个任务」的方式扩大搜索面）。

### R5 (P1) 既有筛选与状态语义不变

- `include_hidden=true` 仍能列出被屏蔽商品，并在那里取消屏蔽；
- 黑名单规则（`result_blacklist_rules`，按文件，`_hidden_reason='rule'`）行为与作用域不变；
- `expired`（`item_recheck_service` 自动写入）语义与作用域不变（按文件），且判定来源不变；
- 排序、分页、导出、价格洞察（insights）对「可见商品」的口径保持现状（即隐藏商品不参与）。

### R6 (P1) 回归与验收

后端新增覆盖「同一商品跨文件屏蔽/取消屏蔽」的自动化用例；不劣化现有测试基线；若前端有改动则 `cd web-ui && pnpm build` 通过。

## Acceptance Criteria

- [ ] **AC1 主场景闭环**：在组 `iPad Air 8` 合并视图屏蔽某商品后，该商品在合并视图、4 个单任务视图、`include_hidden=false` 的接口返回中全部消失；只读 SQL `SELECT result_filename, status FROM result_items WHERE item_id='<id>'` 不再出现 active 行。
- [ ] **AC2 取消屏蔽**：在任一视图对上述商品取消屏蔽后，各文件、各视图都恢复可见（同样用只读 SQL 核对）。
- [ ] **AC3 存量收敛**：收敛脚本/迁移执行后，只读 SQL `SELECT COUNT(*) FROM result_items a JOIN result_items b ON a.link_unique_key=b.link_unique_key AND a.result_filename<>b.result_filename WHERE a.status='hidden' AND b.status='active'` 返回 0；重复执行结果不变。
- [ ] **AC4 新文件不再复现（R4）**：为同一商品造一个新的结果文件（新 keyword），已屏蔽商品在其中仍不可见（`include_hidden=false`）。
- [ ] **AC5 语义不退化（R5）**：`include_hidden=true` 可见并可在该处恢复屏蔽；黑名单规则命中（`_hidden_reason='rule'`）与 `expired` 的既有测试仍通过；insights/导出口径不变。
- [ ] **AC6 测试与构建**：新增用例覆盖 AC1/AC2/AC4；`.venv/bin/python -m pytest tests/ -s` 不低于基线（136 collected / 130 passed / 3 failed / 3 skipped，3 个失败为存量：`test_frontend_build_paths`、`tests/unit/test_task_group.py::test_group_update_partial_apply`、`test_save_to_jsonl`）；前端若有改动，`cd web-ui && pnpm build` 通过。
- [ ] **AC7 生产核验留痕**：修复后在生产库上以只读方式核对 AC1/AC3，并在任务 `check.jsonl` / 完成说明里记录 SQL 与结果。

## Non-Goals（本任务不做）

- 不做「按任务屏蔽」的可选开关 / 每任务独立屏蔽状态（本任务采用商品级语义；若评审改判为按任务语义，修法不同，见 `design.md` 的方案 B/C）。
- 不改黑名单规则的作用域（仍是每个结果文件一套关键词）。
- 不改爬取链路的「重复商品分析 / 通知」行为：被屏蔽商品若被另一个任务重新爬到，仍会被该任务重新分析并可能推送通知（当前所有代码路径都不读 `status`）；这是相邻问题，另行立项。
- 不改屏蔽理由的录入交互（见 `09-25-block-reason-free-tag-popover`），本任务只保证理由标签与屏蔽状态一致地作用于商品全局。
- 不改 `expired` 的自动判定来源与作用域。

## Decisions（2026-09-26 评审定稿）

1. **屏蔽语义 = 商品级（跨任务生效）**。任务级语义被否决：合并视图按商品聚合展示，屏蔽必须与「备注/标签」同一契约（`item_annotation_service` 按 `link_unique_key` 全局存储）；任务级会让「屏蔽后仍出现」无法向用户解释，且用户的实际用法是「换关键词写法开多个任务扩大搜索面」，任务级意味着每开一个新任务都要重新屏蔽。将来若确实需要「某任务不看、别的任务看」，应在商品级之上加 per-task 例外。
2. **`expired` 保持文件级**（不商品级）：它是 recheck 巡检的任务视角结论，商品级化会把一次复核误判放大到所有任务；商品真下架时各任务各自的复核会独立得出同一结论。
3. **存量分裂数据收敛为「已屏蔽」**：尊重用户已有的屏蔽动作（hidden 优先）；迁移前必须备份 `data/app.sqlite3` 并打印受影响清单。只读核对显示该数字随用户在页面上的操作实时变动（2026-09-26 同日先后读到 21 / 13 / 22 条），**实施时以迁移前实测为准**。

以上结论即 `design.md` 方案 A 与 §3.5 决策表的口径，`implement.md` 阶段 0 的门禁据此执行，不再等待评审。