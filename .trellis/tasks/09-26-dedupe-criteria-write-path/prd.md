# criteria 写入逻辑去重（routes/tasks.py PATCH 分支与服务层单点化）

## Goal

criteria 文件的落盘逻辑在仓库里有**两份实现**：`src/api/routes/tasks.py` 的 PATCH 重新生成分支（内联实现，无测试覆盖）与 `src/services/task_generation_runner.py` 的 `build_criteria_filename` / `save_generated_criteria`（生成作业路径，有测试覆盖且已做过 CWD 隔离修复）。本任务把它收敛到服务层单点，路由只做参数解析与错误映射（符合 `AGENTS.md`「项目概览」的分层约定 API → services → domain → infrastructure），并给 PATCH 分支补上集成用例。

## 现象与证据

| 位置 | 现状 |
| --- | --- |
| `src/api/routes/tasks.py:190-215` | 内联实现：手工拼 `safe_keyword` → `f"prompts/{safe_keyword}_criteria.txt"` → `os.makedirs("prompts", exist_ok=True)` → `aiofiles.open(...)` 写入 → 赋值 `task_update.ai_prompt_criteria_file`；含 `try/except` + `traceback` 打印，空内容与异常分别映射 500 |
| `src/services/task_generation_runner.py:14-19, 46-52` | 同一件事实现在服务层：`build_criteria_filename()` + `save_generated_criteria()`（空内容 `raise RuntimeError`），供生成作业使用 |
| 重复的代价 | 两处的文件名构造与落盘语义各写一遍；改一处忘另一处会分叉。且 routes 那份**无测试覆盖**——将来补测试时会再次踩到「相对 CWD 写入仓库 `prompts/`」的坑（该坑的机理与修法见 `.trellis/spec/backend/quality-guidelines.md` §3 与任务 `09-26-fix-test-prompts-write-isolation`） |

## Requirements

### R1 收敛到服务层单点

- 路由 PATCH 分支改为调用服务层同一函数（复用 `build_criteria_filename` + `save_generated_criteria`），删除内联重复实现。
- 必须保留的语义：HTTP 400（`description_for_ai` 为空）/ 404（任务不存在）/ 500（AI 返回空内容、落盘异常）口径不变；`ai_prompt_criteria_file` 仍写**相对路径字符串**；`prompts/` 的 CWD 语义不变（不在本任务改成可注入）。
- 分层检查：SQL/文件落盘留在服务层，路由只做参数与错误映射（参考 `.trellis/spec/guides/cross-layer-thinking-guide.md`）。

### R2 给 PATCH 分支补集成用例

- 成功路径：桩掉 `generate_criteria`，PATCH 触发重新生成，断言任务字段被更新为 `prompts/<safe_keyword>_criteria.txt` 且文件真的写出来了。
- 失败路径：AI 返回空内容 → 500，且任务字段不被改动。
- **新用例必须做 CWD 隔离**（`monkeypatch.chdir(tmp_path)`），否则会把桩文件写进仓库 `prompts/`。

## Acceptance Criteria

- [ ] AC1 仓内只剩一份 criteria 落盘实现 → 取证：`grep -rn 'os.makedirs("prompts"' src/` 仅命中服务层；`grep -rn '_criteria.txt' src/api/routes/` 不再出现文件名字面拼装
- [ ] AC2 新增 PATCH 分支用例（成功 + 失败）通过；跑完全量后 `prompts/` 无新增文件、`prompts/ipad_air_m4_criteria.txt`（线上任务 id 0/1 的真实产物）mtime 与哈希不变
- [ ] AC3 错误语义未变：空 `description_for_ai` → 400、任务不存在 → 404、AI 空返回 → 500（逐条用例或实测取证）
- [ ] AC4 全量结果如实记录新基线（新增用例会使 collected 总数上升），并同步 `.trellis/spec/backend/quality-guidelines.md` §2 与 `AGENTS.md` 的测试基线条
- [ ] AC5 未触碰 `data/`、`state/`、`.env`、`logs/`、`config.json`

## Non-goals

- 不把 `prompts/` 目录改成可注入配置（独立重构，本任务只去重）。
- 不改前端（界面文案与交互不变）。
- 不改生成作业（`run_ai_generation_job`）的既有行为与日志打印，除必要的调用点替换。

## 相邻已知项

- 服务层单点化之后，`save_generated_criteria` 的「相对 CWD」性质仍需在 spec §3 的提示语里保留（生产 CWD=仓库根是设计）。
- 激活前若判定为复杂任务，补 `design.md`（要点：服务层函数签名取舍、路由错误映射的保留方式）与 `implement.jsonl` / `check.jsonl` 清单。