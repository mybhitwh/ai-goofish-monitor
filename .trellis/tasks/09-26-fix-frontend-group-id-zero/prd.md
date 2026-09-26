# 修复任务列表的组 id=0 假值判断（组徽章与调度信息不显示）

## Goal

任务列表把「组 id=0」当成「无组」：`TaskGroup` 的主键 `id` 从 0 开始，而线上唯一的任务组 id 恰为 0，于是该组的成员任务在列表里看不到组徽章、组 cron 与组倒计时。与后端 `09-25-fix-risk-control-stop-loss` 的 R1 同源（同一类假值判断），本任务在前端收口。

## 现象与证据（2026-09-26 实测）

| 位置 | 现状 | 后果 |
| --- | --- | --- |
| `web-ui/src/components/tasks/TasksTable.vue:53-54` | `if (!task.group_id) return null` | `resolveGroup()` 对 `group_id=0` 返回 null → 组徽章不渲染；组 cron 段与倒计时回退到任务自身 cron（`:83-116`、`:152`、`:389` 一带消费同一个 `resolveGroup`） |
| `web-ui/src/composables/useTasks.ts:68-69` | `if (!task?.group_id) return null` | `groupName` 计算同样失效（当前无消费者，影响面为零，但属同一缺陷） |
| `web-ui/src/components/tasks/TaskGroupDialog.vue:54,256` | `task.group_id != null` | 已是正确写法，可作为本仓范式 |
| `web-ui/src/views/TasksView.vue:258-264` | 赋值用 `member ? groupId : null` | 本身无假值问题；确认组归属语义是 `null`（无组）vs 数字（含 0） |

叠加影响（R1 已修复的部分）：组内任务不再单独注册调度 job，`group_0` 成员的 `next_run_at` 为 `null`。列表因此显示任务自身 cron + 「等待调度」，与「由组统一触发」不符——运维看板上会误以为任务没被调度。

生产实例现状（只读核对）：`tasks` 2 条（id 0/1）`group_id` 均为 0，`task_groups` 仅 1 条 id=0、`cron='0 9,21 * * *'`、`serial`、当前 `enabled=0`。

## Requirements

### R1 判定改为「非 null 即属于组」

- `TasksTable.vue` 的 `resolveGroup` 与 `useTasks.ts` 的 `groupName` 一律改为 `group_id != null`（或 `??`/可选链等价写法），与 `TaskGroupDialog.vue` 的既有写法保持一致。
- 只改判定，不改数据契约：`group_id: number | null` 的语义不变（`null`=无组、数字=组 id，**0 是合法 id**）。
- 顺带全仓扫一遍同类假值：`grep -rn '!task.*group_id\|!.*\.group_id' web-ui/src`，把剩余命中一并判断（应只剩已修的两处）。

### R2 构建产物同步

- 改完 `cd web-ui && pnpm build`，`dist/` 必须与源码一致（SPA 服务直接读产物）。

## Acceptance Criteria

- [x] 任务列表中，`group_id=0` 的任务显示组徽章、组名与组 cron（不再是任务自身 cron +「等待调度」）；`group_id=null` 的任务仍显示为无组；`group_id` 为其他数字的组同样正常。→ 实时页面 DOM 断言：行内出现 `iPad Air 8 · 串行执行`、无「等待调度」/「手动」；改前/改后源码执行级对照见 `evidence.md`（AC1）
- [x] `group_id=null` 的任务行为不回归（不显示任何组信息）。→ 对照表中 `null` 一列改动前后逐项一致（生产库无 null 样本，见 `evidence.md` AC2）
- [x] `grep` 复核：`web-ui/src` 内不再有 `!task.group_id` / `!task?.group_id` 形态的判定。→ 唯一命中 `TaskGroupDialog.vue:256` 的正确写法；另普查 17 处 `group_id` 用法无第三处假值
- [x] `cd web-ui && pnpm build` 通过；`tests/test_frontend_build_paths.py` 状态与基线一致（该用例为存量失败，属配置漂移，不得为它改断言）。→ `vue-tsc -b && vite build` rc=0，`dist/` 含修复形态且重建逐字节一致；该用例失败同因基线，断言与 `.dockerignore` 未动
- [x] `.venv/bin/python -m pytest tests/ -s` 不劣于基线（145 passed / 3 failed / 3 skipped）。→ 实测 `3 failed, 145 passed, 3 skipped in 4.02s`，失败三项与基线同名同因
- [x] 验证证据落 `check.jsonl` 或完成说明：改前/改后的界面截图或 DOM 断言（取值 `group_id=0` 的任务）。→ `evidence.md` + `evidence/after-tasks-list-group-badge.png`（改前以源码执行级对照替代界面截图，理由见 evidence 末尾）

## Non-goals

- 不改后端调度与 `next_run_at` 语义（属 `09-25-fix-risk-control-stop-loss`，已落地）。
- 不改 `TaskGroupDialog.vue` / `TasksView.vue` 中已经正确的判定。
- 不改列表其余字段的渲染逻辑。

## 相关任务

- `09-25-fix-risk-control-stop-loss`（已提交 `5598103`）：同类假值判断的后端修复（`scheduler_service.py` 的 `group_id is not None`）。本任务是它在 UI 侧的收口，**验证时由它的检查子代理发现**。

## 相邻已知项（同轮验证发现，未处理，另行评估）

1. **`pause_immediately` 的隐式契约**：熔断标记匹配的是异常文案子串，改 `raise RiskControlError("...")` 的文案而不改标记会让熔断静默失效。已写进 `.trellis/spec/backend/error-handling.md` §3.2 的 Warning；可选加固是把它提为模块级常量。
2. **`tests/unit/test_scraper_risk_control.py` 的 AST 结构用例偏脆**：断言全文件「恰好 1 个」受保护的详情循环，合理的重构（抽 helper、新增第二条循环）会误伤；建议放宽为「所有匹配的 try 都满足顺序 + 裸 raise」。
3. **R5 未覆盖子进程路径**：`spider_v2.py` 直接运行时，进程内预检仍只用 `account_state_file`/`STATE_FILE`，任务未配置时拿不到 guard 记忆路径（正常调度路径下被父进程先解除暂停所掩盖，影响低）。