"""
任务组领域模型
任务组用于统一配置一组任务的调度时间，并控制组内任务的执行方式（串行/并行）。
"""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from src.core.cron_utils import validate_cron_expression


def _normalize_optional_string(value):
    if value == "" or value == "null" or value == "undefined" or value is None:
        return None
    return value


class TaskGroup(BaseModel):
    """任务组实体"""

    model_config = ConfigDict(use_enum_values=True, extra="ignore")

    id: Optional[int] = None
    name: str
    cron: Optional[str] = None
    execution_mode: Literal["serial", "parallel"] = "serial"
    enabled: bool = True

    def can_schedule(self) -> bool:
        """任务组是否参与定时调度"""
        return self.enabled and bool(self.cron)


class TaskGroupCreate(BaseModel):
    """创建任务组的DTO"""

    model_config = ConfigDict(extra="ignore")

    name: str
    cron: Optional[str] = None
    execution_mode: Literal["serial", "parallel"] = "serial"
    enabled: bool = True

    @field_validator("cron", mode="before")
    @classmethod
    def normalize_cron(cls, value):
        return _normalize_optional_string(value)

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, value):
        return validate_cron_expression(value)


class TaskGroupUpdate(BaseModel):
    """更新任务组的DTO"""

    model_config = ConfigDict(extra="ignore")

    name: Optional[str] = None
    cron: Optional[str] = None
    execution_mode: Optional[Literal["serial", "parallel"]] = None
    enabled: Optional[bool] = None

    @field_validator("cron", mode="before")
    @classmethod
    def normalize_cron(cls, value):
        return _normalize_optional_string(value)

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, value):
        return validate_cron_expression(value)

    def apply_to(self, group: TaskGroup) -> TaskGroup:
        """应用部分更新并返回新的任务组实例"""
        update_data = self.model_dump(exclude_unset=True)
        return group.model_copy(update=update_data)
