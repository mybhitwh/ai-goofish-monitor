"""
商品标注（备注与自定义标签）服务。

标注按 link_unique_key 全局存储，与某次爬取的结果文件无关：
同一商品在重新爬取或换任务后，备注与标签仍然保留。
"""
from __future__ import annotations

import json
from datetime import datetime

from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection


def _normalize_tags(tags: list[str] | None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags or []:
        text = str(tag or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _parse_tags_json(raw: str | None) -> list[str]:
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def _load_annotation_map_from_conn(conn, link_unique_keys: list[str]) -> dict[str, dict]:
    mapping: dict[str, dict] = {}
    keys = [key for key in dict.fromkeys(link_unique_keys) if key]
    for start in range(0, len(keys), 500):
        chunk = keys[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT link_unique_key, note, tags_json
            FROM item_annotations
            WHERE link_unique_key IN ({placeholders})
            """,
            tuple(chunk),
        ).fetchall()
        for row in rows:
            mapping[str(row["link_unique_key"])] = {
                "note": str(row["note"] or ""),
                "tags": _parse_tags_json(row["tags_json"]),
            }
    return mapping


def get_annotation_map_sync(link_unique_keys: list[str]) -> dict[str, dict]:
    keys = [key for key in dict.fromkeys(link_unique_keys) if key]
    if not keys:
        return {}
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        return _load_annotation_map_from_conn(conn, keys)


def update_annotation_sync(
    link_unique_key: str,
    *,
    note: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """按 key 局部更新标注；note/tags 传 None 表示保持不变。

    当更新后备注与标签均为空时，删除该行，保持标注表干净。
    """
    key = str(link_unique_key or "").strip()
    if not key:
        raise ValueError("link_unique_key 不能为空")
    if note is None and tags is None:
        raise ValueError("note 与 tags 至少提供一项")

    bootstrap_sqlite_storage()
    now = datetime.now().isoformat()
    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT note, tags_json FROM item_annotations WHERE link_unique_key = ?",
            (key,),
        ).fetchone()
        current_note = str(row["note"]) if row is not None else ""
        current_tags = _parse_tags_json(row["tags_json"]) if row is not None else []

        new_note = current_note if note is None else str(note or "").strip()
        new_tags = _normalize_tags(current_tags if tags is None else tags)

        if not new_note and not new_tags:
            conn.execute(
                "DELETE FROM item_annotations WHERE link_unique_key = ?",
                (key,),
            )
        else:
            conn.execute(
                """
                INSERT INTO item_annotations (link_unique_key, note, tags_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(link_unique_key) DO UPDATE SET
                    note = excluded.note,
                    tags_json = excluded.tags_json,
                    updated_at = excluded.updated_at
                """,
                (key, new_note, json.dumps(new_tags, ensure_ascii=False), now),
            )
        conn.commit()
    return {"note": new_note, "tags": new_tags}


def list_used_tags_sync() -> list[dict]:
    """聚合全部已用标签：名称 + 使用次数 + 最近使用时间（按最近使用倒序）。"""
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        rows = conn.execute(
            "SELECT tags_json, updated_at FROM item_annotations"
        ).fetchall()
    counts: dict[str, int] = {}
    last_used: dict[str, str] = {}
    for row in rows:
        for tag in _parse_tags_json(row["tags_json"]):
            text = str(tag or "").strip()
            if not text:
                continue
            counts[text] = counts.get(text, 0) + 1
            updated_at = str(row["updated_at"] or "")
            if updated_at > last_used.get(text, ""):
                last_used[text] = updated_at
    ordered = sorted(counts, key=lambda name: last_used.get(name, ""), reverse=True)
    return [
        {
            "name": name,
            "count": counts[name],
            "last_used_at": last_used.get(name, ""),
        }
        for name in ordered
    ]
