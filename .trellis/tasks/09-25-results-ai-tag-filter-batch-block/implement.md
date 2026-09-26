# 执行计划：结果页 AI 标签筛选与批量屏蔽

依据：`prd.md`（R1–R7）与 `design.md`（契约、权衡、回滚）。本文件是实现阶段的顺序清单、验证命令与评审门。

## 实现前置

- [x] 评审门已过（2026-09-26）：prd.md Decisions 1-4 定稿（OR / 默认写理由标签 / 词表 v1.1 不开放任务级扩展 / 不做只标不屏），design.md 读路径派生方案确认。
- [ ] `task.py start`（状态 → `in_progress`）之后才开始改代码。
- [ ] 环境：Python 一律 `.venv/bin/python`；前端一律 `pnpm`；改动 `web-ui/` 后必须 `pnpm build`。
- [ ] 相邻任务顺序：若 `09-25-block-reason-free-tag-popover` 先落地，本任务的弹窗理由输入直接复用其自由标签录入组件；冲突预期集中在 `ResultCard.vue`。若 `09-25-fix-blocked-item-reappear-merged-view` 先落地，先读它的屏蔽语义结论：S4 的"选取判定"与"状态改写"必须拆开，全局语义只需替换改写层，计数文案改成"按行计（去重前）"。

## S0 只读基线（不改代码）

- [ ] 写 `research/verify_ai_tags_offline.py`：以 `mode=ro` 打开 `data/app.sqlite3`，对每行跑词表归一化（先内联实现，实现后改为 import 服务函数），输出全量与 `active` 的标签分布、14 词条的合并计数与**裸未知标签数**（重点 5 项：`型号不符` / `容量不符` / `卖家信用低` / `电池健康未知` / `描述与实际不符`）；把输出贴进 `research/ai-tags-and-results-filter-map.md` 作为期望值。
- 命令：`.venv/bin/python .trellis/tasks/09-25-results-ai-tag-filter-batch-block/research/verify_ai_tags_offline.py`
- 门：脚本**只读**（`mode=ro`，无 `INSERT`/`UPDATE`/`DELETE`）；期望值（2026-09-26 真值，全量）：型号不符 167、容量不符 102、卖家信用低 96、电池健康未知 10、描述与实际不符 13；active 子集（随用户屏蔽操作变动，取 ≥）：型号不符 ≥ 34、容量不符 ≥ 16、卖家信用低 ≥ 48、电池健康未知 ≥ 7、描述与实际不符 ≥ 7；**裸未知标签数 = 0**。

## S1 标签服务与单测（R1/R2 的服务端部分）

- [ ] 新增 `src/services/ai_tag_service.py`：`CANONICAL_AI_TAGS`、`AI_TAG_SYNONYMS`、`normalize_ai_tags()`、`derive_ai_tags()`（派生链与顺序严格按 `design.md` §2.2，`error` 判断在兜底之前）。
- [ ] 新增 `tests/unit/test_ai_tag_service.py`，覆盖 `design.md` §8 的单测清单（近义映射逐条、去重顺序、上限、未知标签保留、`有条件推荐` 丢弃、`error` 记录、criteria 回退大小写不敏感、兜底、推荐项空）。
- 命令：`.venv/bin/python -m pytest tests/unit/test_ai_tag_service.py -s`
- 门：单测全绿；不得改动 `src/ai_handler.py` 的既有校验契约。

## S2 读路径装饰 `_ai_tags`（R2）

- [ ] `src/services/result_storage_service.py`：在 `_load_filtered_records_from_conn` 的装饰段加 `_ai_tags = derive_ai_tags(record)`（与 `_note`/`_user_tags` 同级，恒为 list）。
- [ ] 补集成断言：`GET /api/results/{filename}` 每条的 `_ai_tags` 存在且为 list；含 `error` 的记录 `_ai_tags` 为空。
- 命令：`.venv/bin/python -m pytest tests/integration/test_api_results.py tests/integration/test_api_annotations.py -s`
- 门：既有集成测试无回归；`data/app.sqlite3` 未被写入（测试用 tmp cwd）。

## S3 筛选参数与 facets（R3）

- [ ] 抽出可复用的过滤判定函数（列表 / insights / export / facets / 批量共用一套），避免复制粘贴过滤逻辑（对照 `code-reuse-thinking-guide`）。
- [ ] 列表 / insights / export 增加 `ai_tags` 参数：逗号分隔、trim 后丢弃空项、OR 语义、与既有条件 AND。
- [ ] 新增 `GET /api/results/ai-tags`，**声明在 `GET /api/results/{filename}` 之前**（否则被 `{filename}` 吃掉）；响应 `{"tags":[{"name","count"}]}`，count 降序、name 升序；支持 `filename` 与 `include_hidden`。
- [ ] 新增 `tests/integration/test_api_results_ai_tags.py`（`design.md` §8 清单）。
- 命令：`.venv/bin/python -m pytest tests/integration/test_api_results_ai_tags.py tests/integration/test_api_results.py -s`
- 门：facets 计数与列表口径一致（同一条记录多标签各计一次）；`/api/results/ai-tags` 返回 JSON 而不是被当成文件名。

## S4 批量改状态端点（R4）

- [ ] `src/services/result_storage_service.py` 增加批量函数：按筛选选取全部匹配行 + 状态约束（屏蔽只动 `active`、恢复只动 `hidden`、`expired` 不可达）→ 单事务 `UPDATE ... WHERE link_unique_key IN (...)`；`reason_tags` 合并进 `item_annotations`（trim/去重/≤5 个/每个 ≤24 字）。路由层沿用 `asyncio.to_thread` 形态。
- [ ] `src/api/routes/results.py` 增加 `POST /api/results/{filename}/items/batch-status`：`status` ∈ {hidden, active}；`dry_run`；护栏（无收窄条件 400、推荐筛选互斥 400、未知文件 200/affected=0）。
- [ ] 新增 `tests/integration/test_api_results_batch_status.py`（护栏、dry_run=affected=列表 total_items、状态机、理由合并、第二次调用 affected=0、多文件隔离）。
- 命令：`.venv/bin/python -m pytest tests/integration/test_api_results_batch_status.py -s`
- 门：`expired` 行在任何用例中都不被改动；`dry_run=true` 时数据库零写入（用例断言 status 未变）。

## S5 提示词词表（R1 的模型侧）

- [ ] 改 `prompts/base_prompt.txt`：把 `risk_tags` 说明改为固定词表 + 规则（只输出不符原因、最多 5 个、无适用标签用 `其他不符要求`、`有条件推荐` 不得出现）。
- [ ] **保留 CRLF**（文件当前 47 个 CRLF 换行）；不重排段落、不动其它字段。
- 命令：`file prompts/base_prompt.txt`（应仍为 `with CRLF line terminators`）；`git diff --stat prompts/base_prompt.txt`；`git diff prompts/base_prompt.txt | cat -A | grep -c '\^M\$'` 抽查行尾
- 门：diff 只落在 `risk_tags` 相关段落；`git diff --check` 无空白错误。

## S6 前端（R5/R6）

顺序：类型与 API 客户端 → composable → 视图与组件 → i18n → 构建。

- [ ] `web-ui/src/types/result.d.ts`：`_ai_tags?: string[]`、facet 类型、批量请求/响应类型。
- [ ] `web-ui/src/api/results.ts`：`ai_tags` 参数、`fetchAiTagFacets()`、`batchUpdateStatus()`。
- [ ] `web-ui/src/composables/useResults.ts`：`aiTags` 筛选状态（旧 `localStorage['resultFilters']` 缺键按 `[]` 补齐）、facets 状态与刷新时机（初次/刷新/批量后/websocket `results_updated`）、`batchUpdateStatus(status)` 合并视图逐文件扇出并汇总、部分失败上报。
- [ ] 新组件 `web-ui/src/components/results/BatchStatusDialog.vue`（基于 `ui/dialog`）：条数、"未在当前页显示 N 条"、后果说明、理由标签编辑器（默认填选中的 AI 标签，可增删）。
- [ ] `ResultsFilterBar.vue`：AI 标签 facet 区（`名称 (计数)`，多选，空则不渲染）+ 批量按钮（`include_hidden` 决定屏蔽/恢复，计数取 `total_items`，禁用规则）。
- [ ] `ResultCard.vue`：`_ai_tags` 徽章（最多 4 个 + `+N`，可点击触发筛选，`title` 提示"AI 判定标签"），与用户标签区分。
- [ ] `ResultsView.vue` / `ResultsGrid.vue`：接线 props/events。
- [ ] `web-ui/src/i18n/messages/{zh-CN,en-US}.ts`：`results.filters.aiTag*`、`results.card.aiTagBadge`、`results.batch.*`，两文件键位一致。
- 命令：`cd web-ui && pnpm build`
- 门：构建通过、`dist/` 更新；`tests/test_frontend_build_paths.py` 通过；手工清单（`design.md` §8）逐条走一遍并在 PR/提交信息里记录结果。

## S7 全量回归与只读线上验证

- [ ] `.venv/bin/python -m pytest tests/ -s` → 与基线对比（130 passed / 3 failed / 3 skipped，3 个失败为存量：`test_frontend_build_paths`、`test_task_group.py::test_group_update_partial_apply`、`test_save_to_jsonl`）。
- [ ] 跑 `research/verify_ai_tags_offline.py`，与 S0 的期望值逐项对齐（5 项重点词条 + 全 14 词条计数 + **裸未知标签数 = 0**）。
- [ ] 确认 `git status` 中没有 `data/`、`state/`、`.env` 的改动（AGENTS.md 明令禁止）。

## S8 收尾

- [ ] `trellis-update-spec`：把「AI 标签词表 + 读路径派生契约 + 批量状态端点护栏」沉淀到 `.trellis/spec/backend/`（当前是脚手架，按真实约定填）。
- [ ] 提交（中文 Conventional Commits，按层拆分：`feat(results): AI 标签筛选` → `feat(results): 批量屏蔽/恢复` → `feat(prompts): risk_tags 固定词表` → `feat(web-ui): 标签筛选与批量操作`）。
- [ ] 部署不由本任务执行：是否在 u12 重启 `goofish.service` 由用户决定；当前任务组处于风控冷却（禁用），提示词变更不会立即影响线上输出。

## 回滚点

| 阶段 | 回滚方式 |
| --- | --- |
| S1–S4（后端） | 逐提交 `git revert`；无 schema 变更，旧代码读同一份数据 |
| S5（提示词） | `git revert`；只影响未来分析，存量记录不受影响 |
| S6（前端） | `git revert` + `pnpm build` 重新生成 `dist/`；旧前端与含新端点的后端可共存 |
| 数据（误批量屏蔽） | 「包含已屏蔽 + 同筛选」批量恢复原路还原；必要时用 `data/backups/` 快照（运维层，需用户执行） |