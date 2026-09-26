"""
调度服务：单任务 job 与任务组 job 的注册规则测试

背景（2026-09-25 生产事故）：任务组 id 可以是 0，而 `reload_jobs` 用真值判断
`if task.group_id` 判定组成员，导致 id=0 的组内任务既被任务组触发、又被注册了
单任务 job，一轮 cron 触发并发拉起多个爬虫。
"""
import asyncio

import pytest

from src.domain.models.task import Task
from src.domain.models.task_group import TaskGroup
from src.services.scheduler_service import SchedulerService


class StubProcessService:
    """只记录启动调用，不启动真实爬虫进程"""

    def __init__(self):
        self.started = []

    async def start_task(self, task_id, task_name):
        self.started.append((task_id, task_name))
        return True


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


def build_group(group_id: int, *, cron="0 9 * * *", enabled=True):
    return TaskGroup(
        id=group_id,
        name=f"group-{group_id}",
        cron=cron,
        execution_mode="serial",
        enabled=enabled,
    )


def job_ids_after_reload(tasks, groups):
    """在真实调度器实例上执行 reload_jobs，返回注册后的 job id 集合"""

    async def scenario():
        scheduler = SchedulerService(StubProcessService())
        scheduler.start()
        try:
            await scheduler.reload_jobs(tasks, groups=groups)
            return {job.id for job in scheduler.scheduler.get_jobs()}
        finally:
            scheduler.stop()

    return asyncio.run(scenario())


def test_group_member_not_scheduled_individually_when_group_id_zero():
    """组 id=0 是合法值：组成员只由 group_0 触发（修复前会同时注册 task_1）"""
    tasks = [build_task(1, group_id=0)]
    groups = [build_group(0)]

    job_ids = job_ids_after_reload(tasks, groups)

    assert job_ids == {"group_0"}


def test_task_without_group_keeps_individual_job():
    """group_id 为 None 的任务行为不变：仍注册单任务 job"""
    job_ids = job_ids_after_reload([build_task(1)], groups=[])

    assert job_ids == {"task_1"}


@pytest.mark.parametrize(
    "group",
    [
        build_group(0, enabled=False),
        build_group(0, cron=None),
    ],
    ids=["group-disabled", "group-without-cron"],
)
def test_disabled_group_members_fall_back_to_individual_jobs(group):
    """任务组不调度（禁用/无 cron）时，组内任务回落到独立调度"""
    job_ids = job_ids_after_reload([build_task(1, group_id=0)], groups=[group])

    assert job_ids == {"task_1"}