# 隔离 test_process_service 对生产库的打开（消除 data/ 目录 mtime 抖动）

## Goal

`tests/unit/test_process_service.py` 的一个用例会以 WAL 模式打开**生产库** `data/app.sqlite3`，产生又删除瞬时 `-wal`/`-shm` 文件，导致 `data/` **目录** mtime 变化（库文件字节与 mtime 不变）。后果：任何拿「`data/` 目录 mtime 未变」证明「运行态未被触碰」的验证都会失效——2026-09-26 隔离 `prompts/` 写入的质检里就踩到过（`data/` 目录 mtime 从 15:08:18 跳到 15:09:52，逐文件二分才定位到这条用例），险些被记成「运行态被污染」。

## 现象与证据（2026-09-26 实测）

| 项 | 内容 |
| --- | --- |
| 触发用例 | `tests/unit/test_process_service.py:29-74` `test_process_service_marks_task_stopped_when_process_exits` |
| 未桩点 | `src/services/process_service.py:57` 的 `find_task_by_name_sync`（用例只桩了 `build_task_log_path`，见 `tests/unit/test_process_service.py:57-60`） |
| 默认库路径 | `src/infrastructure/persistence/storage_names.py:7` → 仓库根 `data/app.sqlite3` |
| 观测事实 | 单独跑 `tests/unit/` 复现；跑 `tests/integration/` 不复现；`data/app.sqlite3` 的 sha256、size、mtime 全程不变，仅 `data/` 目录 mtime 变 |
| 定位方式 | 隔离 `prompts/` 写入任务的检查子代理逐文件二分（见任务 `09-26-fix-test-prompts-write-isolation`） |

## Requirements

### R1 用例内隔离库路径

- 首选：用例内 `monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))`——读取点见 `src/infrastructure/persistence/sqlite_connection.py:146-147`。
- 备选：像同文件 `_isolate_cookie_resolution` 那样桩掉 `find_task_by_name_sync`；若该用例的语义必须走真实查询链路，则回到首选。
- 目标：该用例不再打开 `data/app.sqlite3`，跑它前后 `data/` 目录 mtime 不变。

### R2 同文件排查同类缺口

- 扫 `tests/unit/test_process_service.py` 其余用例（以及同轮顺手 grep `APP_DATABASE_FILE` 的使用面）是否也走默认库路径；命中则同一轮一并隔离，不留给下一位。

## Acceptance Criteria

- [ ] AC1 单跑该用例前后 `stat -c '%y' data` 不变，且 `data/app.sqlite3` 的 sha256/size/mtime 不变 → 取证：跑前后对照
- [ ] AC2 单独跑 `tests/unit/` 与全量跑 `tests/`，`data/` 目录 mtime 均不变（R2 命中项一并验证）→ 取证：跑前后对照
- [ ] AC3 全量结果不劣于基线（145 passed / 3 failed / 3 skipped），失败同名同因，断言未被削弱/跳过
- [ ] AC4 未新增对 `data/`、`state/`、`.env`、`logs/`、`config.json` 的写入

## Non-goals

- 不改 `src/` 的默认库路径解析（生产 CWD=仓库根是设计，同 `09-26-fix-test-prompts-write-isolation` 的口径）。
- 不清理历史上可能残留的 `-wal`/`-shm` 文件（若存在，属独立清理，动手前先备份）。
- 不改 `spec` 里其他条目的既有描述（§3 的存量破例条目在本任务完成后按收口流程更新）。

## 相邻已知项

- 同类风险面：任何直接实例化 service、未注入 `db_path` / 未设 `APP_DATABASE_FILE` 的测试。排查策略与既有姿势见 `.trellis/spec/backend/quality-guidelines.md` §3。
- 本任务完成后，spec §3「已知隔离破例」条目里这条应改记为已修，并保留「别用 `data/` 目录 mtime 判断运行态」这条操作提醒的适用边界。