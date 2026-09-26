# 设计：屏蔽理由浮层（BlockReasonPopover）+ 屏蔽就地更新

## 边界与影响面

**仅前端**：`web-ui/`，无后端、无 API、无数据契约变更。

| 文件 | 动作 |
| --- | --- |
| `web-ui/src/components/results/BlockReasonPopover.vue` | 新增：浮层组件（触发器插槽 + Portal 内容 + 标签式理由输入） |
| `web-ui/src/components/results/ResultCard.vue` | 改造：移除内联预设面板与 `blockReasonPresets`，屏蔽按钮接入浮层组件；取消屏蔽路径的交互保持不变 |
| `web-ui/src/composables/useResults.ts` | 改造（R4/R5）：`blockItem` / `toggleItemBlock` 就地落地，新增 `applyLocalVisibility()`、防抖 `scheduleReconcile()`、`fetchResults({ soft })` 与 `isRefreshing` |
| `web-ui/src/views/ResultsView.vue` | 小改（R4/R5）：`handleBlock` 改 `async` + `await` + 失败 toast（与 `handleAnnotate` 同口径）；向筛选栏透传 `isRefreshing` |
| `web-ui/src/components/results/ResultsFilterBar.vue` | 小改（R5）：新增可选 `isRefreshing` 进度提示（不参与 `disabled`，与 `isLoading` 语义分开） |
| `web-ui/src/i18n/messages/zh-CN.ts`、`en-US.ts` | 删除 5 个预设理由键；新增浮层文案键与软刷新/屏蔽失败文案（两侧同步） |

不改：`ResultsGrid.vue`（`isLoading` 语义与骨架屏分支保持现状；软刷新不经过它）、`ui/` 目录、事件链 `block(item, reasons)` / `toggle-block(item)` 的签名、后端与结果数据结构。

## 组件契约

`BlockReasonPopover.vue`

```
Props:
  candidates: string[]        // 最近使用标签（来自 ResultCard 的 usedTags），仅作快捷候选
  open: boolean               // v-model:open，由父组件持有（同现有 isBlockPicking 语义）
Emits:
  update:open(boolean)
  confirm(reasons: string[])  // 提交屏蔽；reasons 可能为空数组（等价旧「仅屏蔽」）
Slots:
  trigger                     // 屏蔽按钮本体仍留在 ResultCard，保住 group-hover 等卡片级样式
```

内部状态：`reasonTags: string[]`（已确认的理由标签）、`input: string`。`open` 变为 true 时重置两者（`watch(open)`），保证每次打开是干净的。

提交语义：`confirm` 前把 `input.trim()` 非空文本追加进 `reasonTags`（去重），再 emit。取消/`Esc` 只 `update:open(false)`，不 emit。

## 浮层位置与边界（R2 的技术核心）

- 用 `reka-ui` 的 `PopoverRoot` / `PopoverTrigger as-child` / `PopoverPortal` / `PopoverContent`：
  - `PopoverPortal` 把内容挂到 `body`，直接绕开 `Card` 的 `overflow-hidden`（`ResultCard.vue:196`）与网格容器，这是必须先定的机制——卡片内绝对定位方案不可行。
  - `PopoverContent` 设 `side="bottom"`、`align="end"`、`:side-offset="6"`、`:collision-padding="8"`；reka-ui 的 `avoidCollisions` 默认开启，贴边时自动翻转（`side` 变 `top`）与移位（`align` 落到 `start`/`center`），这是"考虑屏幕边界"的现成实现，不手写 `getBoundingClientRect` 逻辑。
  - 内容尺寸上限：`w-72 max-w-[calc(100vw-1rem)]` + 高度用 reka-ui 暴露的 `--reka-popover-content-available-height` 配 `max-h-… overflow-auto`，保证 375px 视口与"最后一行卡片"都不出屏。
- 触发按钮可见性：`PopoverRoot` 的 `open` 状态绑到按钮 class，`open` 时追加 `sm:opacity-100`，抵消 `sm:opacity-0 sm:group-hover:opacity-100`（`ResultCard.vue:216`）在鼠标移入浮层后按钮消失的问题。
- 关闭时机：`confirm` 先 `update:open(false)` 再 emit `block`。R4 落地后卡片不会再被「整表重拉」卷走，但被屏蔽的那条仍会在 `include_hidden=false` 时从列表移除，先关浮层可避免移除瞬间 portal 还挂在已卸载的触发器上。

## 理由录入（R1 的技术选型）

用 `reka-ui` 的 `TagsInput*` 原语（`TagsInputRoot` / `TagsInputInput` / `TagsInputItem` / `TagsInputItemText` / `TagsInputItemDelete`）：输入回车生成 chip、退格删除、可访问性由原语负责。两个要点：

- 关闭分隔符切分（理由是可含标点的自由文本，按逗号切开会把一句理由拆成多条）。
- 候选行渲染 `candidates.slice(0, 8)` 的 chips，点击即加入理由（与既有标签编辑器 `ResultCard.vue:98` 同规则）；候选为空则不渲染该行。
- 兜底方案（若 TagsInput 的受控行为与提交时机摩擦）：退回既有"input + chips"手写模式（与标签编辑器同构）。二选一，行为验收标准相同。

## 触发区改造

`ResultCard.vue` 右上角改为二选一渲染，避免把取消屏蔽塞进浮层：

- `canToggleBlock && isHidden`（`_hidden_reason !== 'rule'/'expired'` 且已屏蔽）→ 原按钮，`@click` 直接 `emit('toggle-block')`（现状不变）。
- `canToggleBlock && !isHidden` → 同一按钮放入 `BlockReasonPopover` 的 `#trigger` 插槽。
- 按钮 class 抽成组件内常量/计算属性，两处复用同一串样式，避免复制粘贴漂移。

## 就地更新与软刷新（R4 / R5 的技术核心）

现状：`blockItem()` / `toggleItemBlock()` 成功后 `await fetchResults()`，而 `fetchResults()` 会置 `isLoading`，`ResultsGrid` 的 `v-if="isLoading"` 整片换骨架屏——这是「页面刷新一次」的全部来源。同文件的 `saveItemAnnotation()` 已给出就地更新范式，R4 就是把它补到屏蔽路径上。

`useResults.ts` 的三件新增能力：

```ts
const isRefreshing = ref(false)          // 软刷新进度；与 isLoading 语义分离
let reconcileTimer: ReturnType<typeof setTimeout> | null = null

// 1) 就地落地可见性：只覆盖「手动屏蔽」这一档，与服务端 _decorate_record_visibility 同语义
function applyLocalVisibility(item: ResultItem, status: 'active' | 'hidden') {
  item._status = status
  item._hidden_reason = status === 'hidden' ? 'manual' : null
  item._effective_hidden = status === 'hidden'
  if (status === 'hidden' && !filters.include_hidden) {
    const index = results.value.indexOf(item)
    if (index >= 0) results.value.splice(index, 1)
    totalItems.value = Math.max(0, totalItems.value - 1)
  }
}

// 2) 软刷新：不置 isLoading，不经过 ResultsGrid 的骨架屏分支
async function fetchResults(opts: { soft?: boolean } = {}) { /* soft ? isRefreshing : isLoading */ }

// 3) 防抖收敛：修正本地推不出的差异（include_hidden=true 的排序把非 active 排最后；
//    「手动屏蔽 + 命中黑名单规则」的商品取消屏蔽后服务端仍隐藏）
function scheduleReconcile(delayMs = 800) { /* 防抖后 fetchResults({ soft: true }) + fetchInsights() */ }
```

原子性：`blockItem()` 先 `updateItemAnnotation`（有理由时）再 `updateItemStatus`，两者都成功才 `applyLocalVisibility` + `scheduleReconcile`；失败直接抛出，由 `ResultsView.handleBlock` 捕获并 toast，本地列表保持原样（不做乐观回滚，故无需快照）。

要点与取舍：

- **合并视图安全**：合并视图的 item 是 `tagSourceFile()` 的浅拷贝（`{...item, _source_file}`），就地改字段不会污染其他视图的数据；`_source_file` 路由逻辑原样复用。
- **`include_hidden=true` 的排序偏差**：就地更新保持原位，服务端语义是「非 active 排最后」；偏差由防抖软刷新在停止操作后收敛，避免「刚屏蔽的卡片立刻跳走」。
- **`totalItems` 目前无人消费**（`ResultsView.vue` 未解构，全仓只有 composable 内部与 dashboard 的另一字段在用）：仍同步维护，避免留一个假状态。
- **insights 一并收敛**：面板展示的是价格统计（`sample_count` 等），屏蔽会改变可见样本数；放进防抖回调里顺带刷新，不额外触发骨架屏。
- **失败反馈补口子**：`blockItem` 目前吞错误（只写 `error.value`）且 `ResultsView.handleBlock` 不 `await`；改为 `async` + `await` + toast，与 `handleAnnotate`（`ResultsView.vue:100-113`）完全同构。
- **防抖软刷新与本地变更的竞态**：维护一个 `mutationSeq` 计数，每次 `applyLocalVisibility` 自增；软刷新发起时记下序号，响应回来若序号已变（期间又屏蔽/取消屏蔽了别的商品）则**丢弃该响应并重排一次收敛**，否则把「刚屏蔽的卡片」按服务端快照复活。同时切文件/切筛选触发的硬加载要 `cancelReconcile()`，避免旧响应覆盖新列表。
- **`fetchResults` 的软/硬分支不共用一个 `finally` 开关**：两档 loading 各自置位与复位，软刷新结束只清 `isRefreshing`，不误动 `isLoading`。

## 合并视图的会话级抑制集合（R4 的必要条件）

合并视图下「就地更新」会被服务端旧状态撤销：屏蔽只写 `_source_file` 一个文件（`resolveTargetFile()`），而同一商品可能同时存在于同组多个结果文件里（线上只读核对：`1086388785081` 在 `iPad_Air_11_256=active`、`iPad_Air_M4/Air=hidden`；`item_id` 跨文件重复最多 4 个文件）。软刷新重新合并去重后，active 副本会把这个商品渲染回来——用户看到「屏蔽后又冒出来」。

处理（本任务范围内、最小实现）：composable 内维护 `locallyHiddenIds: Set<string>`（键为 `商品信息.商品ID`，与去重键一致）：

- `applyLocalVisibility(item, 'hidden')` 时加入；`applyLocalVisibility(item, 'active')` 时移出。
- `fetchResults()` 落地前过滤：单文件视图与合并视图统一执行 `items.filter(i => !locallyHiddenIds.has(商品ID))`，硬刷新（切文件/筛选）同样过滤。
- 生命周期 = composable 实例（离开结果页或刷新浏览器即失效）；失效后商品是否复活取决于服务端的持久化语义，**那是并行任务 `09-25-fix-blocked-item-reappear-merged-view` 的范畴**，不在本任务解决，也不因本任务恶化。
- 该集合不写入任何持久化存储（localStorage 等）：它只是「本次会话别再冒出来」的 UI 兜底，避免掩盖服务端语义问题。

## i18n 变更

删除（两侧）：`results.card.blockReasonUnmatched`、`blockReasonSeller`、`blockReasonUsage`、`blockReasonShipping`、`blockReasonPrice`；`blockAndRecord`、`blockOnly` 合并为 `blockConfirm`。

新增（两侧）：`blockReasonTitle`（改为提示语，如「屏蔽理由（可留空，回车创建）」）、`blockReasonPlaceholder`（「输入理由，回车创建」）、`blockReasonRecent`（「最近使用」）、`blockReasonRemove`（删除理由的 aria-label，或复用 `removeTag`）。删除后先 `grep -rn "blockReason" web-ui/src` 确认无残留引用。

## 兼容性与回滚

- 无数据迁移：理由仍写 `_user_tags`，旧数据（历史预设中文理由已作为普通标签存着）照常显示与筛选。
- 回滚 = `git revert` 该提交 + `cd web-ui && pnpm build`。无运行时状态需要清理。
- `dist/` 是服务端直接读取的产物：改动后必须 build，否则线上（8000）看到的还是旧 UI。

## 验证策略

- 构建门禁：`cd web-ui && pnpm build`（`vue-tsc -b && vite build`，类型错误即失败）。项目无前端单测设施（`package.json` 无 test script），不新引入测试框架。
- 手工 GUI 验收（浏览器）：浮层锚定与不裁切、右列/末行/375px 三种边界、`Esc` 不提交、空理由直接屏蔽、理由落进标签、已屏蔽项直接取消屏蔽。用结果页真实数据进行，验收截图存 task 目录。
- 就地更新与软刷新（AC8/AC9）的验收方法：DevTools 打开 Network 面板并按 `results` 过滤——屏蔽一条商品时应只看到 `PATCH .../status`（有理由时多一条 `PATCH .../annotation`），**不应**出现 `GET /api/results/{file}`；同时观察页面无骨架屏、被屏蔽卡片直接消失（或灰化）、同屏已展开的 AI 理由与正在编辑的标签不被打断。软刷新用筛选栏「刷新」按钮验证：卡片保持可见 + 进度提示；再连续屏蔽 5 条，等约 1s 让收敛完成，比对列表条数/顺序与刷新后一致（`include_hidden` 开/关各一次）。
- 后端基线：`.venv/bin/python -m pytest tests/ -s` 与基线一致（本任务不应触及后端）。

## 风险与备选

| 风险 | 处理 |
| --- | --- |
| `Card` 的 `overflow-hidden` 裁切浮层 | Portal 到 body（本设计的先决条件） |
| 鼠标移向浮层时触发按钮隐藏 | `open` 状态强制 `sm:opacity-100` |
| 理由含逗号被 TagsInput 分隔 | 关闭分隔符切分 |
| 窄视口浮层出屏 | `max-w-[calc(100vw-1rem)]` + `max-h` 可滚动 |
| 屏蔽后卡片被移出列表、portal 残留 | 先关浮层再 emit；portal 随组件卸载 |
| 就地更新与服务端判定不一致（黑名单规则命中、非 active 排序） | 前端只复刻手动屏蔽语义；差异由防抖软刷新收敛，AC9 验证最终一致 |
| 软刷新响应把刚屏蔽的卡片复活 | `mutationSeq` 序号校验，过期响应丢弃并重排收敛 |
| 软刷新与切文件/切筛选竞态 | 硬加载前 `cancelReconcile()`；防抖回调校验发起时的文件名 |
| 合并视图下商品被其他文件的 active 副本复活 | 会话级 `locallyHiddenIds` 抑制集合（见上一节），R4 起对软/硬刷新统一过滤；持久化语义留给并行任务 |
| TagsInput 受控行为不达预期 | 退回"input + chips"同构实现（验收标准不变） |