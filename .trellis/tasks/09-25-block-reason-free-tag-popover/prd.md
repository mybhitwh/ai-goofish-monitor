# 结果卡片屏蔽：自由标签理由 + 按钮旁浮层 + 就地更新（不再整页刷新）

## Goal

把结果页卡片的「屏蔽 + 记录理由」从**卡片内联的预设多选面板**，改成**按钮旁的快捷浮层 + 自由标签式录入**；并让屏蔽这件事**就地生效**，不再整表重拉：

- 理由不再是 5 个写死的预设选项，而是用户自己输入（与既有「标签」交互同构，理由本质上就是给商品打标签）。
- 理由录入不再在卡片正文中部展开（视线与鼠标要跨卡片移动，且改变卡片高度），而是点击屏蔽按钮后在按钮附近弹出浮层；浮层需要处理屏幕边界，贴近视口边缘时不被裁剪。
- 屏蔽/取消屏蔽成功后只动被操作的那一条卡片（失败才提示），不再触发整片骨架屏 + 整表重建（见 R4、R5）。

用户价值：屏蔽是结果页最高频的整理动作，当前每屏要屏蔽十几个商品时，每次都要"点按钮 → 视线下移找面板 → 在预设里挑 → 确认"，且预设往往对不上真实原因（例如"卖家不回消息""图太糊"这类没有对应预设）；更糟的是每次屏蔽都会让整页闪一次骨架屏、卡片全部重建，连续屏蔽十几条时页面像被打字机反复重刷。改成浮层 + 自由理由 + 就地更新后，一次屏蔽的路径收敛到"点按钮 → 打字/回车 → 确认"，画面只少一张卡片。

## Background

现状代码（2026-09-25 主线 `ResultCard.vue`）：

- 屏蔽按钮：`web-ui/src/components/results/ResultCard.vue:176-185`，卡片图片右上角的 EyeOff 圆形按钮；`sm` 断点下默认 `opacity-0`、`group-hover` 才显示。点击后 `handleBlockButtonClick()`（`:141-147`）判定：已屏蔽 → 直接 `toggle-block` 取消屏蔽（**此路径不改**）；未屏蔽 → `requestBlock()` 置 `isBlockPicking = true`。
- 理由面板：`ResultCard.vue:273-300`，`v-if="isBlockPicking"` 的内联区块，渲染在 `CardContent` 与标签行之间（卡片正文中部）。面板含 5 个预设理由（`:62-68` 的 `blockReasonPresets`，来自 i18n `results.card.blockReasonUnmatched/Seller/Usage/Shipping/Price`）、「取消 / 仅屏蔽 / 屏蔽并记录」三个按钮。
- 提交：`confirmBlockWithReasons()` / `confirmBlockOnly()`（`:131-139`）→ emit `block(item, reasons)` → `ResultsGrid.vue:58` → `ResultsView.vue:115` → `useResults.blockItem()`（`web-ui/src/composables/useResults.ts:441-455`）：理由合并进 `item._user_tags`（`updateItemAnnotation`）后置状态 `hidden`（`updateItemStatus`），再刷新列表。**理由的持久化契约就是用户标签，后端与 API 无需改动。**
- 裁剪风险：`ResultCard.vue:151` 的 `Card` 带 `overflow-hidden`，任何在卡片内部用绝对定位弹出的浮层都会被裁掉；浮层必须 Portal 到 body。
- 可复用基础件：`reka-ui@^2.7` 已是依赖（`ui/dialog/*` 等即基于它封装的 shadcn-vue 风格），自带 `Popover*`（含碰撞避让）与 `TagsInput*` 原语；项目内 `ui/` 目录目前没有 popover 组件。
- 候选数据：`usedTags` 已从 `ResultsView.vue:194` 一路传到 `ResultCard.vue:19`（既有标签编辑器用它做候选，`:98`）。理由候选可复用同一数据源，而不是另造预设。

两条问题由用户在评审时提出，原话：

1. 「屏蔽时，屏蔽理由，我不想要预设，而是自己填写，类似打标签一样。」
2. 「屏蔽时，屏蔽理由的填写位置不要在卡片上展开，这样效率太低了，我期望在点屏蔽按钮后，在屏蔽按钮附近弹出快捷操作窗口，注意考虑屏幕边界情况。」

第三条问题（2026-09-25 用户补充，本文档追加 R4/R5）：

3. 「结果查看页面，屏蔽一个商品后，页面要刷新一次，体验太差了，怎么优化」

现状机制（2026-09-25 主线 `useResults.ts`）：

- `blockItem()`（`web-ui/src/composables/useResults.ts:441-455`）与 `toggleItemBlock()`（`:391-405`）在 PATCH 成功后都调 `await fetchResults()`。这是结果页仅有的两个「写成功后整表重拉」的动作——同文件的 `saveItemAnnotation()`（`:425-436`）已经是就地更新范式，注释即写「成功后就地更新本地列表，不整页刷新」。
- `fetchResults()` 第一行同步置 `isLoading = true`（`:225`），而 `ResultsGrid.vue:26` 用 `v-if="isLoading"` 把整片卡片网格换成 8 个骨架屏，请求返回后整片重建：观感就是「页面重刷一次」，且所有卡片组件实例被销毁（同屏其他卡片已展开的 AI 理由、正在编辑的标签/备注、刚打开的屏蔽浮层状态全部丢失）。
- 默认 `include_hidden=false`，被屏蔽商品由服务端 `_is_record_visible()`（`src/services/result_storage_service.py:111`）过滤 → 列表少一条，后续卡片回流跳位。
- 合并视图（任务组 / 全部商品）代价更高：`fetchResults()` 对组内每个结果文件各发一次 `limit=100` 请求再前端合并去重排序（`useResults.ts:248-257`）。线上（`data/app.sqlite3` 只读核对）为 4 个结果文件，屏蔽一条要付 4 次请求 + 全量重排 + 全量重渲染。（数字为 2026-09-25/26 只读快照，随线上操作实时变动：274 行结果，active/hidden 先为 117/157、后为 115/159。）
- `isLoading` 期间筛选栏的刷新 / 黑名单 / 导出 / 删除四个按钮被 `disabled`（`ResultsFilterBar.vue:222-245`）。

## Requirements

### R1 (P0) 屏蔽理由自由填写，取消预设选项

- 浮层内**不得**出现硬编码的预设理由选项；理由由用户输入（标签式：输入 + 回车生成一个理由标签，可添加多条、可逐个删除）。
- 理由的存储与语义不变：仍然合并进该商品的 `_user_tags`（沿用 `updateItemAnnotation` + `updateItemStatus` 两个既有接口），不新增字段、不改后端、不改 API。
- 空理由仍可直接屏蔽（保留"只想屏蔽、不想写理由"的低成本路径）。
- i18n 中不再使用的 5 个预设理由键在 `zh-CN` / `en-US` 两侧同步移除；新增文案键同样两侧同步。

### R2 (P0) 理由录入改为按钮旁浮层，且屏幕边界安全

- 点击屏蔽按钮后，在按钮附近弹出浮层（锚定触发按钮，默认在按钮下方、右对齐）；浮层渲染不改变卡片布局，卡片高度与网格排布不变。
- 浮层必须 Portal 到 body，不被 `Card` 的 `overflow-hidden`（及任何滚动/裁剪容器）裁切。
- 边界处理：浮层贴近视口右边缘/下边缘时自动翻转或收缩（默认避让行为由 reka-ui 的碰撞检测提供），保证浮层完整可见；视口过小或内容过高时浮层自身可滚动，不撑破屏幕。
- 移动端（无 hover、窄视口）可用：浮层宽度不超出视口，触发按钮在 `sm` 以下本来就常显。

### R3 (P1) 交互细节收敛

- 浮层打开时输入框自动聚焦，键盘即可完成"输入理由 → 回车 → 提交"；`Esc` 关闭且不提交（不触发屏蔽）。
- 浮层打开期间，触发按钮保持可见（当前 `sm:opacity-0 sm:group-hover:opacity-100` 的机制在鼠标移向浮层时会隐藏按钮，需按打开状态强制显示）。
- 已屏蔽商品的按钮点击仍是直接取消屏蔽（现行为不变，不弹浮层）。
- 浮层内的引入/移除理由标签控件具备可访问名称（复用或新增 i18n 文案）。

### R4 (P0) 屏蔽/取消屏蔽就地更新，不再整表重拉

- PATCH 成功后直接在本地那条 `ResultItem` 上落地可见性：`_status` / `_hidden_reason` / `_effective_hidden`，语义与服务端 `_decorate_record_visibility()`（`result_storage_service.py:94-108`）一致；理由标签就地写回 `_user_tags`（annotation 接口本来就返回 `{note, tags}`，无需额外请求）。
- `filters.include_hidden === false`（默认视图）时把该条从列表移除并同步 `totalItems`；`include_hidden === true` 时原地保留，卡片按现有 `isHidden` 逻辑灰化 + 遮罩，不走「消失再出现」。
- 全过程不置 `isLoading`、不出现骨架屏、不整表重建；`v-for` 的 key 是商品ID，只有被操作的那一条 DOM 会变化。
- 失败时不改动本地列表，并给出可见提示（toast，与 `handleAnnotate` 的失败提示口径一致）；当前 PATCH 失败只写 `error.value`、页面无反馈。
- 实现取「提交成功后落地」而非乐观回滚：本机 PATCH 延迟可控，避免为失败回滚维护快照与还原逻辑。
- **合并视图必须带会话级抑制集合**（否则就地更新会被服务端旧状态撤销）：同一闲鱼商品常被同组多个任务命中而分落在多个结果文件——只读核对线上库（2026-09-25 `data/app.sqlite3`）`item_id` 跨文件重复最多达 4 个文件（如 `1087991288384`、`1086760717414` 各在 4 个文件），且 `1086388785081` 处于 `iPad_Air_11_256=active` / `iPad_Air_M4=hidden` / `iPad_Air=hidden` 的分裂状态。而屏蔽只写 `_source_file` 一个文件（`resolveTargetFile()`），合并去重后仍会有 active 副本参与渲染。因此 composable 需维护会话级「本地已屏蔽商品键」集合，软/硬刷新落地时过滤这些键，保证本次会话内屏蔽结果不被撤销（取消屏蔽时移出该集合）。
- **屏蔽的持久化语义不在本任务范围**：屏蔽状态是否应按商品全局（跨结果文件）；并行任务 `09-25-fix-blocked-item-reappear-merged-view` 负责定夺与修复；本任务只保证「本次会话内不再复活」。

### R5 (P1) 拆出「软刷新」通道

- `fetchResults()` 区分两种语义：首屏加载（首次进入 / 切换结果文件或筛选条件）保持现状——骨架屏；软刷新（写操作后的后台收敛、筛选栏「刷新」按钮）保留当前卡片、只给进度提示，不置 `isLoading`。
- 写操作的收敛做**防抖**：连续屏蔽多条只在停止后合并成一次请求；其作用是修正本地推不出的差异——`include_hidden=true` 时服务端把非 active 商品排到最后（`result_storage_service.py:73`），以及「手动屏蔽且同时命中黑名单规则」的商品取消屏蔽后服务端仍按规则隐藏（黑名单匹配含 `re:` 前缀与 ASCII 词边界，见 `result_blacklist_service.py`，不复刻到前端）。
- 软刷新落地时数组替换不销毁卡片实例（key 相同即复用），卡片内的展开态与编辑态保留。

## Decisions（已定，review 可改）

- **理由候选**：复用 `usedTags`（用户自己的最近标签，取前 8 个，与既有标签编辑器一致），仅作为"最近用过"的快捷候选，不是预设理由。若 review 认为应完全空白输入，去掉候选行即可。
- **提交按钮**：收敛为一个主按钮「屏蔽」；提交时把输入框中尚未回车的文本（trim 后非空）自动收作理由，连同已确认的理由标签一起提交（少一步）。原「屏蔽并记录 / 仅屏蔽」二分取消，空理由提交即等价于原「仅屏蔽」。
- **不做**：理由统计/聚合视图、屏蔽理由的后端建模（如独立 reason 字段）、结果页以外的入口。
- **本地可见性只算手动屏蔽这一档**：前端只复刻 `_status='hidden'` / `'active'` 的手动语义；黑名单规则命中与过期（`expired`）仍以服务端 `_hidden_reason` 为准，由防抖软刷新收敛，不在前端复刻匹配逻辑。
- **不引入撤销**：撤销 toast 与退场动画列在 Open Questions，不在本轮范围（用户本次报告的是刷新问题，不是可逆性）。

## Acceptance Criteria

- **AC1 无预设**：浮层内不存在任何硬编码理由选项；`zh-CN.ts` / `en-US.ts` 中 `blockReasonUnmatched`、`blockReasonSeller`、`blockReasonUsage`、`blockReasonShipping`、`blockReasonPrice` 已移除（`grep` 无残留引用）。
- **AC2 标签式理由**：输入文本回车生成理由标签；标签可删除；支持多条；提交后该商品 `_user_tags` 包含这些理由（可用"标签筛选"或 `jsonl/` 结果文件核对），且 `_status` 为 `hidden`；不填理由提交后也能屏蔽。
- **AC3 浮层与布局**：点击屏蔽按钮后浮层出现在按钮附近；卡片高度/网格排布在开合浮层前后不变；浮层不被卡片裁切（Portal 渲染）。
- **AC4 边界**：手工验证以下场景浮层完整可见——(a) 视口最右列卡片的屏蔽按钮（浮层向右越界时应右对齐/收缩）；(b) 最后一行卡片的屏蔽按钮（下方空间不足时应翻转到上方）；(c) 移动端窄视口（375px 宽）。
- **AC5 交互**：打开即聚焦输入框；`Esc` 关闭不提交，商品未被屏蔽；浮层打开期间触发按钮可见；已屏蔽商品点击按钮仍直接取消屏蔽（无浮层）。
- **AC6 构建与基线**：`cd web-ui && pnpm build`（含 `vue-tsc`）通过；后端测试基线不劣化（`.venv/bin/python -m pytest tests/ -s` → 136 collected / 130 passed / 3 failed / 3 skipped）。
- **AC7 验收留痕**：UI 变更按 `AGENTS.md` 约定提供截图（浮层打开态 + 一条边界场景）。
- **AC8 就地更新**：屏蔽 / 取消屏蔽单条商品时，DevTools 网络面板中**没有** `GET /api/results/{file}` 列表请求（只有 PATCH 与必要的 insights 请求）；页面无骨架屏闪烁；同屏其他卡片的滚动位置、已展开的 AI 理由、正在编辑的标签/备注不丢失；`include_hidden=false` 时该卡片消失、`include_hidden=true` 时原地灰化保留；PATCH 失败时列表不变且有失败提示。
- **AC9 软刷新**：点筛选栏「刷新」按钮时卡片保持可见（不被骨架屏替换），另有进度提示；切换结果文件 / 筛选条件仍走骨架屏（首屏语义不退化）；连续屏蔽 5 条商品后列表最终与服务端一致（条数、顺序；`include_hidden` 两种模式各验一次）。

## Non-Goals

- 不改后端、API、结果文件结构；理由继续复用 `_user_tags` 契约，不新增状态字段。
- 不改既有「标签」编辑器的交互与视觉。
- 不引入新依赖（Popover/TagsInput 用已装的 `reka-ui`）。
- 取消屏蔽的行为语义不变（仍是直接取消、不弹浮层），但其落盘方式同样改为就地更新（R4）；黑名单规则、备注不动。
- 不在前端复刻黑名单匹配（含 `re:` 前缀与 ASCII 词边界）；规则的最终判定以服务端返回为准。
- 不做撤销与退场动画（见 Open Questions）。

## Open Questions（实现前无阻塞，review 时可推翻 Decisions）

- 是否需要"最近使用理由"的快捷候选行（当前按"复用 usedTags 前 8 个"设计）？
- 提交按钮文案：统一「屏蔽」是否足够，还是输入非空时用「屏蔽并记录」？
- 屏蔽后是否加一个 5 秒「已屏蔽 · 撤销」toast？就地更新落地后撤销成本很低（记下原 index 插回即可），但属于新增能力，不在本次报告范围内。
- 被屏蔽卡片消失时是否需要 `<TransitionGroup>` 淡出（纯观感，可另开）。