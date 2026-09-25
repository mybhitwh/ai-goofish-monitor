"""
调度服务
负责管理定时任务的调度（含任务组统一调度）
"""
import asyncio
from datetime import datetime
from typing import Awaitable, Callable, List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.core.cron_utils import build_cron_trigger
from src.domain.models.task import Task
from src.domain.models.task_group import TaskGroup
from src.services.process_service import ProcessService

TaskProvider = Callable[[], Awaitable[List[Task]]]
GroupProvider = Callable[[], Awaitable[List[TaskGroup]]]


class SchedulerService:
    """调度服务"""

    def __init__(self, process_service: ProcessService):
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self.process_service = process_service
        self._task_provider: Optional[TaskProvider] = None
        self._group_provider: Optional[GroupProvider] = None

    def set_providers(
        self,
        task_provider: Optional[TaskProvider] = None,
        group_provider: Optional[GroupProvider] = None,
    ) -> None:
        """注入任务/任务组的数据提供者，供任务组触发时查询组内成员"""
        self._task_provider = task_provider
        self._group_provider = group_provider

    def start(self):
        """启动调度器"""
        if not self.scheduler.running:
            self.scheduler.start()
            print("调度器已启动")

    def stop(self):
        """停止调度器"""
        if self.scheduler.running:
            self.scheduler.shutdown()
            print("调度器已停止")

    def _get_next_fire_time(self, job_key: str):
        job = self.scheduler.get_job(job_key)
        if job is None:
            return None

        next_run_time = getattr(job, "next_run_time", None)
        if next_run_time is not None:
            return next_run_time

        trigger = getattr(job, "trigger", None)
        if trigger is None or not hasattr(trigger, "get_next_fire_time"):
            return None

        try:
            now = datetime.now(self.scheduler.timezone)
            return trigger.get_next_fire_time(None, now)
        except Exception:
            return None

    def get_next_run_time(self, task_id: int):
        return self._get_next_fire_time(f"task_{task_id}")

    def get_group_next_run_time(self, group_id: int):
        return self._get_next_fire_time(f"group_{group_id}")

    async def reload_jobs(
        self,
        tasks: List[Task],
        groups: Optional[List[TaskGroup]] = None,
    ):
        """重新加载所有定时任务（含任务组）"""
        print("正在重新加载定时任务...")
        self.scheduler.remove_all_jobs()

        if groups is None:
            groups = await self._load_groups()

        group_map = {group.id: group for group in groups if group.id is not None}

        for task in tasks:
            if not (task.enabled and task.cron):
                continue
            group = group_map.get(task.group_id) if task.group_id else None
            if group is not None and group.can_schedule():
                # 任务已加入开启调度的任务组，由任务组统一触发，跳过单独调度
                continue
            self._add_task_job(task)

        for group in groups:
            if not group.can_schedule():
                continue
            self._add_group_job(group)

        print("定时任务加载完成")

    def _add_task_job(self, task: Task) -> None:
        try:
            trigger = build_cron_trigger(
                task.cron,
                timezone=self.scheduler.timezone,
            )
            self.scheduler.add_job(
                self._run_task,
                trigger=trigger,
                args=[task.id, task.task_name],
                id=f"task_{task.id}",
                name=f"Scheduled: {task.task_name}",
                replace_existing=True
            )
            print(f"  -> 已为任务 '{task.task_name}' 添加定时规则: '{task.cron}'")
        except ValueError as e:
            print(f"  -> [警告] 任务 '{task.task_name}' 的 Cron 表达式无效: {e}")

    def _add_group_job(self, group: TaskGroup) -> None:
        try:
            trigger = build_cron_trigger(
                group.cron,
                timezone=self.scheduler.timezone,
            )
            self.scheduler.add_job(
                self._run_group,
                trigger=trigger,
                args=[group.id],
                id=f"group_{group.id}",
                name=f"Scheduled group: {group.name}",
                replace_existing=True,
            )
            print(
                f"  -> 已为任务组 '{group.name}' 添加定时规则: '{group.cron}' "
                f"(模式: {group.execution_mode})"
            )
        except ValueError as e:
            print(f"  -> [警告] 任务组 '{group.name}' 的 Cron 表达式无效: {e}")

    async def _run_task(self, task_id: int, task_name: str):
        """执行定时任务"""
        print(f"定时任务触发: 正在为任务 '{task_name}' 启动爬虫...")
        await self.process_service.start_task(task_id, task_name)

    async def run_group_now(self, group_id: int) -> List[Task]:
        """手动触发任务组（不校验 cron），返回本次涉及的组内任务"""
        return await self._run_group(group_id)

    async def _run_group(self, group_id: int) -> List[Task]:
        """执行任务组：按组的执行模式（串行/并行）启动组内启用的任务"""
        group = await self._find_group(group_id)
        if group is None:
            print(f"任务组 {group_id} 不存在，跳过执行")
            return []
        if not group.enabled:
            print(f"任务组 '{group.name}' 已禁用，跳过执行")
            return []

        members = await self._load_group_members(group_id)
        if not members:
            print(f"任务组 '{group.name}' 内没有启用的任务")
            return []

        mode = group.execution_mode or "serial"
        print(
            f"任务组 '{group.name}' 触发: {len(members)} 个任务, "
            f"执行模式: {'并行' if mode == 'parallel' else '串行'}"
        )

        if mode == "parallel":
            results = await asyncio.gather(
                *(
                    self.process_service.start_task(task.id, task.task_name)
                    for task in members
                ),
                return_exceptions=True,
            )
            for task, result in zip(members, results):
                if isinstance(result, Exception):
                    print(f"启动任务 '{task.task_name}' 失败: {result}")
            return members

        for task in members:
            await self.process_service.start_task(task.id, task.task_name)
            # 串行：等待当前任务退出后再启动下一个
            await self.process_service.wait_task_exit(task.id)
        return members

    async def stop_group(self, group_id: int) -> int:
        """停止任务组内所有正在运行的任务，返回停止数量"""
        members = await self._load_group_members(group_id, include_disabled=True)
        stopped = 0
        for task in members:
            if await self.process_service.stop_task(task.id):
                stopped += 1
        return stopped

    async def _load_tasks(self) -> List[Task]:
        if self._task_provider is None:
            return []
        try:
            return await self._task_provider()
        except Exception as exc:
            print(f"加载任务列表失败: {exc}")
            return []

    async def _load_groups(self) -> List[TaskGroup]:
        if self._group_provider is None:
            return []
        try:
            return await self._group_provider()
        except Exception as exc:
            print(f"加载任务组列表失败: {exc}")
            return []

    async def _find_group(self, group_id: int) -> Optional[TaskGroup]:
        for group in await self._load_groups():
            if group.id == group_id:
                return group
        return None

    async def _load_group_members(
        self, group_id: int, include_disabled: bool = False
    ) -> List[Task]:
        tasks = await self._load_tasks()
        members = [
            task for task in tasks
            if task.group_id == group_id and task.id is not None
        ]
        if not include_disabled:
            members = [task for task in members if task.enabled]
        return members
