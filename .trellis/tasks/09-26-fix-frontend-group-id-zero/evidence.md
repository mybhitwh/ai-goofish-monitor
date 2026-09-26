# 验证证据（AC 逐条）

> 采集时间：2026-09-26 12:26–12:47（CST）。改动本身是两处单行判定 + `pnpm build` 产物重建。
> 生产数据现状（只读）：`GET /api/tasks` → 2 条任务 `id=0/1`、`group_id` 均为 `0`、`cron='0 9,21 * * *'`、`enabled=false`、`next_run_at=null`；`GET /api/groups` → 唯一组 `id=0`「iPad Air 8」`serial/enabled=false`。

## AC1 `group_id=0` 显示组徽章/组名/组 cron（其它数字正常）

**（a）源码执行级改前/改后对照**（独立质检子代理用 esbuild 剥离类型后直接执行真实 `<script setup>` 源码，喂真实 API payload，非重写逻辑）：

| 取值 | HEAD（改前） | worktree（改后） |
| --- | --- | --- |
| `group_id=0` badge | `null`（整块不渲染） | `iPad Air 8` |
| `group_id=0` cronText | `MANUAL` | `0 9,21 * * *` |
| `group_id=0` countdown | `tasks.table.manualTrigger` | 组倒计时（组启用）/ `tasks.table.disabled`（组禁用） |
| `group_id=null` | 回退任务自身 | 与改前逐项一致 |
| `group_id=1` | `Other` | `Other` |

**（b）实时页面 DOM 断言（改后）**：只读 headless Chrome 打开 `http://127.0.0.1:8080/goofish/tasks`（`createWebHistory` 基路径是 `/goofish/`，直连 `:8000` 根会 404 资源）。

`iPad Air M4 256G 全国包邮` 所在 `tr` 的可见文本：

```
IDLE | iPad Air 8 | · 串行执行 | iPad Air M4 256G 全国包邮 | AI ENGINE | ipad air 8 | …… | 0 9,21 * * * | 已禁用 | 3P | 启动
```

- 组徽章与组名 `iPad Air 8` ✅、串行模式文案 `· 串行执行` ✅ —— 这两项是组独有信息，改前 `resolveGroup` 返回 null 时整块不渲染。
- 不含「等待调度」✅、不含「手动」✅（AC1 要求的「不再回退到任务自身 cron」）。
- 徽章取证来自该行第 2 个 `td` 的 HTML：`div.inline-flex … border-violet-200 bg-violet-100 text-[10px] … font-black text-violet-700` + lucide `layers` 图标，与 `TasksTable.vue:389-400` 模板逐项对应 → 确认是组徽章，不是其它元素凑出的同名文本。

**（c）截图**：`evidence/after-tasks-list-group-badge.png`（1680×1050 全页；两条任务均可见组徽章与「0 9,21 * * *／已禁用」触发规则列）。

**口径说明**：生产数据里两条任务的 `cron` 恰好等于组 cron 且任务/组都 disabled，所以本次肉眼可判读的差异是「组徽章/组名/串行模式出现」与「不再显示 MANUAL／等待调度」；「组 cron 覆盖任务自身 cron」的可见差异要等组启用后才出现（(a) 的探针已覆盖该分支）。

## AC2 `group_id=null` 不回归

(a) 表中 `null` 一列改前/改后逐项一致。生产库当前 2 条任务 `group_id` 均为 0，**无 null 样本可做界面实测**，故以源码执行级对照为准（对应后端 `09-25-fix-risk-control-stop-loss` 的 `test_task_without_group_keeps_individual_job` 一侧语义）。

## AC3 grep 复核

```
$ grep -rn '!task.*group_id\|!.*\.group_id' web-ui/src
web-ui/src/components/tasks/TaskGroupDialog.vue:256:  v-if="task.group_id != null && task.group_id !== group.id"
```

唯一命中是正确写法（正则把 `!=` 的 `!` 与后半段 `.group_id` 连读造成的假阳性）。另独立普查 17 处 `group_id` 用法：`TaskForm.vue:172` 用 `??`、`:265` 用字符串哨兵、`useResults.ts:121/235` 严格比较、`TaskGroupDialog.vue:54/74` 严格比较 —— 无第三处假值判断。

## AC4 构建

- `cd web-ui && pnpm build`（`vue-tsc -b && vite build`）rc=0、无类型错误；`dist/` mtime 12:28 晚于源码 12:27:52。
- 产物内含 `group_id==null?null:…` 形态，全 `dist/` 无 `!…group_id`；质检方重跑构建得到**逐字节一致**的产物（确定性构建）。
- 运行中的 SPA 服务的正是修复后的 chunk（`curl 127.0.0.1:8080/goofish/assets/useTasks-CyjKNnzA.js` 输出修复形态），浏览器加载的 `index-CahpIE8Y.js` 亦为当次构建产物。
- `tests/test_frontend_build_paths.py` 失败与基线同因（`.dockerignore:13` 含 `web-ui/dist`），测试断言与 `.dockerignore` 均未改动。

## AC5 测试

`.venv/bin/python -m pytest tests/ -s -q` → `3 failed, 145 passed, 3 skipped, 1 warning in 4.02s`（151 收集）。失败三项与基线同名同因：`test_frontend_build_paths`、`tests/unit/test_task_group.py::test_group_update_partial_apply`、`tests/unit/test_utils.py::test_save_to_jsonl`。

## AC6 证据落盘

本文件 + `evidence/after-tasks-list-group-badge.png`；AC 勾选见 `prd.md`。另按 Phase 3.3 把本轮两条教训写进 `.trellis/spec/backend/quality-guidelines.md`（见下）。

## 未覆盖 / 边界（如实标注）

- **无「改前界面截图」**：改前产物已被覆盖；不做「stash → 重建 → 截图 → 回滚」的双次构建（会短暂把生产 `dist/` 退回旧逻辑）。改前行为以 (a) 源码执行级对照为准。
- **未构造「组已启用」运行态**（需改生产状态，超出边界）：该分支以实时 payload 喂探针覆盖。
- **运行时状态未被触碰**：`data/app.sqlite3`、`state/`、`.env`、`config.json`、`logs/` 的 mtime 均早于改动窗口（12:26–12:47），未重启 systemd、未改 `.dockerignore`。
- **窗口内唯一新增文件**是测试写入的 `prompts/apple_watch_s10_criteria.txt`（41 字节桩字符串），已确认为测试残渣（不属于任何生产任务）并删除；根因与修法已记入 `quality-guidelines.md` §3。
- **相邻观察（不属本任务，未改）**：`TasksTable.vue:496` 的倒计时外框样式只认任务自身 `cron`/`enabled`，在「组启用而任务自身禁用」时外框配色可能与内文色调不一致；纯样式，不影响 AC1 结论。
- **浏览器自动化口径**：本会话未暴露 browser-use 所需的 `mcp__node_repl__js` MCP 工具（`~/.zcode/cli/config.json` 无 MCP 配置），故改用仓库自带 Playwright + 系统 Chrome 做只读 DOM 断言与截图。