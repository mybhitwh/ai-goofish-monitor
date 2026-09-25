"""概览任务计数缺陷的复现脚本（研究工件，不是产品代码）。

在临时目录里用真实生产代码复现 6 个场景，全程不触碰仓库的 data/app.sqlite3。
Phase 2 应把它转化为 tests/integration 下的 pytest 用例（断言未来应成立的不变量），
而不是把这个脚本留在仓库里当测试。

用法：
    .venv/bin/python .trellis/tasks/09-25-dashboard-task-identity/research/repro_dashboard_identity.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
tmp = Path(tempfile.mkdtemp(prefix="dash-identity-repro-"))
os.environ["APP_DATABASE_FILE"] = str(tmp / "app.sqlite3")   # 隔离：结果存储走这个库
os.chdir(tmp)
sys.path.insert(0, str(REPO))

from src.domain.models.task import TaskCreate, TaskUpdate                      # noqa: E402
from src.infrastructure.persistence.storage_names import build_result_filename  # noqa: E402
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository  # noqa: E402
from src.services.dashboard_service import build_dashboard_snapshot             # noqa: E402
from src.services.price_history_service import delete_price_snapshots           # noqa: E402
from src.services.result_storage_service import delete_result_file_records      # noqa: E402
from src.services.task_service import TaskService                               # noqa: E402


def write_jsonl(keyword: str, task_name: str, n: int) -> str:
    """预置结果文件；必须在首次触碰存储之前调用（引导导入只跑一次）。"""
    d = tmp / "jsonl"
    d.mkdir(exist_ok=True)
    fn = d / build_result_filename(keyword)
    with fn.open("w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({
                "爬取时间": f"2026-03-1{i + 1}T10:00:00",
                "搜索关键字": keyword,
                "任务名称": task_name,
                "商品信息": {"商品ID": f"{keyword}-{i}", "商品标题": f"{task_name} 商品{i}", "当前售价": "¥1000"},
                "ai_analysis": {"analysis_source": "ai", "is_recommended": i == 0},
            }, ensure_ascii=False) + "\n")
    return fn.name


async def report(svc: TaskService, label: str) -> dict:
    tasks = await svc.get_all_tasks()
    snap = await build_dashboard_snapshot(tasks)
    print(f"--- {label} ---")
    print(f"    任务管理={len(tasks)}  概览 task_summaries={len(snap['task_summaries'])}  "
          f"enabled_tasks={snap['summary']['enabled_tasks']}  result_files={snap['summary']['result_files']}")
    for s in snap["task_summaries"]:
        print(f"      | task_id={s['task_id']} name={s['task_name']!r} file={s['filename']} items={s['total_items']}")
    return snap


async def main() -> None:
    write_jsonl("kw_a", "同名任务", 1)
    write_jsonl("kw_b", "同名任务", 2)
    write_jsonl("both_old", "改名前任务", 3)
    write_jsonl("del_kw", "待删除任务", 4)
    write_jsonl("rename_kw", "旧名任务", 5)

    svc = TaskService(SqliteTaskRepository(db_path=str(tmp / "app.sqlite3"), legacy_config_file=None))

    print("场景 A：两个任务同名、keyword 不同")
    await svc.create_task(TaskCreate(task_name="同名任务", keyword="kw_a", description="d"))
    await svc.create_task(TaskCreate(task_name="同名任务", keyword="kw_b", description="d"))
    await report(svc, "A 结果：2 个任务 → 1 条摘要，kw_b 的 2 条数据从汇总中消失")

    print("\n场景 B：只改 keyword（预期无幽灵，靠任务名称回退救回）")
    b = await svc.create_task(TaskCreate(task_name="待删除任务", keyword="del_kw", description="d"))
    await svc.update_task(b.id, TaskUpdate(keyword="del_kw_v2"))
    await report(svc, "B 结果")

    print("\n场景 C：只改 task_name（预期无幽灵）")
    c = await svc.create_task(TaskCreate(task_name="旧名任务", keyword="rename_kw", description="d"))
    await svc.update_task(c.id, TaskUpdate(task_name="新名任务"))
    await report(svc, "C 结果")

    print("\n场景 B2：任务名与 keyword 同时改（预期幽灵）")
    d = await svc.create_task(TaskCreate(task_name="改名前任务", keyword="both_old", description="d"))
    await svc.update_task(d.id, TaskUpdate(task_name="改名后任务", keyword="both_new"))
    await report(svc, "B2 结果")

    print("\n场景 D：改过 keyword 之后再删除任务（复刻 DELETE /api/tasks/{id} 的清理逻辑）")
    await svc.delete_task(b.id)
    removed = await delete_result_file_records(build_result_filename("del_kw_v2"))  # 只清新 keyword 的文件
    delete_price_snapshots("del_kw_v2")
    print(f"    按当前 keyword 清理删除了 {removed} 行；旧 keyword 的 {build_result_filename('del_kw')} 未处理")
    await report(svc, "D 结果：任务已删，残留文件变成幽灵")

    print(f"\n临时目录：{tmp}")


if __name__ == "__main__":
    asyncio.run(main())