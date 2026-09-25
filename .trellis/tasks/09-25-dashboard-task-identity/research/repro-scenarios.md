# 复现场景与实测输出

全部结论来自临时库中运行**真实生产代码**（不是推演）。脚本见同目录 `repro_dashboard_identity.py`，用临时目录 + `APP_DATABASE_FILE` 隔离，不触碰 `data/app.sqlite3`。

预置说明：每个场景里都会出现若干 `task_id=None` 的条目，那是「结果文件先于任务存在」（引导导入或外部工具写入）造成的无主条目——本身就属场景 E。阅读时请只看与当前场景相关的行。

## 场景 A：两个任务同名、keyword 不同 → 合并 + 数据被吞

创建 `同名任务`（kw_a）与 `同名任务`（kw_b），磁盘上分别有 1 条、2 条记录：

```
任务管理条数=2   概览 task_summaries 条数=3   enabled_tasks=2   result_files=3   scanned=8
   | task_id=0  name='同名任务' file=kw_a_full_data.jsonl items=1
```

两个任务只剩 1 条摘要；`kw_b_full_data.jsonl` 的 2 条记录**从所有汇总里消失**（`result_files` 只数到 3 个文件，`scanned` 少了 2 条）。同时 `enabled_tasks=2` 与「监控任务」计数直接矛盾。

根因：`task_summaries` 以 `task_name` 为 key，`src/services/dashboard_service.py:39-41` 与 `:48` 两次都用展示名。

## 场景 B：只改 keyword → **不会**产生幽灵

`改keyword任务`（old_kw，3 条记录）把 keyword 改成 `new_kw`：

```
改之前: task_id=2 name='改keyword任务' file=old_kw_full_data.jsonl items=3
改之后: task_id=2 name='改keyword任务' file=old_kw_full_data.jsonl items=3   # 完全没变
```

原因：`_resolve_task` 的第二轮「按任务名称回退匹配」（`src/services/dashboard_payloads.py:122-126`）把老文件重新挂回了同一个任务。**这里纠正一个常见误判：改 keyword 本身不会造幽灵条目**，幽灵需要两个匹配键同时失效。

## 场景 C：只改 task_name → 安全

keyword 不变、把 `旧名任务` 改成 `新名任务`：条目跟着改名（`task_id=3 name='新名任务' file=rename_kw_full_data.jsonl items=4`），无幽灵。

## 场景 B2：任务名与 keyword **同时**改 → 幽灵

```
改之前: 任务管理=3  task_summaries=3
改之后: 任务管理=3  task_summaries=4
   | task_id=None name='改名前任务' file=both_old_full_data.jsonl items=3   ← 幽灵
   | task_id=2    name='改名后任务' file=None items=0
```

与线上报告现象同形：概览比任务管理多 1。

## 场景 D：改过 keyword 之后再删除任务 → 残留永久留下

复刻 `DELETE /api/tasks/{id}` 的清理逻辑（`src/api/routes/tasks.py:248-258`，按**当前** keyword 清理）：

```
删除任务后按其当前 keyword 清理: 删除 del_kw_new_full_data.jsonl 的 0 行（旧 keyword 的 del_kw_full_data.jsonl 未处理）
任务管理=3  task_summaries=4
   | task_id=None name='待删除任务' file=del_kw_full_data.jsonl items=4     ← 幽灵
```

这是代码级漏洞：任务一旦改过 keyword，删除时清的是新 keyword 的文件，旧文件永远成为无主数据。

## 场景 E：非任务数据（线上真实发生）

`feed_scan` 工具直接写库、从未注册为任务，任务删除路径也不会覆盖它 → 长期占用一个「监控任务」名额。线上见 `root-cause-and-live-evidence.md`。

## 汇总：触发条件

| 场景 | 计数偏差 | 根本触发条件 |
|---|---|---|
| A 同名 | 偏少（合并），连带丢数据 | `task_name` 作身份 |
| B2 名字+keyword 同改 | 偏多（幽灵） | 两个匹配键都失效 |
| C 只改名 | 无 | 名称回退救回 |
| B 只改 keyword | 无 | 名称回退救回 |
| D 改 keyword 后删除 | 偏多（幽灵） | 删除按当前 keyword 清理 |
| E 外部/引导数据 | 偏多（幽灵） | 结果文件先于任务存在 |

**不变量缺失**：没有任何一层保证「一个结果文件恰好归属一个任务」，也没有保证「`task_summaries` 条数 == 任务数」。