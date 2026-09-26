# 百炼输入审核拒绝（data_inspection_failed）实测证据

2026-09-25 夜间在 u12 主仓（生产实例）对同一件商品做了受控重放，用于给修复定方向。结论先行，证据随后，最后写清"还没搞清的部分"——避免后续把猜测当成结论用。

- 复现脚本：`repro_moderation_bisect.py`（本目录）
- 商品输入快照：`product-1086957165721-snapshot.json`（本目录）
- 线上原始事件：`logs/iPad_Air_M4_256G_1.log:2203-2243`，落库 `data/app.sqlite3` 的 `result_items.id=274`，最终 `request_id=5c0fc299-e8d2-9285-8bd4-b524e9247acd`

环境：`OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1`、`OPENAI_MODEL_NAME=qwen-vl-max`、`temperature=0.1`、`max_tokens=4000`、`response_format={"type":"json_object"}`；prompt 用 `prompts/ipad_air_m4_criteria.txt`。所有用例的文字部分逐字节相同，只改图片入参。

## 结论

1. **拒绝的是重型多模态入参**：多张全分辨率（4368×5824 ≈ 25MP/张）图片的组合最容易命中；缩图或减少图片张数后稳定通过。
2. **不是违禁词、也不是某一张图**：纯文本恒通过；5 张图逐张单独发送全部通过。
3. **判定不稳定、不可按需复现**：线上被拦 4/4 的那次 payload（5 张原始 WebP，6.00MB），事后重放 10 次全通过。因此这是平台侧审核在重负载入参上的 fail-closed，而非稳定的内容判定。
4. **声明 MIME 不影响判定**：`image/webp` 与 `image/jpeg` 声明下结果完全一致（4 组对照）。图片字节实际是 WebP、扩展名是 `.jpg`、声明是 `image/jpeg`——错标值得顺手修，但它不是触发原因。
5. **另有一条体积硬错误**：38.52MB 时返回 `Multimodal file size is too large`（与审核拒绝是不同的 code），修复需要把它和审核拒绝一起纳入降级路径。

## 实验记录

### A. 图片形态与审核判定

| 用例 | 图片入参 | payload | 判定 |
| --- | --- | --- | --- |
| T1 | 纯文本 | 0.02MB | 通过 |
| T2 | 5 张原始字节（WebP，声明 jpeg）——**线上口径** | 6.00MB | 通过（共重放 10 次，见 D 组） |
| T4–T8 | 单张原始字节，逐张发送 | 0.10–3.19MB | 全部通过 |
| L1 | 5 张缩到 30% 后转 JPEG q85 | 1.06MB | 通过 |
| E1 | 单张最大图，JPEG q90 原分辨率 | 6.56MB | 通过 |
| E2 | 单张最大图，JPEG q60 原分辨率 | 3.36MB | 通过 |
| E3 | 两张（最大+次大），JPEG q90 原分辨率 | 10.54MB | 通过 |
| E4 | 5 张 JPEG q45 原分辨率 | 6.75MB | **审核拦截** |
| E5 | 5 张 JPEG q50 原分辨率 | 7.07MB | **审核拦截** |
| E6 | 5 张 JPEG q55 原分辨率 | 7.38MB | **审核拦截** |
| L2 | 5 张 JPEG q60 原分辨率 | 7.77MB | **审核拦截** |
| L3 | 5 张 JPEG q90 原分辨率 | 14.63MB | **审核拦截** |
| L4 | 5 张 JPEG q100 原分辨率 | 38.52MB | 体积硬错误（非审核 code） |

要点：**体积不是唯一变量**——E3（两图 10.54MB）通过，而 E4（五图 6.75MB）被拦；单图 6.56MB 通过。最能解释全部数据的是"图片张数 × 单图分辨率"这个组合（5 张 25MP 图 = 约 127MP），而缩图或减图都能把它拉回安全区。

### B. 稳定性

| 用例 | 次数 | 结果 |
| --- | --- | --- |
| 线上原始 5 图（WebP，6.00MB）连发 | 3 | 全通过 |
| 线上原始 5 图并发 4 路（模拟 ai_analysis_concurrency=2 的同批请求） | 4 | 全通过 |
| 5 张 JPEG 原分辨率（q45/q50/q55/q60/q90） | 9 | 全被拦 |

线上事发时（21:10）同一 payload 连挂 4 次；21:40 之后同一 payload 10 次全过。**同一内容、不同时点，判定不同**——所以修复不能建立在"内容合规"假设上。

### C. 声明 MIME 2×2 对照

| 真实字节 | 声明 | payload | 判定 |
| --- | --- | --- | --- |
| WebP | `image/webp` | 6.00MB | 通过 ×2 |
| WebP | `image/jpeg` | 6.00MB | 通过 ×2 |
| JPEG q60 | `image/webp` | 7.77MB | 审核拦截 ×2 |
| JPEG q60 | `image/jpeg` | 7.77MB | 审核拦截 ×2 |

横向（同一字节不同声明）结果一致 ⇒ 声明不参与判定；纵向（不同字节）结果不同 ⇒ 判定跟实际图片数据走。

### D. 顺带确认的事实

- 闲鱼 CDN 的 `-xy_item.jpg`、`~livephoto~` 链接返回的都是 **WebP 字节**（`file(1)` 与 PIL 均确认；被拒那件商品的 5 张图是 4368×5824，成功对照商品的 8 张是 1440×1920）。
- 模型能正常读取 WebP 图片：单图用例的回答引用了"关于本机""电池健康"截图里的内容，说明平台侧解码没问题，问题只在审核环节。
- 纯文本与体积无关的成功用例都返回了完整 JSON 判定，说明文本侧没有任何会被审核命中的内容。

## 复现方式

```bash
# 需要 .env 里可用的 OPENAI_API_KEY；会产生少量 API 调用费用
.venv/bin/python .trellis/tasks/09-25-fix-ai-moderation-rejected-analysis/research/repro_moderation_bisect.py --case all
# 单组：--case text|raw|scaled|jpeg|ladder|single|mime，可配 --quality / --img-dir
```

修复验收时建议固定跑 `--case raw`（修复前口径，应显著变小或不再触发）与 `--case scaled`（修复后新口径）。

## 尚未搞清的部分（不要当成结论）

- 平台侧审核的确切规则未公开：为什么"5 张 25MP 原始 WebP"在一个时点连挂、另一个时点连过，而"同一批像素的 JPEG 编码"稳定被拦——只能观察到相关性。
- 无法从外部确认这是配额、内部处理预算，还是审核服务超时后的 fail-closed 行为；也无法判断阈值是否随时间/账号漂移。
- 因此**不能**据此推断"某张图 / 某段描述违规"，也**不能**用"避开某个词"作为修复手段。