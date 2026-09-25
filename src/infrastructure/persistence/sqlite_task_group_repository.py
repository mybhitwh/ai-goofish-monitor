"""
基于 SQLite 的任务组仓储实现。
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

from src.domain.models.task_group import TaskGroup
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection


def _row_to_group(row) -> TaskGroup:
    payload = dict(row)
    payload["enabled"] = bool(payload["enabled"])
    return TaskGroup(**payload)


class SqliteTaskGroupRepository:
    """基于 SQLite 的任务组仓储"""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path

    async def find_all(self) -> List[TaskGroup]:
        return await asyncio.to_thread(self._find_all_sync)

    async def find_by_id(self, group_id: int) -> Optional[TaskGroup]:
        return await asyncio.to_thread(self._find_by_id_sync, group_id)

    async def save(self, group: TaskGroup) -> TaskGroup:
        return await asyncio.to_thread(self._save_sync, group)

    async def delete(self, group_id: int) -> bool:
        return await asyncio.to_thread(self._delete_sync, group_id)

    def _find_all_sync(self) -> List[TaskGroup]:
        bootstrap_sqlite_storage(self.db_path)
        with sqlite_connection(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM task_groups ORDER BY id ASC").fetchall()
        return [_row_to_group(row) for row in rows]

    def _find_by_id_sync(self, group_id: int) -> Optional[TaskGroup]:
        bootstrap_sqlite_storage(self.db_path)
        with sqlite_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM task_groups WHERE id = ?", (group_id,)
            ).fetchone()
        return _row_to_group(row) if row else None

    def _save_sync(self, group: TaskGroup) -> TaskGroup:
        bootstrap_sqlite_storage(self.db_path)
        with sqlite_connection(self.db_path) as conn:
            group_id = group.id
            if group_id is None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(id), -1) AS max_id FROM task_groups"
                ).fetchone()
                group_id = int(row["max_id"]) + 1
            payload = group.model_copy(update={"id": group_id}).model_dump()
            conn.execute(
                """
                INSERT OR REPLACE INTO task_groups (
                    id, name, cron, execution_mode, enabled
                ) VALUES (
                    :id, :name, :cron, :execution_mode, :enabled
                )
                """,
                {
                    "id": group_id,
                    "name": payload["name"],
                    "cron": payload["cron"],
                    "execution_mode": payload["execution_mode"],
                    "enabled": int(payload["enabled"]),
                },
            )
            conn.commit()
        return group.model_copy(update={"id": group_id})

    def _delete_sync(self, group_id: int) -> bool:
        bootstrap_sqlite_storage(self.db_path)
        with sqlite_connection(self.db_path) as conn:
            # 解除组内任务的关联，任务回落到各自的 cron 调度
            conn.execute(
                "UPDATE tasks SET group_id = NULL WHERE group_id = ?", (group_id,)
            )
            cursor = conn.execute(
                "DELETE FROM task_groups WHERE id = ?", (group_id,)
            )
            conn.commit()
        return cursor.rowcount > 0
