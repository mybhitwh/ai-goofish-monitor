"""
任务组 API 集成测试
"""
from src.domain.models.task_group import TaskGroupUpdate


def _create_group(client, name="iPad Air 组", cron="0 9,21 * * *", mode="serial"):
    response = client.post(
        "/api/groups",
        json={"name": name, "cron": cron, "execution_mode": mode, "enabled": True},
    )
    assert response.status_code == 200, response.text
    return response.json()["group"]


def _create_task(client, name, keyword, group_id=None):
    response = client.post(
        "/api/tasks/",
        json={
            "task_name": name,
            "keyword": keyword,
            "description": "desc",
            "cron": "0 8 * * *",
            "decision_mode": "ai",
        },
    )
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    if group_id is not None:
        patch = client.patch(
            f"/api/tasks/{task['id']}", json={"group_id": group_id}
        )
        assert patch.status_code == 200, patch.text
        task = patch.json()["task"]
    return task


def test_group_crud_and_member_flow(api_client):
    group = _create_group(api_client)
    assert group["id"] is not None

    task = _create_task(api_client, "组内任务", "ipad air", group_id=group["id"])
    assert task["group_id"] == group["id"]

    # 列表包含组且统计组内任务数
    groups = api_client.get("/api/groups").json()["groups"]
    assert any(g["id"] == group["id"] and g["task_count"] == 1 for g in groups)

    # 更新组
    updated = api_client.patch(
        f"/api/groups/{group['id']}",
        json={"execution_mode": "parallel"},
    )
    assert updated.status_code == 200
    assert updated.json()["group"]["execution_mode"] == "parallel"

    # 删除组后任务解除关联，回落到独立调度
    deleted = api_client.delete(f"/api/groups/{group['id']}")
    assert deleted.status_code == 200
    task_after = api_client.get(f"/api/tasks/{task['id']}").json()
    assert task_after["group_id"] is None


def test_create_task_with_unknown_group_rejected(api_client):
    response = client_post_with_group(api_client, group_id=999)
    assert response.status_code == 400
    assert "任务组不存在" in response.json()["detail"]


def client_post_with_group(api_client, group_id):
    return api_client.post(
        "/api/tasks/",
        json={
            "task_name": "坏引用任务",
            "keyword": "kw",
            "description": "desc",
            "decision_mode": "ai",
            "group_id": group_id,
        },
    )


def test_group_invalid_cron_rejected(api_client):
    response = api_client.post(
        "/api/groups",
        json={"name": "坏 cron", "cron": "not-a-cron"},
    )
    assert response.status_code == 422


def test_start_stop_missing_group_404(api_client):
    assert api_client.post("/api/groups/start/123").status_code == 404
    assert api_client.post("/api/groups/stop/123").status_code == 404


def test_group_update_nonexistent_404(api_client):
    response = api_client.patch("/api/groups/123", json={"name": "x"})
    assert response.status_code == 404
