"""
任务组管理服务
封装任务组相关的业务逻辑
"""
from typing import List, Optional

from src.domain.models.task_group import TaskGroup, TaskGroupCreate, TaskGroupUpdate


class TaskGroupService:
    """任务组管理服务"""

    def __init__(self, repository):
        self.repository = repository

    async def get_all_groups(self) -> List[TaskGroup]:
        """获取所有任务组"""
        return await self.repository.find_all()

    async def get_group(self, group_id: int) -> Optional[TaskGroup]:
        """获取单个任务组"""
        return await self.repository.find_by_id(group_id)

    async def create_group(self, group_create: TaskGroupCreate) -> TaskGroup:
        """创建新任务组"""
        group = TaskGroup(**group_create.model_dump())
        return await self.repository.save(group)

    async def update_group(self, group_id: int, group_update: TaskGroupUpdate) -> TaskGroup:
        """更新任务组"""
        group = await self.repository.find_by_id(group_id)
        if not group:
            raise ValueError(f"任务组 {group_id} 不存在")
        updated_group = group_update.apply_to(group)
        return await self.repository.save(updated_group)

    async def delete_group(self, group_id: int) -> bool:
        """删除任务组（组内任务自动解除关联）"""
        return await self.repository.delete(group_id)
