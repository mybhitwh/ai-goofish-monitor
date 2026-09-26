# 隔离测试对仓库 prompts/ 的写入（全量跑不再留桩文件）

## Goal

全量跑 `pytest tests/ -s` 会在**仓库根**留下 `prompts/<keyword>_criteria.txt` 桩文件（测试残渣）。本任务把这条写路径在测试内隔离，使全量跑不再改动仓库树；生产路径语义保持不变。

## 现象与证据

| 环节 | 现状 |
| --- | --- |
| 触发用例 | `tests/integration/test_api_tasks.py:78` `test_generate_ai_task_returns_job_and_completes_async`（POST `/api/tasks/generate`，keyword=`apple watch s10`，用例内 stub 掉 `generate_criteria`） |
| 文件名构造 | `src/services/task_generation_runner.py:15-19` `build_criteria_filename()` → `prompts/{safe_keyword}_criteria.txt`（相对路径字符串；DB 的 `ai_prompt_criteria_file` 也存这个相对值） |
| 落盘点 | `src/services/task_generation_runner.py:48-52` `save_generated_criteria()`：`os.makedirs("prompts", exist_ok=True)` + `aiofiles.open(output_filename, "w")` —— **相对进程 CWD** |
| 后果 | 从仓库根跑 pytest → 仓库根 `prompts/` 出现 41 字节桩文件（2026-09-26 实测名 `apple_watch_s10_criteria.txt`，内容为 stub 字符串）。`prompts/` 整目录被 `.gitignore` 忽略，`git status` 看不见它 → 有混入提交或误删生产产物的风险 |

**生产语义必须保留**：`prompts/` 是生产资源目录——`prompts/ipad_air_m4_criteria.txt` 是线上任务 id 0/1 的真实产物（两任务的 `ai_prompt_criteria_file` 均指向它，2026-09-26 只读核对）；生产服务 CWD=仓库根（systemd 单元），相对路径是既有设计，本任务不改。

## Requirements

### R1 测试内隔离写入

- 在触发污染的用例内 `monkeypatch.chdir(tmp_path)`（`.trellis/spec/backend/quality-guidelines.md` §3 认可的既有姿势），使相对 CWD 的写入落到临时目录。
- 只改测试；**不动** `src/` 的路径语义，也不改 DB 里 `ai_prompt_criteria_file` 的相对值契约。

### R2 全量跑后仓库树无新增

- 全量跑完 `prompts/` 不新增文件；`prompts/ipad_air_m4_criteria.txt` 的内容与 mtime 不变。

## Acceptance Criteria

- [x] AC1 全量 `.venv/bin/python -m pytest tests/ -s` 跑完，`ls prompts/` 与跑前一致（无新增），且 `ipad_air_m4_criteria.txt` 的 mtime 未变 → 取证：检查子代理实测跑前后 `ls -a --time-style=full-iso prompts/` 逐字节一致（含隐藏文件）；该文件 mtime 仍 `2026-09-25 13:32:30.751302476 +0800`、sha256 `d9f78c20…4060` 不变；桩文件实际落在 `/tmp/pytest-of-myb/pytest-34/…/prompts/apple_watch_s10_criteria.txt`（41 字节，内容与既有污染指纹一致——说明不是靠「不写」换绿）
- [x] AC2 触发用例隔离后仍真实覆盖原行为、断言未被削弱 → 取证：`git diff -U3 -- tests/` 仅 +2/−1（签名加 `tmp_path`、函数体首行 `monkeypatch.chdir(tmp_path)`），零断言改动；检查方另做两次变异反证——把 `save_generated_criteria` 置为抛错 → 用例失败于轮询超时（`:117`）、把 `build_criteria_filename` 改后缀 → 失败于 `assert …endswith("_criteria.txt")`（`:120`），两次变异写入仍落在 tmp
- [x] AC3 全量结果不劣于基线，失败同名同因 → 取证：`.venv/bin/python -m pytest tests/ -s` → `3 failed, 145 passed, 3 skipped, 1 warning in 3.92s`；三项失败为 `tests/test_frontend_build_paths.py:31`、`tests/unit/test_task_group.py:76`（NameError）、`tests/unit/test_utils.py:54`；3 个 skip 仍全是 live 冒烟
- [x] AC4 未触碰运行态 → 取证：文件级 mtime 跑前后全等（`data/app.sqlite3` 1790386380、`state` 1790317148、`logs` 1790386380、`.env` 1790316971、`config.json` 1790314014），并比对 `data/app.sqlite3` 的 sha256 与 size（5853184）不变；`data/` **目录** mtime 跳动经二分定位为既有 `test_process_service` 打开生产库所致（与本改动无关），已记入 spec §3 存量破例

## Non-goals

- 不改 `src/api/routes/tasks.py:201-215` 那份**重复**的 criteria 写入实现（PATCH 重新生成分支；当前无测试覆盖、不产生污染）——记为相邻项。
- 不把 prompts 目录改成可注入配置（改它属生产路径重构，与本任务的价值不成比例）。
- 不清理 `prompts/ipad_air_m4_criteria.txt`（线上产物，禁止删除/覆盖）。

## 相邻已知项（另行评估）

1. `src/api/routes/tasks.py` 与 `src/services/task_generation_runner.py` 的 criteria 写入逻辑重复，值得合并为服务层单点。
2. 若将来给「PATCH 重新生成 criteria」补测试，同样需要 CWD 隔离（同一写法的第二个入口）。