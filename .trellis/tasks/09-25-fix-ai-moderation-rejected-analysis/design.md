# 技术设计：AI 分析被平台审核拒绝的入参瘦身、阶梯降级与失败态

依据：`prd.md`（R1–R6、AC1–AC8）与 `research/dashscope-moderation-evidence.md`（实验证据）。
覆盖 2026-09-26 评审定稿的四项决策（见 `prd.md` 的 Decisions）：缩图参数、纯文本降级标记、失败态统计口径、失败重跑与设置页。

---

## 1. 现状与代码锚点

| 环节 | 现状 | 锚点 |
| --- | --- | --- |
| 图片下载 | CDN 返回 WebP 字节（`.jpg` 后缀），原字节直接落盘 | `src/ai_handler.py:117-129` |
| 编码 | 读文件 → 原字节 base64，无缩放、无转码 | `src/ai_handler.py:228-237` |
| 声明 | 一律拼 `data:image/jpeg;base64,`（与实际字节不符） | `src/ai_handler.py:325-331`、`src/infrastructure/external/ai_client.py:123-137,171` |
| 重试 | `max_retries = 4`，`messages` 在循环外只构造一次，重试仅把 temperature 0.1→0.05（逐字节相同） | `src/ai_handler.py:338`、`:372`、`:376-382` |
| 错误识别 | 只识别 json / responses / chat 三类 API 兼容错误 | `src/services/ai_request_compat.py:78-101` |
| 失败落库 | 任何异常 → `is_recommended=False` + `analysis_source="ai"`，照常入库 | `src/services/item_analysis_dispatcher.py:112-121,123-144` |
| 前端 | `is_recommended === false` → 红色「不建议购买」，`value_score` 缺失显示 0% | `web-ui/src/components/results/ResultCard.vue:35-47` |
| 写库 | `INSERT OR IGNORE`（已存在的 `link_unique_key` 不再更新）；`is_recommended INTEGER NOT NULL` | `src/services/result_storage_service.py:186-202`、`src/infrastructure/persistence/sqlite_connection.py:65` |
| 复检 | 只判已售/下架信号，不重跑 AI；参数按 `os.getenv` 读 | `src/services/item_recheck_service.py:242-245,295-304` |
| 设置 | `GET/PUT /api/settings/ai` → `env_manager`（写 `.env`）+ `_reload_env()`（`load_dotenv(override=True)` 立即生效） | `src/api/routes/settings.py:96-104,255-274`、`:46-49` |
| 设置页 | 「AI」标签页四个字段 | `web-ui/src/views/SettingsView.vue:199-238` |

两个硬约束决定了方案形状：

1. **`is_recommended` 列 NOT NULL**，失败态无法用"置空"表达 → 失败态落在 `raw_json.ai_analysis` 内，读侧改判定优先级（见 §4）。
2. **`INSERT OR IGNORE` 不更新既有行** → 重跑成功后必须显式 `UPDATE`（见 §5.3），不能指望再插一次。

---

## 2. R1 图片入参瘦身

新增模块 `src/services/ai_image_preprocess.py`（纯函数，无 IO 依赖，便于单测）：

```python
prepare_image(original_bytes: bytes, *, max_long_edge: int, quality: int) -> tuple[bytes, int, int]
```

处理链（顺序固定）：

1. `Image.open(BytesIO(original_bytes))` → `ImageOps.exif_transpose`（手机竖拍图不丢方向）；
2. 有 alpha（WebP 常见 RGBA）→ 合成白底后转 `RGB`；
3. 长边超上限才 `thumbnail((max_long_edge, max_long_edge))`（等比、Lanczos）；未超限不放大；
4. `save(BytesIO(), format="JPEG", quality=quality, optimize=True)` → 真 JPEG 字节。

调用点与数据流：

- 在 `src/ai_handler.py` 构造 `messages` 之前插入一步：把 `image_paths` 读成原始字节（上限 `AI_IMAGE_MAX_COUNT` 张，保持原顺序，主图优先），逐张 `prepare_image`，**把结果与原始字节一起缓存在本次调用内**（`list[tuple[bytes, bytes]]`）。
- 阶梯每一级都从**原始字节**重新派生，而不是对上一级的 JPEG 二次压缩——二次压缩会累积伪影且体积不可预测。
- 声明统一为 `data:image/jpeg;base64,`，此时与实际字节一致（顺带修掉 WebP 谎报 jpeg）。
- `ai_client`（Responses API 路径）复用同一 helper，避免两处实现漂移。
- 磁盘行为不变：`images/` 仍存原图，缩放只发生在内存；`analyze_images=false` 路径完全不经过本模块。

参数默认值（决策 5，对齐唯一"5 张且通过"的实验口径 L1）：

| 参数 | 默认 | 依据 |
| --- | --- | --- |
| `AI_IMAGE_MAX_LONG_EDGE` | 1600 | L1 为 30% 缩放（≈长边 1747）+ q85 通过；1600 留余量且贴近 |
| `AI_IMAGE_JPEG_QUALITY` | 85 | 同 L1 |
| `AI_IMAGE_MAX_COUNT` | 5 | 证据中 5 张 25MP 全被拦、1–2 张通过；5 张是保守上限 |

预期效果：4368×5824（25MP/张）→ 1600×2133（3.4MP/张），单张约 0.2–0.5MB，5 张合计约 1–2.5MB（修复前实测 6.00MB），总像素 127MP → 约 17MP。
参数是全局配置，**不做任务级可配**（决策 5）；在设置页可改（§6）。

已知取舍：判据里有需要看图内小字的场景（电池健康截图、型号标签）。若抽查发现小字不可判，第一顺位升 `AI_IMAGE_JPEG_QUALITY` 到 90，而不是升长边——长边是体积的主要来源。

---

## 3. R2 错误识别与阶梯降级

### 3.1 错误识别（`src/services/ai_request_compat.py`）

沿用既有 `is_*_unsupported_error` 风格，新增两个纯函数：

```python
def is_moderation_rejected_error(error: Exception) -> bool   # 匹配 'data_inspection_failed'
def is_payload_too_large_error(error: Exception) -> bool     # 匹配 'Multimodal file size is too large'
```

匹配用 `str(error)` + 容错大小写；不改动既有三个函数的语义。

### 3.2 阶梯（`src/ai_handler.py`）

| 级别 | 图片入参 | 触发方式 |
| --- | --- | --- |
| L0 | 长边 1600 / q85 / ≤5 张 | 首次请求 |
| L1（缩图） | 长边 1024 / q75 / ≤5 张 | 命中审核/体积错误 |
| L2（减图） | 长边 1024 / q75 / ≤2 张 | 再次命中 |
| L3（纯文本） | 0 张 | 再次命中 |

状态机：把"重试预算"与"降级级别"解耦。

- 外层 `for level in range(4)`；内层保留现有重试循环的职责（API 模式回退、`response_format` 参数回退、temperature 调整）——这些"同参重试"仍只在**同一级别内**发生。
- 命中 `is_moderation_rejected_error` / `is_payload_too_large_error` 时**不消耗**同级别重试预算，直接升一级并重建 `messages`（这是本次最关键的修复：现状 `messages` 只构造一次）。
- 每级切换写一行日志：`[降级] level=1 reason=data_inspection_failed images=5 payload=1.83MB`。
- L3 仍失败 → 抛新异常 `AIAnalysisUpstreamError(kind, level, request_id)`；`kind ∈ {moderation_rejected, payload_too_large, other}`，`request_id` 从异常文本里抓（`Error code: 400 ... 'request_id': '...'`，抓不到则 None）。
- 既有三类 API 兼容错误与普通异常的行为不变（普通异常仍在同一级别内重试 4 次后按现状抛出，只是最终由 §4 落成 failed 态）。

单商品最坏情况：4 次 AI 调用（L0–L3 各一次）；同级别重试仍受原 `max_retries` 约束。

---

## 4. R3 失败态与读侧口径

### 4.1 落库字段（`raw_json.ai_analysis`）

```jsonc
{
  "analysis_source": "ai",
  "analysis_status": "ok",            // ok | ok_text_only | failed（缺字段的历史记录按 ok 处理）
  "is_recommended": false,            // 列 NOT NULL，仍写 0，但不再被当作"判定"
  "degrade_level": 0,                 // 最终生效级别 0..3
  "error_kind": null,                 // failed 时：moderation_rejected | payload_too_large | other
  "request_id": null,                 // failed 时尽力保留，便于追溯
  "auto_retry_count": 0,              // §5 自动重跑计数（护栏）
  "retry_requested": false,           // §5 手动重跑登记位
  "error": "..."                      // 仅 failed：原始异常文本
}
```

- 正常判定：`analysis_status="ok"`、`degrade_level` 记录本轮实际级别；
- 纯文本降级成功（决策 6）：`analysis_status="ok_text_only"`，**仍计入推荐与统计**，但前端显示小徽章「无图分析」提示证据强度；
- 失败（决策 7）：`analysis_status="failed"` + `error_kind` + `request_id` + `error`。

`src/services/item_analysis_dispatcher.py` 的 `_build_ai_error_result`（`:112-121`）改为构造上述 failed 形态；`:123-144` 的捕获与入库路径保留。`item_analysis_dispatcher.py:168` 的通知门（`if not analysis_result.get("is_recommended")`）行为不变——失败项不通知。

### 4.2 读侧改动（必须逐项落实，否则失败态不可见或又伪装成判定）

| 位置 | 改动 |
| --- | --- |
| `web-ui/src/components/results/ResultCard.vue:35-47` | 先判 `ai.analysis_status === 'failed'` → 渲染「分析未完成」（中性/琥珀色），不显示「不建议购买」、不显示 0%；再判 `ok_text_only` → 加「无图分析」徽章；其余走现状 |
| `web-ui/src/types/result.d.ts:36-48` | `AiAnalysis` 增 `analysis_status` / `degrade_level` / `error_kind` / `request_id` |
| `src/services/result_export_service.py:48` | 导出列对 failed 输出「未完成」，不再输出「否」 |
| 推荐筛选与统计 | `is_recommended = 1` 的既有条件（`result_storage_service.py:60-64`）不变 → failed 行天然不计入推荐列表、AI Match 均值与概览统计；不在本任务引入新统计口径 |
| 失败项筛选入口 | **不在本任务**做（决策 7）：交给 `09-25-results-ai-tag-filter-batch-block` 的筛选条一并实现，两边在各自"相关任务"里互相登记 |

历史兼容：缺 `analysis_status` 的旧记录按 ok 处理，不批量改写（AC7）。

### 4.3 生产库现存记录（`result_items.id=274`）

口径（决策 7/8）：**保留并标记，不静默改写**。实现后该行仍为旧形态，因缺 `analysis_status` 会被当作 ok——因此不作为"已完成标记"的样本，而是：
- 由 §5 的周期性重跑或手动重跑覆盖为正常判定（重跑成功即写入新 `raw_json`）；
- 在任务完成说明里用只读 SQL 记录它的前后状态，作为 AC7 的留痕。

---

## 5. R4 重跑

### 5.1 触发面（决策 8）

| 来源 | 机制 |
| --- | --- |
| 周期重跑 | 扫描收尾的复检环节（`run_stale_recheck` 之后）追加 `run_ai_retry`，每轮最多 `AI_RETRY_MAX_PER_RUN`（默认 5）条、最老优先 |
| 手动重跑 | 结果页失败卡片提供「重新分析」按钮 → `POST /api/results/{filename}/items/{item_id}/retry-analysis` 只做**登记**（`retry_requested=true` 写回 raw_json），由下一轮复检执行；不新造调度器 |

候选条件：`analysis_status IN ('failed')` 或 `retry_requested=true`，且 `status='active'`（不重跑已屏蔽/已售），且 `crawl_time` 距今 ≥ `AI_RETRY_MIN_AGE_MINUTES`（默认 30，避免失败后立刻原地重撞）。

### 5.2 执行

复用 `item_recheck_service` 已建立的详情页访问路径：`_recheck_one` 拿到快照（含图片 URL）后，若该商品是重跑候选，则把图片交给分析入口重跑一次 AI（无需二次开页）。重跑沿用 §2/§3 的入参与阶梯。

### 5.3 写回（`INSERT OR IGNORE` 的坑）

成功重跑必须显式 `UPDATE result_items SET raw_json=?, is_recommended=? WHERE result_filename=? AND link_unique_key=?`（新增 helper，形如 `replace_ai_analysis`）。同时：

- `auto_retry_count` 或 `retry_requested` 在每次尝试后更新，护栏生效（自动重跑每条上限 `AI_RETRY_MAX_PER_ITEM`，默认 1；手动登记不受此限，但清位后需重新登记）；
- 每次重跑把上一版 `error`/`request_id` 追加进 `ai_analysis.retry_history`（审计，不丢证据）；
- 重跑成功 → `analysis_status="ok"`，`is_recommended` 按新判定写 0/1，通知路径按既有规则（`is_recommended=True` 才通知）。

---

## 6. R6 配置与设置页（决策 8）

### 6.1 参数与校验

| 键 | 默认 | 校验范围 |
| --- | --- | --- |
| `AI_IMAGE_MAX_LONG_EDGE` | 1600 | 512–4096 |
| `AI_IMAGE_JPEG_QUALITY` | 85 | 40–95 |
| `AI_IMAGE_MAX_COUNT` | 5 | 1–9 |
| `AI_RETRY_ENABLED` | true | bool |
| `AI_RETRY_MAX_PER_ITEM` | 1 | 0–3 |
| `AI_RETRY_MAX_PER_RUN` | 5 | 0–50 |
| `AI_RETRY_MIN_AGE_MINUTES` | 30 | 0–1440 |

### 6.2 后端

- `AISettingsModel`（`src/api/routes/settings.py:96-104`）增上述字段；`GET /api/settings/ai`（`:255-264`）返回现值并做类型转换；`PUT`（`:265-274`）写入 `env_manager.update_values()` 后调用既有 `_reload_env()`（`load_dotenv(override=True)`，无需重启）。
- 流水线侧一律 `os.getenv` 在调用点读取（与 `RECHECK_*` 同模式），保证改完即生效。
- 数值越界 → 400 并给出可读信息（校验在路由层做，不留到流水线）。

### 6.3 前端

- `web-ui/src/views/SettingsView.vue` 的「AI」标签页（`:199-238`）新增两组字段：「图片入参」（长边 / 质量 / 张数）与「失败重跑」（开关 / 每条上限 / 每轮上限 / 最小间隔），沿用既有 `Input` 组件与保存按钮形态；
- `zh-CN` / `en-US` 文案同步（沿用键位结构）；
- 改动后 `cd web-ui && pnpm build` 并确认 `dist/` 与源码一致。

---

## 7. 测试计划

| 层级 | 用例 | 断言 |
| --- | --- | --- |
| 单测 | `ai_image_preprocess` | 长边 ≤ 上限；输出为真 JPEG（magic bytes）；声明格式与实际一致；给定 5 张 4368×5824 样本，总体积 ≤ 2.5MB（AC1）；EXIF 方向保留；RGBA → RGB 白底 |
| 单测 | `ai_request_compat` 两个识别函数 | 命中/不命中样本（含大小写与包装形态） |
| 单测 | 阶梯状态机（打桩） | 注入 `data_inspection_failed` → 序列为 L0→L1→L2→L3；每级入参（张数/长边/体积）递减；每级有降级日志；正常路径不多发调用（AC2/AC5） |
| 单测 | 失败态落库 | 阶梯耗尽 → `analysis_status=failed` + `error_kind` + `request_id`；`is_recommended` 仍写 0（列约束）；纯文本成功 → `ok_text_only` |
| 单测 | 重跑写回 | `replace_ai_analysis` 覆盖既有行；`auto_retry_count` 上限生效；`retry_history` 保留旧 error |
| 集成 | settings API | GET/PUT 七个键往返、越界 400、`_reload_env` 后 `os.getenv` 生效 |
| 回归 | 全量 pytest | 不劣于基线 130 passed / 3 failed / 3 skipped；`tests/unit/test_ai_handler_analysis.py` 不回归 |
| 前端 | `pnpm build` + 类型 | 构建通过，`dist/` 与源码一致（`tests/test_frontend_build_paths.py` 保持通过） |

离线核对：用 `research/product-1086957165721-snapshot.json` + `research/repro_moderation_bisect.py` 复算改造前后体积（AC1 的 6.00MB 基线）。

---

## 8. 影响面、回滚与风险

影响面：AI 分析入参（全部走 AI 的商品）、失败态落库形态、结果页卡片渲染、导出文案、复检环节新增重跑分支、设置页 AI 标签页。不改 prompt 判定口径、不改 `result_items` 表结构、无迁移。

回滚：纯 `git revert` 即可——新增字段都在 `raw_json` 内，旧代码忽略未知键；设置项删掉不影响旧读取（`os.getenv` 有默认值）。`data/`、`state/` 不被本任务写入（唯一例外是 §5 的 `UPDATE result_items`，属业务正常写路径且只针对失败项）。

| 风险 | 缓解 |
| --- | --- |
| 缩图影响判定质量（小字/截图） | 先按 1600/q85 落地，抽查后按"先升质量、后升长边"调整 |
| 平台审核仍可能拒绝（不可控） | 失败态如实上报 + 阶梯 + 周期重跑 |
| 阶梯放大调用次数（单商品最多 4 次） | 只在命中审核/体积错误时升级；日志统计降级率，异常升高时收紧参数 |
| 重跑拖长轮次 | 每轮上限 5、最老优先、`AI_RETRY_MIN_AGE_MINUTES` 冷却 |
| 重跑覆盖历史判定 | 只覆盖 `analysis_status='failed'`；`retry_history` 保留证据 |
| 生产库写入不可逆 | 无 DDL；重跑仅改目标行；完成说明里留只读 SQL 前后对照 |

---

## 9. 未覆盖（明确不做）

- 内容层面的规避（裁剪敏感区域、打码等）——平台策略对抗，不在范围。
- 任务级参数覆盖（决策 5）：参数是全局配置。
- 失败项的独立筛选入口与统计面板（决策 7）：交给 `09-25-results-ai-tag-filter-batch-block`。
- 把历史失败记录批量订正为 failed（决策 7）：不静默改写，靠重跑覆盖。
- 「批量打标签但不屏蔽」等相邻能力：与本任务无关。