# 实施计划：AI 分析被平台审核拒绝的入参瘦身、阶梯降级与失败态

依据：`prd.md`（R1–R6、AC1–AC8）与 `design.md`（契约、状态机、配置项、测试计划）。
前置：`prd.md` 的 Decisions 已定稿（2026-09-26，四项决策全部拍板）；`task.py start` 后进入实现。改动集中在 `src/ai_handler.py`、`src/services/`、`src/api/routes/settings.py`、`web-ui/`；**无数据库迁移**，不动 `prompts/` 判定口径。

## Step 0 前置核对（读证据 + 基线）

- [ ] 通读 `research/dashscope-moderation-evidence.md`：确认"5 张 25MP 全被拦、缩图或减图后可过、判定不稳定"这三条前提，以及 6.00MB / 1.06MB 两个体积基线。
- [ ] 核对 `design.md` §1 的代码锚点仍成立（逐条 grep）；若已漂移，先更新 design 再动手。
- [ ] 记录基线：`.venv/bin/python -m pytest tests/ -s`（u12 基线 130 passed / 3 failed / 3 skipped）。
- [ ] 记录 `result_items.id=274` 的当前形态（只读 SQL：`raw_json` 的键集合），作为 AC7 的前后对照起点。

## Step 1 图片预处理（R1）

- [ ] 新增 `src/services/ai_image_preprocess.py`：`prepare_image(original_bytes, *, max_long_edge, quality)`，处理链按 `design.md` §2（EXIF → 白底 RGB → thumbnail(不放大) → JPEG optimize）。
- [ ] `src/ai_handler.py` 在构造 `messages` 前接入：读原图字节（≤ `AI_IMAGE_MAX_COUNT` 张，主图优先）→ 逐张预处理 → 缓存 **(原始字节, 派生字节)** 两套，供阶梯各级从原始字节重新派生。
- [ ] `src/infrastructure/external/ai_client.py` 的 Responses 路径复用同一 helper（`design.md` §2 调用点表），消除第二处声明。
- [ ] 单测（`tests/unit/test_ai_image_preprocess.py`）：长边上限、真 JPEG magic bytes、EXIF 方向、RGBA 白底、以及 AC1 的体积断言（5 张 4368×5824 样本 ≤ 2.5MB）。

## Step 2 错误识别（R2 前半）

- [ ] `src/services/ai_request_compat.py` 增 `is_moderation_rejected_error`（`data_inspection_failed`）与 `is_payload_too_large_error`（`Multimodal file size is too large`），风格与既有三个 `is_*_unsupported_error` 一致。
- [ ] 单测：命中/不命中样本（含大小写、嵌套 body 形态、误命中护栏——普通 400 不得被识别成审核拒绝）。

## Step 3 阶梯降级状态机（R2 后半）

- [ ] `src/ai_handler.py` 重试循环改为「级别 × 同级别重试」双层结构：L0 1600/q85/≤5 → L1 1024/q75/≤5 → L2 1024/q75/≤2 → L3 纯文本；审核/体积错误直接升级、不消耗同级别重试预算，并**重建 `messages`**（现状只构造一次，是本任务核心修复点）。
- [ ] 每级切换打印 `[降级] level=N reason=... images=N payload=X.XXMB`。
- [ ] 阶梯耗尽抛 `AIAnalysisUpstreamError(kind, level, request_id)`（`kind ∈ moderation_rejected | payload_too_large | other`）。
- [ ] 确认既有三类 API 兼容错误（json / responses / chat）与普通异常路径行为不变（AC5）。
- [ ] 单测（打桩）：注入 `data_inspection_failed` → 调用序列 L0→L1→L2→L3 且每级入参递减；正常路径调用次数不增加。

## Step 4 失败态落库与读侧（R3）

- [ ] `src/services/item_analysis_dispatcher.py:112-121` 的 `_build_ai_error_result` 改为 failed 形态（`analysis_status="failed"` + `error_kind` + `request_id` + `error`；`is_recommended` 仍写 0 —— 列 `NOT NULL`，`sqlite_connection.py:65`）。
- [ ] 正常判定补 `analysis_status="ok"`；纯文本降级成功补 `analysis_status="ok_text_only"`（决策 6，仍参与推荐）。
- [ ] 前端：`ResultCard.vue:35-47` 按 `analysis_status` 优先渲染（failed → 「分析未完成」，无「不建议购买」、无 0%；`ok_text_only` → 「无图分析」徽章）；`web-ui/src/types/result.d.ts:36-48` 同步类型。
- [ ] `src/services/result_export_service.py:48` 对 failed 输出「未完成」而非「否」。
- [ ] 确认 `is_recommended = 1` 的既有筛选/统计条件未改动（failed 天然不计入推荐与均值）。
- [ ] 单测：阶梯耗尽 → failed 字段齐全；纯文本成功 → `ok_text_only`；旧记录（无 `analysis_status`）按 ok 处理。

## Step 5 重跑（R4 + R6 后端）

- [ ] `src/services/item_recheck_service.py` 在 `run_stale_recheck` 之后追加 `run_ai_retry`：候选 = `analysis_status='failed'` 或 `retry_requested=true`，且 `status='active'`，且距 `crawl_time` ≥ `AI_RETRY_MIN_AGE_MINUTES`；每轮 ≤ `AI_RETRY_MAX_PER_RUN`、最老优先。
- [ ] 复用 `_recheck_one` 已取得的快照（含图片 URL）重跑分析，不再开第二次详情页；沿用 Step 1/3 的入参与阶梯。
- [ ] 新增写回 helper `replace_ai_analysis`（显式 `UPDATE result_items SET raw_json=?, is_recommended=? WHERE result_filename=? AND link_unique_key=?`）——`INSERT OR IGNORE` 不会更新既有行。
- [ ] 护栏：自动重跑每条 ≤ `AI_RETRY_MAX_PER_ITEM`；每次尝试把旧 `error`/`request_id` 追加进 `ai_analysis.retry_history`；成功 → `analysis_status="ok"` 并按新判定写 `is_recommended`。
- [ ] 手动入口：`POST /api/results/{filename}/items/{item_id}/retry-analysis` 只登记 `retry_requested=true`（返回登记结果，不新造调度）；前端失败卡片加「重新分析」按钮。
- [ ] `AISettingsModel` + `GET/PUT /api/settings/ai` 增七个键（`design.md` §6.1 表），越界 400；流水线用 `os.getenv` 调用点读取（与 `RECHECK_*` 同模式，`_reload_env()` 后即生效）。
- [ ] 单测/集成：候选筛选、护栏计数、`replace_ai_analysis` 覆盖既有行、retry_history 保留；settings 往返与越界。

## Step 6 设置页前端（R6）

- [ ] `web-ui/src/views/SettingsView.vue` 的「AI」标签页新增「图片入参」与「失败重跑」两组字段（沿用既有 Input 与保存按钮形态）。
- [ ] `zh-CN` / `en-US` 文案同步（键位结构一致）。

## Step 7 回归、验收与留痕

- [ ] `.venv/bin/python -m pytest tests/ -s` ≥ 基线；`tests/unit/test_ai_handler_analysis.py` 无回归。
- [ ] AC1 离线复算：用 `research/product-1086957165721-snapshot.json` 对比改造前后 payload 体积，结论落到 `research/`。
- [ ] AC7：只读 SQL 复核 `result_items.id=274` 的前后形态，写入 `check.jsonl` / 完成说明；不静默改写历史判定（靠重跑覆盖）。
- [ ] `cd web-ui && pnpm build` 通过，`dist/` 与源码一致。
- [ ] 生产启用与账号恢复**由用户决定**（本任务只提交代码，不重启服务、不重跑生产任务）。

## 验证命令

```bash
.venv/bin/python -m pytest tests/unit/test_ai_image_preprocess.py tests/unit/test_ai_handler_analysis.py -s
.venv/bin/python -m pytest tests/ -s                      # 对比基线 130p/3f/3s
cd web-ui && pnpm build
```

## 回滚点

- 每步一个 commit；`git revert <sha>` 可独立回退。
- 无 DDL、无迁移；新增字段都在 `raw_json` 内，旧代码忽略未知键。
- 设置项删除不影响旧读取（`os.getenv` 有默认值）。
- 唯一的数据写入是 §Step 5 对失败行的 `UPDATE` 与 `retry_history`：回滚代码后这些行仍带新字段，旧代码按 ok 处理不影响展示；如需完全还原，只读核对后按需订正单行。

## DoD

- [ ] R1–R6 全部落地，AC1–AC8 有据可查（含只读 SQL 与离线体积对照）。
- [ ] 决策 5–8 在代码与设置页可追溯（参数默认值、失败态字段、统计口径、重跑规则）。
- [ ] 未触碰 `prompts/` 判定口径、未写 `data/`（业务写路径除外）、未动 `state/` 与 `.env` 的提交状态。