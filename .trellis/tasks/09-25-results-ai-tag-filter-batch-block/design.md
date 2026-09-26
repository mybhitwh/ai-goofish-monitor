# 设计：结果页 AI 标签筛选与批量屏蔽

对应 PRD：同目录 `prd.md`（R1–R7）。本文只讲技术设计：边界、契约、数据流、权衡、兼容与回滚。

## 1. 边界与分层

改动落在既有的 API → services → infrastructure 与前端 `composables → api → components` 路径上，不新增跨层依赖，不引入新框架：

| 层 | 文件 | 责任 |
| --- | --- | --- |
| 提示词资源 | `prompts/base_prompt.txt`（已跟踪，CRLF） | 固定词表与输出规则（R1） |
| services（新） | `src/services/ai_tag_service.py` | 词表常量、近义映射、`normalize_ai_tags()`、`derive_ai_tags()`（R1/R2） |
| services | `src/services/result_storage_service.py` | 读路径装饰 `_ai_tags`、抽出可复用的过滤判定、`ai_tags` 过滤、facets 聚合、批量改状态（R2/R3/R4） |
| API | `src/api/routes/results.py` | 新查询参数 + facets 端点 + 批量端点（R3/R4） |
| 前端状态 | `web-ui/src/composables/useResults.ts`、`api/results.ts`、`types/result.d.ts` | 筛选状态、facets 数据、批量调用与合并视图扇出（R5） |
| 前端视图 | `components/results/{ResultsFilterBar,ResultCard,ResultsGrid}.vue`、`views/ResultsView.vue`、新 `components/results/BatchStatusDialog.vue` | 标签展示、facet 勾选、确认弹窗（R5/R6） |
| 文案 | `web-ui/src/i18n/messages/{zh-CN,en-US}.ts` | 双语键位同步 |
| 测试 | `tests/unit/test_ai_tag_service.py`、`tests/integration/test_api_results_ai_tags.py`、`tests/integration/test_api_results_batch_status.py` | R7 |

不做的事（含理由）：**不新增 `result_items` 列**（见 §4.1）、不改通知路径、不引入任务级词表配置、不加卡片复选框与分页 UI。

## 2. 数据契约

### 2.1 标签词表（唯一事实来源：`ai_tag_service` 内的常量）

```
型号不符、容量不符、版本不符、卖家信用低、商家或贩子、拆修或维修史、
成色问题、电池健康偏低、电池健康未知、交易方式不符、疑似低价钓鱼、
描述与实际不符、信息缺失、其他不符要求
```

- 提示词侧：`risk_tags` 只能从此表取值（**最多 5 个**，约束新输出），`有条件推荐` 等"非不符原因"不得出现，无合适标签时用 `其他不符要求`。
- 服务端侧：`normalize_ai_tags(raw: object) -> list[str]`，按顺序 trim → 丢弃空值与非字符串 → 近义映射 → 去重（保持首次出现顺序）→ 上限裁剪（**8 个**，比提示词侧宽，用于容忍存量长尾、避免丢信息）。未知标签原样保留。
- 近义映射表（2026-09-26 只读复算：线上 274 行共 76 个取值、614 个实例，本表覆盖 76/76、裸未知 0 个；唯一事实来源是 `ai_tag_service` 内的常量，此处为契约快照）：

| 原始值 | 归一化 |
| --- | --- |
| 型号不符 / 尺寸错误 / 非目标机型 / 型号不符风险 | 型号不符 |
| 容量不符 / 容量错误 / 容量异常 | 容量不符 |
| 版本不符 / 非国行版本 / 非国行 / 版本存疑 | 版本不符 |
| 卖家信用低 / 卖家信用不足 / 卖家信用缺失 / 卖家信用差 / 新注册卖家 / 未达标信用 / 信用较差 / 信用缺失 / 非优质卖家 / 无评价记录 / 无交易历史 / 未通过信用门槛 | 卖家信用低 |
| 商家或贩子 / 商家贩子 / 商家贩子嫌疑 / 商家贩子风险 / 批量同型号 / 同型号密集出货 / 批量出货 / 同型号重复发布 | 商家或贩子 |
| 拆修或维修史 / 需确认维修史 / 需核实维修历史 / `history_missing` / `repair_history_missing` / `MISSING_REPAIR_HISTORY` | 拆修或维修史 |
| 成色问题 / 轻微划痕 / 轻微外观瑕疵 | 成色问题 |
| 电池健康未知 / `battery_health_missing` / 需确认电池状态 / 需确认电池健康 / 需核实电池健康度 / `MISSING_BATTERY_INFO` | 电池健康未知 |
| 交易方式不符 / 交易方式受限 / 仅限本地自提 / 交易方式不合规 / 交易方式不明确 / 地域限制 / 非上海自提 | 交易方式不符 |
| 描述与实际不符 / 虚假宣传 / 虚假信息 / 疑似虚假信息 / 信息不实 / 欺诈性发布 / 描述与图片冲突 / 信息矛盾 / 时间逻辑矛盾 / 生产日期异常 | 描述与实际不符 |
| 信息缺失 / 信息不透明 / 信息不完整 / 未验证身份 / 交易行为不透明 / `NEEDS_MANUAL_CHECK` / `needs_manual_check` / 需确认保修状态 / 容量信息不明确 / 容量信息缺失 | 信息缺失 |
| 疑似低价钓鱼 | 疑似低价钓鱼 |
| 一票否决 / 配件非原装 / 价格偏高 / 交易风险 / 交易风险高 | 其他不符要求（语义兜底） |
| 有条件推荐 / `conditional_recommendation` / `CONDITIONAL_RECOMMENDATION` | 丢弃（不是不符原因） |

### 2.2 派生契约：`derive_ai_tags(record) -> list[str]`

输入是读路径已解析的记录 dict（`raw_json` 的对象形态），输出写入装饰字段 `_ai_tags`：

1. `raw = record["ai_analysis"]["risk_tags"]` → `normalize_ai_tags(raw)`；结果非空即返回。
2. 若 `record["ai_analysis"].get("error")` 为真 → 返回 `[]`（失败态不属于判定结果，PRD R2）。**该判断必须在兜底之前**。
3. `is_recommended` 为假且 `criteria_analysis` 存在 → 取失败项（status 大小写不敏感等于 `FAIL`/`fail`）经键名映射（`model_chip→型号不符`、`seller_credit→卖家信用低`、`seller_type→商家或贩子`、`history→拆修或维修史`、`shipping→交易方式不符`、`condition→成色问题`、`battery_health→电池健康偏低`）得到标签，非空即返回。
4. 仍为空且 `is_recommended` 为假 → `["其他不符要求"]`。
5. 其余（推荐项、无分析结果）→ `[]`。

不变量：`_ai_tags` 恒为 list（可能为空），与 `_user_tags` 一致；派生是纯函数，同一记录多次调用结果相同。

### 2.3 HTTP 契约

**列表 / insights / export 新增参数**

- `ai_tags=型号不符,容量不符`：逗号分隔；空串与纯空白按"未提供"处理；多标签 **OR**（命中任一即可），与既有 `tags=` 完全同构（`result_storage_service.py:146,156-157` 的集合交集写法）。
- 与 `ai_recommended_only`/`keyword_recommended_only`（互斥，400 规则不变）、`tags=`、`has_note`、`include_hidden` 是 AND 组合。

**facets**

```
GET /api/results/ai-tags?filename=<可选>&include_hidden=<bool,默认 false>
→ 200 {"tags": [{"name": "型号不符", "count": 35}, ...]}
```

- 计数口径：该范围内**与列表同一可见性口径**的记录集合中，每个标签出现的条数（一条记录同时含两个标签则各计一次）。
- 顺序：count 降序，count 相同按 name 升序（前端可依赖此顺序，避免每次刷新跳动）。
- **路由必须声明在 `GET /api/results/{filename}` 之前**：现有 `/used-tags`（`results.py:89`）就在 `/{filename}`（`:95`）之前，否则 `/ai-tags` 会被 `{filename}` 吃掉。

**批量改状态**

```
POST /api/results/{filename}/items/batch-status
{
  "status": "hidden" | "active",
  "ai_tags": ["型号不符"], "tags": ["不符合我的要求"],
  "ai_recommended_only": false, "keyword_recommended_only": false,
  "has_note": false, "include_hidden": false,
  "reason_tags": ["型号不符"],
  "dry_run": false
}
→ 200 {"filename": "...", "status": "hidden", "affected": 74, "dry_run": false,
       "reason_tags_applied": ["型号不符"]}
```

- 选定目标的判定 = 列表接口同一套过滤 + 状态约束：`status="hidden"` 时只取 `result_items.status='active'`，`status="active"` 时只取 `status='hidden'`；`expired` 永不入选。因此 `affected` 是**精确**的改写行数。
- 护栏：没有任何收窄条件（`ai_tags`/`tags`/`ai_recommended_only`/`keyword_recommended_only`/`has_note` 全空/假）→ `400`；`status` 非法 → `400`；`ai_recommended_only` 与 `keyword_recommended_only` 同时为真 → `400`（与列表一致）。
- `dry_run=true` 只做选取与计数，不写库；与写库路径共用同一个选取函数，保证"预览 = 实际"。
- `reason_tags`：trim、去空、去重、≤5 个、每个 ≤24 字；合并（union）进命中商品的 `_user_tags`，沿用既有注解契约（`item_annotations.tags_json`，全局按 `link_unique_key`）。
- 幂等：同一请求重复执行第二次 `affected=0`（状态已被改写），不报错。
- 未知 filename → `200 affected=0`（与列表接口对未知文件的宽松行为一致），不新增 404 语义。

### 2.4 状态机（写进实现与测试）

```
active ──批量屏蔽(status=hidden)──> hidden ──批量恢复(status=active)──> active
expired ────── 任何批量操作都不可达 ──────
```

## 3. 数据流

```
模型(受词表约束的 risk_tags) → 分析落库(raw_json，既有路径，不改)
      ↓ GET /{filename}
_load_filtered_records_from_conn：读该文件全部行 → json.loads(raw_json)
      → _decorate_record_visibility（既有）+ 新增 _ai_tags=derive_ai_tags(record) + _user_tags
      → 可见性/黑名单/用户标签/note/AI 标签过滤 → 切片分页
      ↓
前端：卡片渲染 _ai_tags；筛选条 facets（GET /ai-tags）勾选 → 重新请求列表
      ↓
批量：POST /items/batch-status（筛选条件 → 服务端选取全部匹配行 → UPDATE status
      + 可选合并 _user_tags）→ 返回 affected → 前端刷新列表与 facets
```

## 4. 权衡与决策

### 4.1 读路径派生 vs 物理列（决策：读路径派生）

- 物理列需要同时解决"存量回填"（`INSERT OR IGNORE` 永不更新，`result_storage_service.py:168-209`）与"新增字段在存量 raw_json 中不存在"，收益是 SQL 下推与索引——而当前过滤/分页本来就在 Python 侧完成（`:115-161`、`:298-325`），单文件行数量级 10²–10³，索引无实际收益。
- 读路径派生让**存量 274 行立即具备标签**，用户不必等新一轮抓取；代价是每次读取多一次纯函数调用（可忽略）与"标签随代码版本变化"（这其实是优点：词表更新无需回填）。
- 后续若行数上万或需要跨文件 SQL 聚合，再引入物理列 + 回填，届时 `_ai_tags` 的派生函数可直接复用为回填器。

### 4.2 多标签筛选取 OR（决策：OR）

与既有用户标签筛选同构，用户认知一致；已定 OR（2026-09-26）；将来如需"同时满足 A 且 B"的收窄，加 `ai_tags_match=all|any` 参数，不改默认语义。

### 4.3 理由写入用户标签（决策：沿用既有契约）

`blockItem`（`useResults.ts:441-455`）已经把屏蔽理由并进 `_user_tags`，后端没有理由字段。批量沿用同一契约可以复用 `update_annotation_sync` 与既有 `used-tags` 筛选，避免出现第二套"理由"概念。副作用是商品级（跨任务）可见——已定接受（2026-09-26）。

### 4.4 合并视图由前端扇出（决策：客户端逐文件调用）

后端没有跨文件的写入端点，也不应该新造（结果数据本身按文件分区，`result_items.result_filename` + 唯一约束决定了一条记录只属于一个文件）。前端已对 `__all__`/`__group_{id}__` 做逐文件拉取（`useResults.ts:229-258`），批量操作复用同一文件清单逐文件 POST 并汇总；每个文件只动自己的行，天然避免跨文件误伤。部分失败按文件报告。

**与 `09-25-fix-blocked-item-reappear-merged-view` 的接口**：该任务已定**商品级**（2026-09-26）：本设计不变——扇出仍逐文件调用，服务层选取判定与状态改写分离使"全局幂等"只需替换状态改写那一层；`affected` 按行（result_items 行）计数，弹窗写明"按行计，合并视图去重前"。

### 4.5 批量计数来源（决策：弹窗用列表已返回的 `total_items`，API 仍提供 `dry_run`）

`total_items` 是过滤后、切片前的真实计数，已随列表返回，弹窗无需额外请求；`dry_run` 保留给 API 层验证、测试与合并视图的边界核对，并保证"预览 = 实际"这一不变量可被独立检验。

## 5. 前端设计

- **状态**：`ResultFiltersState` 增加 `aiTags: string[]`。持久化到 `localStorage['resultFilters']` 的**旧值缺少该键**时按 `[]` 补齐（读取时 merge 默认值，不因结构变化丢用户既有筛选）。
- **facets**：新增 `aiTagFacets: {name,count}[]`，与结果一起刷新（初次加载、手动刷新、批量操作后、websocket `results_updated`）。空数组时筛选条不渲染该区域。
- **卡片**：`_ai_tags` 渲染为小徽章，位置在 AI 面板（`ResultCard.vue:214-247` 附近）而不是用户标签行（`:302-317`），视觉用 `outline`/`destructive` 变体 + `title` 提示"AI 判定标签"；最多展示 4 个，多余折叠为 `+N`；点击即切换该标签的筛选（R6，卡片 emit → `ResultsView` → composable）。
- **筛选条**：在用户标签筛选（`ResultsFilterBar.vue:204-219`）旁增加「AI 标签」区，chip 显示 `名称 (计数)`，选中态高亮，多选；复用现有 `handleToggleTag` 形态。
- **批量按钮**：位于动作按钮组（`:221-249`）。`include_hidden=false` → 「批量屏蔽（N）」；`include_hidden=true` → 「批量恢复（N）」。N 取当前筛选的 `total_items`（合并视图为各文件之和）。禁用条件：无收窄条件、N=0、加载中。
- **确认弹窗**（新组件 `BatchStatusDialog.vue`，基于既有 `ui/dialog`）：显示将影响条数、`max(0, N - 已加载条数)` 的"未在当前页显示"提示、屏蔽后果说明（不再复查与抓取详情）、理由标签编辑器（默认填当前选中的 AI 标签，可增删改，与 `ResultCard` 的标签录入交互一致；若 `09-25-block-reason-free-tag-popover` 先落地则复用其自由标签录入组件）。
- **结果反馈**：成功后 toast「已屏蔽 N 条 / 已恢复 N 条」并刷新列表 + facets；部分失败时 toast 报错并给出每文件成功数。
- **i18n**：新增 `results.filters.aiTagFilterLabel`、`results.filters.aiTagEmpty`、`results.card.aiTagBadge`、`results.batch.*`（按钮、弹窗标题/正文/后果说明/理由标签、成功与部分失败），`zh-CN.ts` 与 `en-US.ts` 同步。

## 6. 兼容、部署与回滚

- **无数据库变更**：不需要迁移脚本，旧版本代码读同一份数据不受影响。
- **API 兼容**：新参数可选、新端点新增、`_ai_tags` 是新增装饰字段；旧前端调用行为不变。`POST` 端点是新增能力，无既有调用方。
- **提示词变更只影响未来分析**：存量记录保持原样，标签靠派生兜住；`prompts/base_prompt.txt` 是已跟踪文件，改动必须保留 CRLF 与既有段落结构（AGENTS.md 提到的 CRLF 规范化提交与 systemd 单元依赖）。
- **数据回滚**：误批量屏蔽用"包含已屏蔽 + 同筛选 + 批量恢复"原路恢复；写库前可先 `dry_run`。运维层另有 `data/backups/` 的 sqlite 快照惯例。
- **部署**：本任务只改代码与提示词；是否在 u12 上重启 `goofish.service` 由用户在评审后决定（生产实例，AGENTS.md 运维约定）。当前任务组处于禁用冷却状态，提示词变更不会立即影响线上分析输出。

## 7. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 词表约束改变所有任务的分析输出（不只 iPad） | 词表取自 iPad/MacBook 两套硬性规则的公共语义，未知语义可落 `信息缺失`/`其他不符要求`；`is_recommended`/`reason`/`criteria_analysis` 契约不变 |
| 过渡期标签仍然碎（存量与新分析的差异） | 近义映射表覆盖线上已观察到的全部 76 个取值（裸未知 0 个，7 例落语义兜底）；未知标签原样保留（不丢信息），展示顺序把长尾放后面 |
| 批量误屏蔽 | 必须收窄条件（400 护栏）+ 确认弹窗显示条数与后果 + `dry_run` 预览 + 批量恢复 + 备份惯例 |
| 只看到前 100 条 | 批量按服务端筛选选取全部匹配行；弹窗明示"其中 N 条未在当前页显示" |
| facets 与列表口径漂移 | 两者共用同一个可见性与过滤函数，集成测试断言 facets 计数 = 列表里该标签的条数 |
| 分析失败记录被误打标签 | `derive_ai_tags` 在兜底之前先看 `error`；单测钉住；与 `09-25-fix-ai-moderation-rejected-analysis` 的失败态标记对齐 |
| 路由顺序导致 `/ai-tags` 被 `{filename}` 匹配 | 端点声明在 `/{filename}` 之前（与 `/used-tags` 同位置），并加一条集成测试直接打 `/api/results/ai-tags` |

## 8. 测试策略

- **单元**（`tests/unit/test_ai_tag_service.py`）：近义映射（每条一对一断言）、`有条件推荐` 丢弃、去重与顺序、上限裁剪、非字符串/空值容错、未知标签保留、`error` 记录返回空、`criteria_analysis` 回退（大小写不敏感）、兜底标签、推荐项无标签返回空。
- **集成**（`tests/integration/test_api_results_ai_tags.py`）：`_ai_tags` 出现在 item 上；单标签筛选；多标签 OR；与 `has_note`/`tags=`/`include_hidden` 组合；facets 计数与顺序、`filename` 限定、`include_hidden` 影响；insights 与 CSV 导出同步生效；`/api/results/ai-tags` 不被 `{filename}` 吃掉。
- **集成**（`tests/integration/test_api_results_batch_status.py`）：无收窄条件 400；`dry_run` 预览数 = 实际 `affected` = 同条件 `total_items`；屏蔽只动 active、`expired` 不变；恢复只动 hidden；理由标签合并进 `_user_tags`（与既有标签 union）；重复调用第二次 `affected=0`；两个文件互不影响。
- **只读线上验证**（实现后手工跑一次，不写库）：对 `data/app.sqlite3` 逐行跑 `derive_ai_tags`，核对 `型号不符/容量不符/卖家信用低` 的计数 = 原始变体合并后的计数（脚本入 `research/`，查询用 `mode=ro`）。
- **前端**：无测试框架，采用 `pnpm build` + 手工清单（默认视图勾选标签→列表变化；`include_hidden` 切换→按钮在屏蔽/恢复之间切换且计数正确；弹窗条数与"未显示"提示；执行后 toast 与刷新；卡片标签点击即筛选；两种语言文案）。
- **回归**：`pytest tests/ -s` 不得新增失败（u12 基线 130p/3f/3s）；`tests/test_frontend_build_paths.py` 保持通过。