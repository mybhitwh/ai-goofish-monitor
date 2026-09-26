# 调研：AI 标签的来源、结果页筛选/屏蔽契约与线上证据

调研日期 2026-09-25，主线 `master`（工作区未提交改动只涉及 `.trellis/`）。所有线上数字来自对 `data/app.sqlite3` 的**只读**查询（`sqlite3.connect("file:...?mode=ro", uri=True)`），未写入任何数据。

## 1. 线上证据（274 行结果数据，4 个结果文件）

复现脚本（只读）：

```python
import json, sqlite3, collections
con = sqlite3.connect("file:/home/myb/code/ai-goofish-monitor/data/app.sqlite3?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute("SELECT result_filename, status, is_recommended, analysis_source, raw_json FROM result_items").fetchall()
act = [r for r in rows if r["status"] == "active"]
tagcount = collections.Counter()
for r in act:
    for t in (json.loads(r["raw_json"]).get("ai_analysis") or {}).get("risk_tags") or []:
        tagcount[str(t)] += 1
print(len(rows), len(act), tagcount.most_common(15))
```

结果：

- 总计 274 行：`iPad_Air_M4_full_data.jsonl` 77 / `iPad_Air_full_data.jsonl` 74 / `iPad_Air_8_full_data.jsonl` 61 / `iPad_Air_11_256_full_data.jsonl` 62。
- `status`：`hidden` 157 / `active` 117（**没有 `expired`**）。`analysis_source` 全为 `ai`。
- `risk_tags` 非空 **273/274**。全量 Top：型号不符 163、容量不符 96、信息缺失 50、商家贩子 43、有条件推荐 43、卖家信用不足 41、卖家信用缺失 38、交易方式受限 9。
- `active` 117 条中 `is_recommended`：`1` → 43 条，`0` → 74 条。即「待整理的明确不符合项」74 条：`iPad_Air_M4` 21 / `iPad_Air_11_256` 21 / `iPad_Air_8` 22 / `iPad_Air` 10。
- `active` 的 `risk_tags` Top：信息缺失 38、**有条件推荐 36**、型号不符 35、卖家信用缺失 24、卖家信用不足 19、商家贩子 18、容量不符 17、交易方式受限 6、信息不透明 4、需确认维修史 4、批量同型号 4、**卖家信用差 4**、同型号密集出货 4、`NEEDS_MANUAL_CHECK` 3、未验证身份 3。
- `item_annotations` 87 行；用户标签分布：**`不符合我的要求` 84**、已经卖出了 3、不是目标型号 1、不包邮 1；带 note 的 0 行。

两条结论：

1. **词表已经漂移**：同一语义有 `卖家信用缺失`/`卖家信用不足`/`卖家信用差` 三种写法；`商家贩子`/`批量同型号`/`同型号密集出货` 指向同一件事；`信息缺失`/`信息不透明`/`需确认维修史`/`未验证身份`/`NEEDS_MANUAL_CHECK` 也是一族；还有 `有条件推荐` 这种**不是不符原因**的值混在里面。按原样做筛选会得到一列碎标签。
2. **用户已经在为缺少批量/细粒度操作付成本**：157 条逐条屏蔽，84 条统一只打了 `不符合我的要求` —— 因为逐条屏蔽时理由只能手打，用户在"整理"这件事上只能用一个笼统标签兜住。

## 2. AI 输出契约（现状）

- 输出格式在 `prompts/base_prompt.txt:10-46`：`prompt_version`、`is_recommended`、`reason`、`risk_tags: ["string"]`、`criteria_analysis.{model_chip,battery_health,condition,history,seller_type,shipping,seller_credit}`（每项 `{status, comment, evidence}`），`seller_type` 另有 `persona` 与 `analysis_details`。`value_score` / `value_summary` 由 `src/ai_message_builder.py:19-24` 追加。
- 校验在 `src/ai_handler.py:240-276`：只要求 5 个顶层键存在、`risk_tags` 是 list，**没有枚举白名单**——所以词表漂移不会被拦住。
- `criteria_analysis.*.status` **不能作为确定性派生源**：线上实际出现 `FAIL`/`fail`、`pass`/`PASS`、`NEEDS_MANUAL_CHECK`/`needs_manual_check`，还有 `ACCEPTABLE`/`GOOD`/`WARNING`/`PERSONAL_PLAYER`/`PARTIAL_DEFECT` 等词表外取值。`risk_tags` 才是稳定且已被模型持续填写的字段。
- 分析失败记录（与 `09-25-fix-ai-moderation-rejected-analysis` 相关）落成 `{analysis_source:"ai", is_recommended:false, reason:"AI分析异常: ...", error:...}`（`src/services/item_analysis_dispatcher.py:112-142`）。**这类记录绝不能被打上"不符原因"标签**。
- 提示词文件是 **CRLF**：`prompts/base_prompt.txt`（47 CRLF，已跟踪）、`prompts/macbook_criteria.txt`（已跟踪）；`prompts/ipad_air_m4_criteria.txt` 未跟踪但线上在用。`prompts/apple_watch_s10_criteria.txt` 是 41 字节的占位（内容含字面 `\n`），与本任务无关。

## 3. 存储与读路径（决定了"要不要加列"）

- `result_items` 表结构 `src/infrastructure/persistence/sqlite_connection.py:51-72`：`id, result_filename, keyword, task_name, crawl_time, publish_time, price, price_display, item_id, title, link, link_unique_key, seller_nickname, is_recommended, analysis_source, keyword_hit_count, status, raw_json, UNIQUE(result_filename, link_unique_key)`。AI 的全部输出只在 `raw_json` 里。
- 写入 `src/services/result_storage_service.py:168-209` 是 **`INSERT OR IGNORE`**：已存在的 `link_unique_key` 不会再更新，所以"加列 + 写入时填充"对**存量 274 行无效**，需要额外回填；而回填拿不到新字段（老记录里没有 `tags`）。
- 读路径 `src/services/result_storage_service.py:115-161`（`_load_filtered_records_from_conn`）：一次性读取该文件所有行 → **逐行 `json.loads(raw_json)`** → `_decorate_record_visibility`（`:94-108`，产出 `_status`/`_hidden_reason`/`_effective_hidden`/`_matched_blacklist_keywords`）→ 在 Python 里做 hidden/blacklist/用户标签/note 过滤 → 再由 `:298-325` 切片分页。`_build_query_conditions`（`:51-67`）只把 `result_filename`/`is_recommended`/`analysis_source` 下推到 SQL。
- 结论：**AI 标签在读路径派生（`_ai_tags`）不需要任何 DDL/迁移/回填，且对存量 274 行立即生效**；代价是每次读取多一次词表归一化（数据量是单文件行数级别，可忽略）。若改成物理列，则要同时解决"存量回填"和"`INSERT OR IGNORE` 不更新"两个问题，收益（SQL 下推 + 索引）在当前规模下为零。
- 既有 status 语义：`active` / `hidden`（人工屏蔽）/ `expired`（复查判定已售/已下架，`src/services/item_recheck_service.py:295-304`）。规则屏蔽（blacklist）不写 status，只在读取时给 `_hidden_reason="rule"`。
- 屏蔽的连带效果（写进确认弹窗的警示语）：hidden 与 rule-hidden 会被排除出列表、insights、CSV 导出、概览统计、**复查（只有 `status='active'` 才复查，`item_recheck_service.py:51-79`）**，并且 `load_processed_link_keys` 返回所有 status（`result_storage_service.py:212-220`），因此被屏蔽的商品不会再被详情抓取。

## 4. 现有 API 与筛选契约（`src/api/routes/results.py`）

| 端点 | 说明 |
| --- | --- |
| `GET /api/results/files` | `:56-59` 结果文件列表 |
| `GET /api/results/files/{filename}` | `:62-73` NDJSON 下载，不分 status |
| `DELETE /api/results/files/{filename}` | `:76-86` 删除整个结果文件 |
| `GET /api/results/used-tags` | `:89-92` 用户标签聚合 `{name,count,last_used_at}`（全局） |
| `GET /api/results/{filename}` | `:95-145` 列表；参数 `page`(≥1)、`limit`(1..100，默认 20)、`recommended_only`、`ai_recommended_only`、`keyword_recommended_only`（三者互斥，否则 400）、`include_hidden`、`sort_by`、`sort_order`、`tags`(逗号分隔，OR)、`has_note`；返回 `{total_items, page, limit, items}`，`total_items` 是**过滤后、切片前**的计数 |
| `GET /api/results/{filename}/insights`、`/export` | `:148-202` 与列表同一套过滤（无分页） |
| `PATCH /api/results/{filename}/items/{item_id}/status` | `:205-229` 单条改 status（`active`/`hidden`/`expired`） |
| `PATCH /api/results/{filename}/items/{item_id}/annotation` | `:232-261` 单条 note/tags |
| `GET/PUT /api/results/{filename}/blacklist-rules` | `:264-281` 关键词/正则规则 |

**没有任何批量端点**；屏蔽只能逐条 PATCH。

## 5. 前端契约（`web-ui/`）

- 视图与路由：`web-ui/src/router/index.ts:37-42`（`/results` → `ResultsView.vue`）。
- 全部逻辑在 `web-ui/src/composables/useResults.ts`：过滤状态 `:56-65`（`ResultFiltersState`：`ai_recommended_only`/`keyword_recommended_only`/`tags`/`has_note`/`include_hidden`/`sort_by`/`sort_order` 等），持久化到 `localStorage['resultFilters']`（`:53-90`、`:475-477`）；单文件请求 `:260-266`；合并视图（`__all__`、`__group_{id}__`，`:12-29`）对每个文件 `page:1, limit:100` 扇出后去重、客户端排序（`:170-191`、`:229-258`）。
- 页面**没有分页 UI**：`page=1, limit=100` 固定在 `useResults.ts:40-41`，后端上限也是 100 —— 可见集合永远只是每文件前 100 条，而 `total_items` 可能是 137 之类。批量操作必须由**服务端按筛选条件选取目标**，不能用前端已加载的 100 条作目标集合；确认弹窗也要显示"其中 N 条未在当前页显示"。
- 筛选条 `web-ui/src/components/results/ResultsFilterBar.vue`：文件/合并视图选择 `:111-128`、排序 `:130-162`、四个复选框 `:166-202`、**用户标签 chip 筛选** `:204-219`（数据来自 `usedTags` prop，`handleToggleTag` `:85-91`）、动作按钮 `:221-249`（刷新/黑名单/导出/删除，后三者在合并视图禁用）。
- 卡片 `web-ui/src/components/results/ResultCard.vue`：只渲染 `item._user_tags`（`:302-317`，含编辑面板 `:337-357`）；AI 面板显示 `is_recommended` 与 `value_score`（`:36-46`、`:214-247`）；屏蔽按钮在图片右上角（hover 显示，`:176-185`），点击后在**卡片内联**展开 5 个预设理由面板（`:273-300`，预设 `:62-68`）。
- 屏蔽链路：`ResultCard.vue:131-147` → `ResultsGrid.vue:56-59` → `ResultsView.vue:95-117` → `useResults.ts:441-455`（`blockItem`：先把理由**并进 `_user_tags`**，再 `PATCH status=hidden`，再整表刷新）；解除屏蔽 `:391-405`（`_hidden_reason` 为 `rule`/`expired` 时不允许，`:49`）。**理由的持久化契约就是用户标签**，后端没有理由字段。
- 类型：`web-ui/src/types/result.d.ts:45-46` 已声明 `risk_tags`（但没有任何组件渲染它）、`:113` `_user_tags`、`:116-120` `UsedTag`。
- 可复用原语：`ui/` 下有 badge / button / card / checkbox / dialog / input / label / select / switch / table / tabs / textarea / toast；**没有** popover / tooltip / dropdown-menu / tags-input（需要时可基于已是依赖的 `reka-ui@^2.7` 封装）。
- i18n：`web-ui/src/i18n/messages/zh-CN.ts:137-241` 与 `en-US.ts:137-241` 结构一致、需手工同步；相关既有键 `results.filters.tagFilterLabel`（zh-CN `:157`）、`results.card.blockReason*`（`:228-239`）。
- 前端无测试框架（`web-ui/package.json` 只有 dev/build/preview）；构建 `cd web-ui && pnpm build`，产物路径由 `tests/test_frontend_build_paths.py:14-33` 钉住。

## 6. 测试现状（后端）

- `tests/integration/test_api_results.py`：`ai_recommended_only`/`keyword_recommended_only` 互斥 400、排序、insights、CSV、黑名单规则对列表/insights/export 的隐藏与其对 NDJSON 下载不生效。
- `tests/integration/test_api_annotations.py:53-177`：注解往返、`_note`/`_user_tags` 装饰、`tags` OR 过滤、`has_note`、空注解删除、used-tags 聚合、屏蔽后注解保留。
- `tests/unit/test_ai_handler_analysis.py`、`tests/unit/test_item_analysis_dispatcher.py`、`tests/unit/test_result_blacklist_service.py`。
- 隔离方式：`tests/conftest.py:108-172` 用 tmp 库起 tasks API；结果相关集成测试靠 `monkeypatch.chdir(tmp_path)` + 写 legacy `jsonl/*.jsonl` 触发 bootstrap（相对路径默认库）。**测试不得触碰仓库 `data/app.sqlite3`**。

## 7. 相邻任务

- `09-25-block-reason-free-tag-popover`（planning）：改造单卡屏蔽流程为自由理由 + 按钮旁浮层，理由契约仍是用户标签。与本任务同改 `ResultCard.vue`/`useResults.ts`，本任务的批量弹窗应复用同一套"理由=标签"语义，并在实现顺序上写清先后。
- `09-25-fix-ai-moderation-rejected-analysis`（planning）：引入分析失败态。本任务的标签派生必须跳过 `error` 记录并与之保持一致。
- `09-25-prefilter-before-ai`（planning）：在 AI 分析前淘汰标题即可判定的不符合项。它不改变本任务的读路径契约（结果页只展示已进入结果集的记录），但会减少未来入库的不符合项数量。
- `09-25-dashboard-task-identity`（planning）：结果文件归属以 task id 为准；本任务的标签派生与 facets 应复用其结果读取路径，避免再造一套记录装载逻辑。