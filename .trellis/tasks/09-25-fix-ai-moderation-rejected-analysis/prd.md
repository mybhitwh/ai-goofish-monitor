# 修复 AI 分析被平台审核拒绝：入参瘦身、降级重试与失败态区分

## Goal

让「平台审核拒绝」这种基础设施故障不再伪装成商品判定。当百炼（DashScope）输入审核对一次 AI 分析请求返回 `data_inspection_failed` 时，系统应当：先把图片入参压到不会频繁触发审核的量级；触发时按阶梯降级重试；降级全部失败时如实标记「分析未完成」，而不是落一条 `is_recommended=false` 让前端显示「不建议购买 / AI Match 0%」。

用户价值：监控机器人不该把"我这次没看成"报成"这商品不行"。当前行为会让好价商品被静默判死（既丢推荐，也丢通知），而用户完全看不出区别。

## Background

2026-09-25 21:10 生产实例，任务「iPad Air M4 256G 上海包邮或自取」，商品 `1086957165721`（`logs/iPad_Air_M4_256G_1.log:2203`）：

- 4 次尝试全部被拒：`Error code: 400 ... 'code': 'data_inspection_failed'`，最终 `request_id=5c0fc299-e8d2-9285-8bd4-b524e9247acd`（`logs/iPad_Air_M4_256G_1.log:2206/2215/2237/2243`）。
- 落库结果为 `data/app.sqlite3` 的 `result_items.id=274`：`is_recommended=false`、`reason="AI分析异常: <原始异常>"`、`analysis_source="ai"`。
- 前端 `web-ui/src/components/results/ResultCard.vue:38` 对任何 `is_recommended === false` 渲染红色「不建议购买」，`:47` 的 `matchScore = ai?.value_score ?? 0` 因缺 `value_score` 显示 0%——与真实"不建议购买"完全同形。
- 同轮对照：`result_items.id=276`（商品 `1085361274389`，8 张 1440×1920 图）正常返回判定，说明并非该批次整体不可用。

受控重放实验（方法见 `research/dashscope-moderation-evidence.md`，脚本 `research/repro_moderation_bisect.py`）确认：

- 拒绝的是**重型多模态入参**，不是违禁词、也不是某张图：纯文本通过；单张全分辨率图（6.56MB）通过；两张（10.54MB）通过；图片缩到 30%（1.06MB）通过；而 5 张 4368×5824 的 JPEG 变体（6.75 / 7.07 / 7.38 / 7.77 / 14.63MB）全部被拦。
- 判定**不稳定、不可按需复现**：线上被拦 4/4 的那次 payload（5 张 WebP，6.00MB），事后重放 10 次全部通过。
- 声明的 data URL MIME 不影响判定（`image/webp` 与 `image/jpeg` 声明结果一致，2×2 对照 8 次）。
- 38.52MB 时平台返回另一条显式错误：`Multimodal file size is too large`。
- 闲鱼 CDN 的 `-xy_item.jpg` / `~livephoto~` 实际都是 **WebP 字节**，而 `src/ai_handler.py:330` 与 `src/infrastructure/external/ai_client.py:171` 一律拼 `data:image/jpeg;base64,`；图片从不缩放（单图可达 4368×5824 ≈ 25MP，单商品最多 9 张）。

结论：修复目标不是"永不触发审核"（不可控），而是**降低触发概率**（入参瘦身）**+ 触发后仍能完成分析**（阶梯降级）**+ 实在做不了就如实上报**（失败态区分）。

## Requirements

### R1 (P0) 图片入参必须在发送前瘦身并与实际格式一致

当前 `src/ai_handler.py:147-196`（下载原图）、`:325-331`（base64 编码）、`src/infrastructure/external/ai_client.py:123-133` 全程不做尺寸/质量处理，直接发送原始字节并统一声明 `image/jpeg`。

约束：
- 发送前对每张图做本地转码：长边压到配置上限、按 JPEG 质量重编码，产出**声明与实际一致**的 `data:image/jpeg;base64,`。
- 用 Pillow（已是运行时依赖，见 `requirements-runtime.txt`），不新增第三方依赖、不引入外部图像服务。
- 缩放参数（长边上限、质量、单次最多图片数）走配置，默认值在 `design.md` 定稿；改动只作用于 AI 请求入参，不改变磁盘上已下载图片与 `images/` 目录行为。
- `analyze_images=false`（纯文本）路径行为不变。
- 不要改判定相关的 prompt 文本与字段口径。

### R2 (P0) 审核类错误必须走阶梯降级重试，而不是原样重试

`src/ai_handler.py:372-479` 的重试循环对 `messages` 只构造一次，4 次重试发送逐字节相同的 payload（仅 temperature 0.1→0.05），对审核/体积类拒绝必然得到同一结果；`src/services/ai_request_compat.py:78-101` 的既有错误识别函数也不认这两类错误。

约束：
- 新增识别：`data_inspection_failed`（审核拒绝）与 `Multimodal file size is too large`（体积超限）两类错误，判定函数与既有 `is_*_unsupported_error` 风格一致，放在 `src/services/ai_request_compat.py`。
- 命中后按阶梯降级重试，阶梯默认：**缩图重发 → 减少图片数量重发 → 纯文本重发**；每级都要在日志留下降级轨迹（级别、原因、图片张数与 payload 体积）。
- 阶梯耗尽仍失败时，异常语义与今天一致地向上抛出（交由 R3 处理），不得吞掉。
- 其他错误类型的现有重试与 API 模式回退行为不变。

### R3 (P0) 分析失败必须与「不建议购买」区分开

`src/services/item_analysis_dispatcher.py:112-121` 的 `_build_ai_error_result` 把任何异常都落成 `is_recommended=false` + `analysis_source="ai"`，`:123-144` 捕获后照常落库；`web-ui/src/components/results/ResultCard.vue:35-47` 据此渲染成「不建议购买」。

约束：
- 分析未完成时，落库记录必须带可区分的分析状态字段（`ai_analysis` 内新增，如 `analysis_status: "failed"`；`result_items.status` 语义是商品可见性 `active/hidden/expired`，见 `src/services/result_storage_service.py:436`，不得复用）。
- 前端对"分析未完成"渲染独立状态（独立文案与配色），**不显示「不建议购买」、不显示 0% 的 AI Match**；`web-ui/src/types/result.d.ts:36-48` 的 `AiAnalysis` 类型同步扩展。
- 失败项不得触发推荐通知（维持现状），也不得被计入任何"已分析完成"的统计口径。
- 兼容历史记录：缺该字段的旧记录按既有语义处理，不批量改写历史判定。

### R4 (P1) 失败可追溯、可重跑

约束：
- 失败记录保留原始错误文本与最后一次的 `request_id`，并记录最终生效的降级级别（便于区分"审核拒绝""体积超限""其他"）。
- 提供重跑路径（复用既有手动重跑/复检入口，不新造一套调度）：失败项能被重新分析并在成功后覆盖为正常判定。具体入口在 `design.md` 定稿。
- 失败不得永久占用"已分析"语义：复检服务（`src/services/item_recheck_service.py`）当前只判定已售信号，不重跑 AI，这一点需在设计中明确取舍。

### R5 (P1) 回归测试与体积断言

约束：
- 新增单测覆盖：审核错误识别、降级阶梯的次序与每次降级的入参变化、失败态落库字段、前端状态判定（如可测）。既有 AI 相关测试（`tests/unit/test_ai_handler_analysis.py`）不得回归。
- 补一条入参体积断言：给定 N 张超规格图，构建出的请求中每张图长边 ≤ 配置上限、声明格式与实际字节一致、整体体积较修复前下降一个数量级。
- 测试基线不差于 u12 现状（130 passed / 3 failed / 3 skipped）。

### R6 (P1) 参数与重跑规则在设置页面可配置（决策 8）

约束：
- 图片入参（长边上限、JPEG 质量、单次图片数）与失败重跑规则（开关、每条上限、每轮上限、最小间隔）通过 `GET/PUT /api/settings/ai` 暴露，并在设置页「AI」标签页可编辑；写入沿用 `env_manager`（`.env`）+ `_reload_env()`，流水线在调用点用 `os.getenv` 读取（与 `RECHECK_*` 同模式，改后即生效）。
- 参数是**全局配置，不做任务级覆盖**（决策 5）。
- 越界入参返回 400 并给出可读信息（校验在路由层，不留到流水线）。
- 键名、默认值与校验范围见 `design.md` §6.1。

## Acceptance Criteria

- [ ] AC1 给定 5 张 4368×5824（WebP 字节、扩展名 `.jpg`）图片，修复后构建的请求里每张图长边 ≤ 配置上限、字节为真 JPEG、声明 MIME 与实际一致；整体 payload 体积记录前后对比（修复前实测 6.00MB）。
- [ ] AC2 注入 `data_inspection_failed`（mock/openai 客户端打桩）后，流程按「缩图 → 减图 → 纯文本」降级并最终成功返回正常判定；日志中可看到每一级的降级记录。
- [ ] AC3 降级全部失败时，`result_items` 中的记录带 `analysis_status=failed`（或设计定稿的等价字段）与 `error`，前端渲染为「分析未完成」且不出现「不建议购买」与 0% AI Match；`is_recommended` 不再被当作失败结论使用。
- [ ] AC4 `Multimodal file size is too large` 走同一降级路径，不被当作普通异常直接抛出。
- [ ] AC5 正常商品（小尺寸图、图片数在阈值内）的分析行为不变：prompt、判定字段、通知路径与 `analysis_source=ai` 语义均不回归；`keyword` 模式与 `skip_ai_analysis` 行为不变。
- [ ] AC6 `.venv/bin/python -m pytest tests/ -s` 结果不差于基线（130 passed / 3 failed / 3 skipped），新增用例全部通过。
- [ ] AC7 生产库现存同类记录（`result_items.id=274`）有明确处理口径并被执行（保留并标记 failed，或由用户决定重跑），不静默改写为其他判定。
- [ ] AC8 前端改动后 `cd web-ui && pnpm build` 通过，`dist/` 与源码一致（SPA 服务读产物）。

## Out of Scope

- 不改 `prompts/`（`base_prompt.txt`、`ipad_air_m4_criteria.txt` 等）与判定标准、字段口径。
- 不引入外部审核/图像服务，不做并发或调度策略调整（属 `09-25-fix-risk-control-stop-loss` 任务范围）。
- 不触碰运行时状态：`data/`、`state/`、`.env`、`logs/`、`images/`；不重新启用或重跑生产任务（由用户决定）。
- 不重构整个 AI 客户端（`AIClient`）与 Responses API 回退逻辑，仅在本路径做最小改动。
- 不为"降低审核触发率"去做图片内容层面的规避（如裁剪敏感区域、打码）——那是平台策略对抗，不在范围内。

## Key Decisions

1. **范围**：本期做 R1–R3（P0）+ R4/R5（P1）。R1 是概率治理、R2 是可用性兜底、R3 是数据正确性，三者缺一都会让同类故障再次发生；R4/R5 与它们同源，改动可控。
2. **优先级 P1**：不涉及账号风控与请求风暴（那是 P0 的止损失效任务），但会静默产出错误判定——低频（本轮 100+ 商品中 1 件）却直接影响"是否错过好价"，且失败被掩盖，故定 P1 而非 P2。
3. **失败态用 `ai_analysis` 内新字段表达**，不复用 `result_items.status`：后者语义是商品可见性，混用会污染筛选与复检逻辑。
4. **声明 MIME 与实际字节一致**：实测声明不影响审核判定，但错标是潜在拒收风险，且会让日志与排障失真，属低成本顺带修。
5. **不追求"永久不触发审核"**：判定不稳定且平台侧不可控；本任务只保证"触发概率显著下降 + 触发后仍有出路 + 真失败时如实上报"。
6. **实验结论以文件为准**：`research/` 内的证据与复现脚本随任务归档，验收时用它做前后对比，而不是靠对话记忆。
7. **本任务可用子代理**：`.zcode/agents/trellis-implement.md`、`trellis-check.md`、`trellis-research.md` 已随 Trellis 集成入库（前一个任务的 Key Decision 5 已不适用）。

## Decisions（2026-09-26 评审定稿）

1. **缩图参数**：长边上限 1600、JPEG 质量 85、单次最多 5 张（原 9）；阶梯 L1 缩图级 1024/q75、L2 减图级 1024/q75/≤2 张、L3 纯文本。取值对齐唯一「5 张且通过」的实验口径（L1：30% 缩放 ≈ 长边 1747、q85、1.06MB）。**不做任务级可配**（改为全局配置 + 设置页可编辑，见 R6）；若抽查发现图内小字不可判，先升质量到 90，不升长边。
2. **纯文本降级标记**：要标记，但不做成第三种失败态——`analysis_status="ok_text_only"` + 前端「无图分析」徽章，**仍计入推荐与统计**（它是一条可用结论，只是证据强度弱）。
3. **失败态统计口径**：failed 排除在 AI Match 均值与推荐统计之外（沿用 `is_recommended = 1` 的既有条件，不新增统计口径）；**失败项筛选入口不在本任务**，交由 `09-25-results-ai-tag-filter-batch-block` 的筛选条一并实现，两任务在各自"相关任务"里互相登记。
4. **失败重跑**：交复检服务按周期重跑（不在同轮内原地重撞——同一时点大概率撞同一故障，且会拖长轮次、增加请求量）；每条自动重跑上限 1 次、成功后覆盖为正常判定、绝不覆盖已有成功判定；保留手动入口（结果页登记 + 下一轮复检执行，见 `design.md` §5）；**重跑规则在设置页面可配置**（R6）。
5. **生产库现存记录（`result_items.id=274`）**：保留并标记、不静默改写；由周期或手动重跑覆盖为正常判定，并在完成说明里留只读 SQL 前后对照（AC7）。

评审结论已落进 `design.md`（§2 参数、§3 阶梯、§4 失败态与读侧、§5 重跑、§6 设置页）与 `implement.md`（Step 0–7）。