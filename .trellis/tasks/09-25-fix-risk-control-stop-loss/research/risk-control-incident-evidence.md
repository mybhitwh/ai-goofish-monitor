# 事故原始证据（2026-09-25 闲鱼风控）

本文件只记录可复核的原始证据与复现命令，供实现者核对，不重复 PRD 的结论。

## 1. 日志：风控命中分布

```bash
cd /home/myb/code/ai-goofish-monitor
for f in logs/iPad_Air_M4_256G_1.log logs/iPad_Air_M4_256G_11_3.log logs/iPad_Air_M4_256G_Air8_2.log; do
  echo "=== $f: 共 $(grep -c 'CRITICAL BLOCK' "$f") 次"
  awk '{if (match($0,/\[ 2026-09-25 ([0-9][0-9]):/)) {ts=substr($0,RSTART+13,2)} if (/CRITICAL BLOCK/) {n[ts]++}} END {for (h in n) print "  "h" 时: "n[h]" 次"}' "$f" | sort
done
```

结果：

- `iPad_Air_M4_256G_1.log`（任务 1 上海）：32 次 —— 17 时 15 / 18 时 9 / 21 时 8
- `iPad_Air_M4_256G_11_3.log`（任务 3 11 寸）：34 次 —— 17 时 17 / 18 时 13 / 21 时 4
- `iPad_Air_M4_256G_Air8_2.log`（任务 2 Air8）：33 次 —— 17 时 15 / 18 时 15 / 21 时 3
- 三个文件的首次命中时间分别为 17:50:58 / 17:51:06 / 17:51:08 —— **同一分钟并发命中**，与"4 个单任务 job 并行启动"一致。

单次任务内连续命中的原文（任务 1，21:27–21:30）显示每个新商品各命中一次，任务收尾为：

```
[ 2026-09-25 21:30:53] 等待后台分析任务完成...
--- 所有任务执行完毕 ---
任务 'iPad Air M4 256G 上海包邮或自取' 正常结束，本次运行共处理了 0 个新商品。
```

## 2. 调度：一次触发注册了 5 个 job

```bash
journalctl -u goofish --since "20:45" --until "21:06" --no-pager | grep -vE '"(GET|POST|PATCH) /assets|WebSocket'
```

20:48:05 的 `reload_jobs`（应用启动）：

```
-> 已为任务 'iPad Air M4 256G 全国包邮' 添加定时规则: '0 9,21 * * *'
-> 已为任务 'iPad Air M4 256G 上海包邮或自取' 添加定时规则: '0 9,21 * * *'
-> 已为任务 'iPad Air M4 256G 全国包邮·Air8写法' 添加定时规则: '0 9,21 * * *'
-> 已为任务 'iPad Air M4 256G 全国包邮·11寸写法' 添加定时规则: '0 9,21 * * *'
-> 已为任务组 'iPad Air 8' 添加定时规则: '0 9,21 * * *' (模式: serial)
```

21:04:37 一次触发：4 条 `定时任务触发: 正在为任务 '...' 启动爬虫...` → 4 个 PID（68114/68116/68118/68120）→ 组循环 `任务组 'iPad Air 8' 触发: 4 个任务, 执行模式: 串行` → `任务 '...全国包邮' (ID: 0) 已在运行中`。

事后串行循环继续逐个拉起（说明组循环与单任务 job 相互独立）：

```
21:28:29 启动任务 'iPad Air M4 256G 上海包邮或自取' (PID: 77401)
21:30:58 启动任务 'iPad Air M4 256G 全国包邮·Air8写法' (PID: 79087)
21:34:08 启动任务 'iPad Air M4 256G 全国包邮·11寸写法' (PID: 80094)
```

## 3. 熔断器状态（事故中）

```bash
cat logs/task-failure-guard.json
```

4 个任务全部为 `consecutive_failures: 0`、`paused_until: null`、`cookie_path: null`、`last_failure_reason: null`，而 `last_success_at` 被刷新到 `2026-09-25T21:30:58`。

## 4. 代码锚点（修复前）

| 位置 | 现状 |
|------|------|
| `src/services/scheduler_service.py:92` | `group = group_map.get(task.group_id) if task.group_id else None` |
| `src/scraper.py:1035` | `raise RiskControlError("FAIL_SYS_USER_VALIDATE")`（先 `sleep(3~60)`） |
| `src/scraper.py:1156-1157` | `except Exception as e: print(f"   错误: 处理商品详情时发生未知错误: {e}")` —— 吞掉上者 |
| `src/scraper.py:122-127` | `pause_immediately` 仅含"未找到可用的代理地址/登录状态文件" |
| `src/scraper.py:1299` | `FAILURE_GUARD.record_success(task_name_for_guard)`（尝试正常返回即清零） |
| `src/scraper.py:1305-1308` | `except RiskControlError: last_error = str(e); break`（不直接暂停，依赖 `_notify_task_failure` 的阈值 3） |
| `src/scraper.py:1288,1313` | 写侧 `last_state_path` 已正确传入 `_notify_task_failure` |
| `src/services/process_service.py:54-63` | 读侧退回 `STATE_FILE`（`src/config.py:11` → `xianyu_state.json`，该文件不存在） |
| `src/services/item_recheck_service.py:290-294` | 复核已在 `RISK_CONTROL` 时中止本轮（无需修改） |

## 5. 环境事实

- `logs/task-failure-guard.json` 与 `logs/ai/` 属 **root**（`goofish.service` 以 root 运行），普通用户无法写入；线下核对该文件请用 `cat`，不要用脚本改写。
- `xianyu_state.json` 在仓库根目录**不存在**；实际登录态为 `state/acc1.json`（`account_strategy=auto`）。
- 事故期间任务组与 4 个任务已置 `enabled=false`，`next_run_at` 全为 `null`；`sudo systemctl restart goofish` 需用户执行。