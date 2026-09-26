import asyncio
import sys
from types import SimpleNamespace

from src.services.process_service import ProcessService


class FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = None
        self._done = asyncio.Event()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    def finish(self, returncode: int = 0):
        self.returncode = returncode
        self._done.set()

    def terminate(self):
        self.finish(-15)

    def kill(self):
        self.finish(-9)


def test_process_service_marks_task_stopped_when_process_exits(monkeypatch, tmp_path):
    fake_process = FakeProcess(pid=4321)
    events = []

    async def run_scenario():
        service = ProcessService()
        service.failure_guard.should_skip_start = lambda *args, **kwargs: SimpleNamespace(
            skip=False,
            should_notify=False,
            reason="",
            consecutive_failures=0,
            paused_until=None,
        )

        stopped = asyncio.Event()

        async def on_started(task_id: int):
            events.append(("started", task_id))

        async def on_stopped(task_id: int):
            events.append(("stopped", task_id))
            stopped.set()

        service.set_lifecycle_hooks(on_started=on_started, on_stopped=on_stopped)

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            return fake_process

        monkeypatch.setattr(
            "src.services.process_service.build_task_log_path",
            lambda task_id, _task_name: str(tmp_path / f"task-{task_id}.log"),
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        started = await service.start_task(0, "task-a")
        assert started is True
        assert events == [("started", 0)]
        assert service.is_running(0) is True

        fake_process.finish(0)
        await asyncio.wait_for(stopped.wait(), timeout=1)

        assert ("stopped", 0) in events
        assert service.is_running(0) is False

    asyncio.run(run_scenario())


def test_process_service_reindexes_runtime_maps_after_delete():
    service = ProcessService()
    proc_a = object()
    proc_c = object()
    watcher_a = object()
    watcher_c = object()

    service.processes = {0: proc_a, 2: proc_c}
    service.log_paths = {0: "a.log", 2: "c.log"}
    service.task_names = {0: "A", 2: "C"}
    service.exit_watchers = {0: watcher_a, 2: watcher_c}

    service.reindex_after_delete(1)

    assert service.processes == {0: proc_a, 1: proc_c}
    assert service.log_paths == {0: "a.log", 1: "c.log"}
    assert service.task_names == {0: "A", 1: "C"}
    assert service.exit_watchers == {0: watcher_a, 1: watcher_c}


def test_process_service_adds_debug_limit_arg_when_env_enabled(monkeypatch):
    monkeypatch.setenv("SPIDER_DEBUG_LIMIT", "1")
    service = ProcessService()

    command = service._build_spawn_command("task-a")

    assert command == [
        sys.executable,
        "-u",
        "spider_v2.py",
        "--task-name",
        "task-a",
        "--debug-limit",
        "1",
    ]


def _isolate_cookie_resolution(monkeypatch, tmp_path, task=None):
    """隔离 guard 文件、任务查询与仓库根 STATE_FILE"""
    monkeypatch.setenv("TASK_FAILURE_GUARD_PATH", str(tmp_path / "guard.json"))
    monkeypatch.setattr(
        "src.services.process_service.find_task_by_name_sync",
        lambda _task_name: task,
    )
    monkeypatch.setattr(
        "src.services.process_service.STATE_FILE",
        str(tmp_path / "missing_root_state.json"),
    )


def test_resolve_cookie_path_prefers_task_account_state_file(tmp_path, monkeypatch):
    """任务显式配置 account_state_file 时优先使用（既有行为不变）"""
    task = SimpleNamespace(account_state_file="state/acc2.json")
    _isolate_cookie_resolution(monkeypatch, tmp_path, task)

    service = ProcessService()

    assert service._resolve_cookie_path("task-a") == "state/acc2.json"


def test_resolve_cookie_path_uses_guard_remembered_path(tmp_path, monkeypatch):
    """任务未配置登录态时回退到熔断器记录的真实路径（R5：state/acc1.json）"""
    _isolate_cookie_resolution(monkeypatch, tmp_path)

    service = ProcessService()
    service.failure_guard.record_failure(
        "task-a", "FAIL_SYS_USER_VALIDATE", cookie_path="state/acc1.json"
    )

    assert service._resolve_cookie_path("task-a") == "state/acc1.json"


def test_resolve_cookie_path_returns_none_when_unresolvable(tmp_path, monkeypatch):
    """解析不到时返回 None，不伪造路径"""
    _isolate_cookie_resolution(monkeypatch, tmp_path)

    service = ProcessService()

    assert service._resolve_cookie_path("task-a") is None
