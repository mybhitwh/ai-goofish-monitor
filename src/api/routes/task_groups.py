"""
任务组管理路由
"""
from fastapi import APIRouter, Depends, HTTPException

from src.api.dependencies import (
    get_process_service,
    get_scheduler_service,
    get_task_group_service,
    get_task_service,
)
from src.domain.models.task_group import TaskGroupCreate, TaskGroupUpdate
from src.services.process_service import ProcessService
from src.services.scheduler_service import SchedulerService
from src.services.task_group_service import TaskGroupService
from src.services.task_service import TaskService
from src.services.task_payloads import serialize_timestamp

router = APIRouter(prefix="/api/groups", tags=["task-groups"])


def _serialize_group(group, scheduler_service: SchedulerService, task_service: TaskService):
    payload = group.model_dump()
    payload["next_run_at"] = None
    if group.id is not None and scheduler_service is not None:
        next_run_time = scheduler_service.get_group_next_run_time(group.id)
        payload["next_run_at"] = serialize_timestamp(next_run_time)
    payload["task_count"] = 0
    return payload


async def _serialize_groups(
    groups, scheduler_service: SchedulerService, task_service: TaskService
):
    tasks = await task_service.get_all_tasks()
    counts: dict = {}
    for task in tasks:
        if task.group_id is not None:
            counts[task.group_id] = counts.get(task.group_id, 0) + 1
    serialized = []
    for group in groups:
        item = _serialize_group(group, scheduler_service, task_service)
        item["task_count"] = counts.get(group.id, 0)
        serialized.append(item)
    return serialized


@router.get("")
async def get_groups(
    group_service: TaskGroupService = Depends(get_task_group_service),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
    task_service: TaskService = Depends(get_task_service),
):
    """获取所有任务组（含组内任务数量与下次执行时间）"""
    groups = await group_service.get_all_groups()
    return {"groups": await _serialize_groups(groups, scheduler_service, task_service)}


@router.get("/{group_id}")
async def get_group(
    group_id: int,
    group_service: TaskGroupService = Depends(get_task_group_service),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
    task_service: TaskService = Depends(get_task_service),
):
    """获取单个任务组"""
    group = await group_service.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="任务组未找到")
    serialized = await _serialize_groups([group], scheduler_service, task_service)
    return serialized[0]


@router.post("")
async def create_group(
    group_create: TaskGroupCreate,
    group_service: TaskGroupService = Depends(get_task_group_service),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
    task_service: TaskService = Depends(get_task_service),
):
    """创建新任务组"""
    group = await group_service.create_group(group_create)
    tasks = await task_service.get_all_tasks()
    await scheduler_service.reload_jobs(tasks)
    return {"message": "任务组创建成功", "group": group.model_dump()}


@router.patch("/{group_id}")
async def update_group(
    group_id: int,
    group_update: TaskGroupUpdate,
    group_service: TaskGroupService = Depends(get_task_group_service),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
    task_service: TaskService = Depends(get_task_service),
):
    """更新任务组"""
    try:
        group = await group_service.update_group(group_id, group_update)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    tasks = await task_service.get_all_tasks()
    await scheduler_service.reload_jobs(tasks)
    return {"message": "任务组更新成功", "group": group.model_dump()}


@router.delete("/{group_id}")
async def delete_group(
    group_id: int,
    group_service: TaskGroupService = Depends(get_task_group_service),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
    task_service: TaskService = Depends(get_task_service),
):
    """删除任务组（组内任务自动解除关联，回落到各自 cron 调度）"""
    success = await group_service.delete_group(group_id)
    if not success:
        raise HTTPException(status_code=404, detail="任务组未找到")
    tasks = await task_service.get_all_tasks()
    await scheduler_service.reload_jobs(tasks)
    return {"message": "任务组删除成功，组内任务已恢复独立调度"}


@router.post("/start/{group_id}")
async def start_group(
    group_id: int,
    group_service: TaskGroupService = Depends(get_task_group_service),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
):
    """立即执行任务组（按组的执行模式启动组内任务）"""
    group = await group_service.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="任务组未找到")
    members = await scheduler_service.run_group_now(group_id)
    return {
        "message": f"任务组 '{group.name}' 已触发",
        "started_tasks": [task.task_name for task in members],
    }


@router.post("/stop/{group_id}")
async def stop_group(
    group_id: int,
    group_service: TaskGroupService = Depends(get_task_group_service),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
    process_service: ProcessService = Depends(get_process_service),
):
    """停止任务组内所有正在运行的任务"""
    group = await group_service.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="任务组未找到")
    stopped = await scheduler_service.stop_group(group_id)
    return {"message": f"任务组 '{group.name}' 已停止 {stopped} 个任务"}
