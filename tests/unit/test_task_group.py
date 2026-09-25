"""
任务组模型与调度逻辑单元测试
"""
import pytest
from pydantic import ValidationError

from src.domain.models.task import Task
from src.domain.models.task_group import TaskGroup, TaskGroupCreate
from src.services.scheduler_service import SchedulerService


def build_task(task_id: int, *, cron="0 9 * * *", group_id=None, enabled=True):
    return Task(
        id=task_id,
        task_name=f"task-{task_id}",
        enabled=enabled,
        keyword=f"kw-{task_id}",
        description="desc",
        max_pages=1,
        personal_only=True,
        cron=cron,
        ai_prompt_base_file="prompts/base_prompt.txt",
        ai_prompt_criteria_file="",
        group_id=group_id,
    )


def build_group(group_id: int, *, cron="0 9 * * *", mode="serial", enabled=True):
    return TaskGroup(
        id=group_id,
        name=f"group-{group_id}",
        cron=cron,
        execution_mode=mode,
        enabled=enabled,
    )


class StubProcessService:
    def __init__(self):
        self.started = []
        self.waits = []
        self.stopped = []

    async def start_task(self, task_id, task_name):
        self.started.append((task_id, task_name))
        return True

    async def wait_task_exit(self, task_id):
        self.waits.append(task_id)

    async def stop_task(self, task_id):
        self.stopped.append(task_id)
        return True


class StubProviders:
    def __init__(self, tasks, groups):
        self.tasks = tasks
        self.groups = groups

    async def provide_tasks(self):
        return self.tasks

    async def provide_groups(self):
        return self.groups


@pytest.mark.asyncio
async def test_group_create_rejects_invalid_cron():
    with pytest.raises(ValidationError):
        TaskGroupCreate(name="g", cron="not-a-cron")


def test_group_update_partial_apply():
    group = build_group(1)
    updated = TaskGroupUpdate(execution_mode="parallel").apply_to(group)
    assert updated.execution_mode == "parallel"
    assert updated.cron == group.cron
    assert updated.name == group.name


@pytest.mark.asyncio
async def test_reload_jobs_schedules_group_and_skips_member_tasks():
    proc = StubProcessService()
    scheduler = SchedulerService(proc)
    # 组内任务带独立 cron，应被组调度取代；组外任务保留自身调度
    tasks = [
        build_task(1, group_id=10),
        build_task(2, group_id=10, cron=None),
        build_task(3),
    ]
    groups = [build_group(10, cron="0 21 * * *", mode="serial")]

    await scheduler.reload_jobs(tasks, groups=groups)

    job_ids = {job.id for job in scheduler.scheduler.get_jobs()}
    assert "group_10" in job_ids
    assert "task_1" not in job_ids  # 组成员由任务组统一调度
    assert "task_2" not in job_ids
    assert "task_3" in job_ids  # 未分组成员保留独立调度


@pytest.mark.asyncio
async def test_reload_jobs_member_scheduled_alone_when_group_disabled():
    proc = StubProcessService()
    scheduler = SchedulerService(proc)
    tasks = [build_task(1, group_id=10)]
    groups = [build_group(10, cron=None, enabled=False)]

    await scheduler.reload_jobs(tasks, groups=groups)

    job_ids = {job.id for job in scheduler.scheduler.get_jobs()}
    assert "group_10" not in job_ids
    assert "task_1" in job_ids


@pytest.mark.asyncio
async def test_run_group_serial_starts_one_by_one():
    proc = StubProcessService()
    providers = StubProviders(
        tasks=[build_task(1, group_id=10), build_task(2, group_id=10), build_task(3)],
        groups=[build_group(10, mode="serial")],
    )
    scheduler = SchedulerService(proc)
    scheduler.set_providers(
        task_provider=providers.provide_tasks,
        group_provider=providers.provide_groups,
    )

    started = await scheduler.run_group_now(10)

    assert [t.id for t in started] == [1, 2]
    # 串行：每个任务启动后立即等待退出，再启动下一个
    assert proc.started == [(1, "task-1"), (2, "task-2")]
    assert proc.waits == [1, 2]


@pytest.mark.asyncio
async def test_run_group_parallel_starts_all():
    proc = StubProcessService()
    providers = StubProviders(
        tasks=[build_task(1, group_id=10), build_task(2, group_id=10)],
        groups=[build_group(10, mode="parallel")],
    )
    scheduler = SchedulerService(proc)
    scheduler.set_providers(
        task_provider=providers.provide_tasks,
        group_provider=providers.provide_groups,
    )

    started = await scheduler.run_group_now(10)

    assert [t.id for t in started] == [1, 2]
    assert sorted(proc.started) == [(1, "task-1"), (2, "task-2")]
    assert proc.waits == []


@pytest.mark.asyncio
async def test_stop_group_stops_members():
    proc = StubProcessService()
    providers = StubProviders(
        tasks=[build_task(1, group_id=10), build_task(3)],
        groups=[build_group(10)],
    )
    scheduler = SchedulerService(proc)
    scheduler.set_providers(
        task_provider=providers.provide_tasks,
        group_provider=providers.provide_groups,
    )

    stopped = await scheduler.stop_group(10)

    assert stopped == 1
    assert proc.stopped == [1]
