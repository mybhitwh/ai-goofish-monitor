# 实施计划：屏蔽理由浮层 + 屏蔽就地更新

前置：task 状态 `in_progress`（`task.py start`）后执行。改动仅限 `web-ui/`，每个阶段结束可独立回滚。

## 阶段 0：动手前

- [ ] 读 `.trellis/spec/guides/code-reuse-thinking-guide.md` 与 `cross-layer-thinking-guide.md`（复用 reka-ui 原语、确认理由仍走 `_user_tags` 契约、就地更新复用 `saveItemAnnotation` 的既有范式）。
- [ ] `grep -rn "blockReason\|blockAndRecord\|blockOnly" web-ui/src` 记录全部引用点（预期只有 `ResultCard.vue` 与两个 i18n 文件）。
- [ ] `grep -rn "TagsInput\|Popover" web-ui/src` 确认项目内尚无同类封装，避免重复造组件。
- [ ] `grep -rn "isLoading\|isRefreshing\|fetchResults\|totalItems" web-ui/src` 确认改动面：`ResultsGrid.vue:26` 的骨架屏分支、`ResultsFilterBar.vue:222-245` 的 `disabled`、`ResultsView.vue` 未消费 `totalItems`。

## 阶段 1：i18n（zh-CN / en-US 同步）

- [ ] `web-ui/src/i18n/messages/zh-CN.ts`、`en-US.ts`：删除 `blockReasonUnmatched/Seller/Usage/Shipping/Price`、`blockAndRecord`、`blockOnly`；新增 `blockConfirm`、`blockReasonPlaceholder`、`blockReasonRecent`、`blockReasonRemove`；改写 `blockReasonTitle` 文案为可留空提示。
- [ ] `grep -rn "blockReason" web-ui/src` 应只剩新键；确认无 `t('results.card.blockOnly')` 之类残留。
- 回滚点：单独提交或 `git checkout -- web-ui/src/i18n`。

## 阶段 2：新增 `BlockReasonPopover.vue`

- [ ] `web-ui/src/components/results/BlockReasonPopover.vue`：按 `design.md` 的契约实现（props `candidates` / `open`，emits `update:open` / `confirm`，slot `trigger`）。
- [ ] `PopoverPortal` + `PopoverContent`（`side="bottom"`、`align="end"`、`:side-offset="6"`、`:collision-padding="8"`、宽度/高度上限与内部滚动）。
- [ ] 标签式输入（reka-ui `TagsInput*`，关闭分隔符切分）或兜底 input + chips；`open` 变 true 时重置并聚焦。
- [ ] 候选行渲染 `candidates.slice(0, 8)`；空则整行不渲染。
- [ ] 主按钮「屏蔽」提交（含输入框未回车文本，trim + 去重）；「取消」与 `Esc` 只关闭。

## 阶段 3：改造 `ResultCard.vue`

- [ ] 删除 `blockReasonPresets`、`togglePickedReason`、`confirmBlockOnly`、内联面板模板块（`:272-297`）。
- [ ] `requestBlock()` 语义改为打开浮层；`confirmBlockWithReasons()` 保留 emit `block`。
- [ ] 右上角按钮：已屏蔽 → 原直接取消屏蔽；未屏蔽 → 放进浮层 `#trigger` 插槽；按钮样式串抽常量复用。
- [ ] 浮层 `open` 时触发按钮追加 `sm:opacity-100`。
- [ ] `pnpm build` 的基本检查：`cd web-ui && pnpm build`。

## 阶段 4：`useResults.ts` 就地更新与软刷新（R4 / R5）

- [ ] `applyLocalVisibility(item, 'hidden' | 'active')`：置 `_status` / `_hidden_reason` / `_effective_hidden`；`hidden` 且 `!filters.include_hidden` 时从 `results` 移除并递减 `totalItems`。
- [ ] `locallyHiddenIds: Set<string>`（键 `商品信息.商品ID`）：`applyLocalVisibility` 里增删；`fetchResults()` 落地前统一过滤（软/硬刷新都过），防合并视图被其他文件的 active 副本复活。
- [ ] `blockItem()`：annotation（有理由时）→ status → `applyLocalVisibility(item, 'hidden')` → `scheduleReconcile()`；删掉 `await fetchResults()`。理由标签就地写回 `_user_tags`（用 annotation 响应）。
- [ ] `toggleItemBlock()`：status → `applyLocalVisibility(item, 'active')` → `scheduleReconcile()`；删掉 `await fetchResults()`。
- [ ] `fetchResults({ soft })`：soft 走 `isRefreshing`、不置 `isLoading`；两档 loading 各自复位。
- [ ] `scheduleReconcile(delayMs = 800)`：防抖后软刷新 + `fetchInsights()`；带 `mutationSeq` 序号校验，过期响应丢弃并重排一次；`cancelReconcile()` 在切文件/切筛选时调用。
- [ ] 失败路径：两个函数不再吞错误，向上抛给调用方。
- [ ] `ResultsView.vue`：`handleBlock` 改 `async` + `await` + 失败 toast（照抄 `handleAnnotate`）；`toggleItemBlock` 同样补失败提示；把 `isRefreshing` 透传给筛选栏。
- [ ] `ResultsFilterBar.vue`：新增可选 `isRefreshing` 进度提示，不参与按钮 `disabled`。
- [ ] `pnpm build` 通过。
- 回滚点：`git checkout -- web-ui/src/composables/useResults.ts web-ui/src/views/ResultsView.vue web-ui/src/components/results/ResultsFilterBar.vue`。

## 阶段 5：验证（门禁全过才算完成）

- [ ] `cd web-ui && pnpm build` → 通过（`vue-tsc` 无类型错误）。
- [ ] `.venv/bin/python -m pytest tests/ -s` → 136 collected / 130 passed / 3 failed / 3 skipped（与基线一致；后端未改，出现新增失败即回归）。
- [ ] GUI 手工验收（`.venv/bin/python -m src.app` 后开结果页；有 `dist/` 时后端直接服务构建产物）：
  - [ ] AC1：浮层内无预设理由；`grep` 无旧键残留。
  - [ ] AC2：回车建理由标签、可删、多条；提交后商品被屏蔽且理由出现在标签（用"显示已屏蔽结果 + 标签"核对）；空理由可屏蔽。
  - [ ] AC3：浮层锚定按钮、卡片高度不变、不被裁切。
  - [ ] AC4：最右列、最后一行、375px 视口三种边界下浮层完整可见。
  - [ ] AC5：自动聚焦；`Esc` 关闭不提交；浮层打开时触发按钮可见；已屏蔽项直接取消屏蔽。
  - [ ] AC7：截图存 `.trellis/tasks/09-25-block-reason-free-tag-popover/`（浮层态 + 一条边界场景）。
  - [ ] AC8：DevTools Network 过滤 `results` —— 屏蔽/取消屏蔽只出现 `PATCH .../status`（有理由时含 `PATCH .../annotation`），无 `GET /api/results/{file}`；无骨架屏闪烁；同屏其他卡片的展开态/编辑态存活；`include_hidden` 关 → 卡片消失，开 → 原地灰化；用一次人为失败（例如改错 item_id 或断网易）确认列表不变且有失败提示。
  - [ ] AC9：筛选栏「刷新」按钮不替换卡片、有进度提示；切换文件/筛选仍走骨架屏；连续屏蔽 5 条后停手约 1s，列表条数与顺序与服务端一致（`include_hidden` 开/关各验一次，可用「手动刷新后再看」作为对照）。
  - [ ] 合并视图专项（任务组视图）：屏蔽一条已知跨文件重复的商品（如 `1086388785081`），等待防抖收敛后该卡片**不得**复活；点「刷新」也不复活（会话级抑制集合生效）；离开结果页再回来复活属已知问题，记录到并行任务 `09-25-fix-blocked-item-reappear-merged-view`。
- [ ] 复核：`git diff` 只包含上述前端文件；`dist/` 是否入库按仓库既有口径（`dist/` 已在 `.gitignore`，只保证线上构建产物更新）。

## 提交

- [ ] 提交信息（中文 Conventional Commits）建议拆两个：
  - `feat(web-ui): 结果卡片屏蔽理由改为自由标签 + 按钮旁浮层`（阶段 1-3）
  - `perf(web-ui): 屏蔽/取消屏蔽改为就地更新，刷新不再闪整页骨架屏`（阶段 4）
- [ ] 提交前确认 `data/`、`state/`、`.env` 未被误改。

## 风险回滚

- 阶段 1 / 2 / 3 / 4 各自可 `git checkout` 单文件回退（阶段 4 的回滚文件见该阶段末尾）；整体回退 = revert 提交 + `pnpm build`。
- 若 TagsInput 交互不达验收标准：改用兜底 input + chips（阶段 2 内切换，不扩大范围）。
- 若就地更新的竞态在验收中暴露（卡片复活/错序）：先退回「软刷新」单档（保留 R5、`applyLocalVisibility` 只做灰化不移除）作为降级路径，仍不动后端。