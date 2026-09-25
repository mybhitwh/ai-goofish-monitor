import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import results


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _make_record(item_id: str, title: str, price: str, crawl_time: str) -> dict:
    return {
        "爬取时间": crawl_time,
        "搜索关键字": "demo",
        "任务名称": "Demo 任务",
        "商品信息": {
            "商品ID": item_id,
            "商品标题": title,
            "商品链接": f"https://www.goofish.com/item?id={item_id}",
            "当前售价": price,
        },
        "卖家信息": {"卖家昵称": "卖家A"},
        "ai_analysis": {
            "analysis_source": "ai",
            "is_recommended": True,
            "reason": "测试",
        },
    }


def _make_client():
    app = FastAPI()
    app.include_router(results.router)
    return TestClient(app)


def _bootstrap_results(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    records = [
        _make_record("1001", "Demo One", "¥950", "2026-01-02T09:00:00"),
        _make_record("1002", "Demo Two", "¥1200", "2026-01-02T09:05:00"),
    ]
    _write_jsonl(jsonl_dir / "demo_full_data.jsonl", records)
    return records


def test_annotation_patch_and_query_roundtrip(tmp_path, monkeypatch):
    _bootstrap_results(tmp_path, monkeypatch)
    client = _make_client()

    resp = client.get("/api/results/demo_full_data.jsonl")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_items"] == 2
    assert data["items"][0]["_note"] == ""
    assert data["items"][0]["_user_tags"] == []

    resp = client.patch(
        "/api/results/demo_full_data.jsonl/items/1001/annotation",
        json={"note": "周末面交，2500 以内要", "tags": ["等降价", "已联系卖家"]},
    )
    assert resp.status_code == 200
    assert resp.json()["tags"] == ["等降价", "已联系卖家"]

    resp = client.get("/api/results/demo_full_data.jsonl")
    items = {item["商品信息"]["商品ID"]: item for item in resp.json()["items"]}
    assert items["1001"]["_note"] == "周末面交，2500 以内要"
    assert items["1001"]["_user_tags"] == ["等降价", "已联系卖家"]
    assert items["1002"]["_note"] == ""

    resp = client.patch(
        "/api/results/demo_full_data.jsonl/items/1002/annotation",
        json={"tags": ["不包邮"]},
    )
    assert resp.status_code == 200
    assert resp.json()["note"] == ""

    resp = client.get("/api/results/used-tags")
    assert resp.status_code == 200
    tag_names = [tag["name"] for tag in resp.json()["tags"]]
    assert set(tag_names) == {"等降价", "已联系卖家", "不包邮"}


def test_annotation_tag_and_note_filters(tmp_path, monkeypatch):
    _bootstrap_results(tmp_path, monkeypatch)
    client = _make_client()

    client.patch(
        "/api/results/demo_full_data.jsonl/items/1001/annotation",
        json={"note": "价格可以谈", "tags": ["等降价"]},
    )
    client.patch(
        "/api/results/demo_full_data.jsonl/items/1002/annotation",
        json={"tags": ["不包邮"]},
    )

    resp = client.get("/api/results/demo_full_data.jsonl", params={"tags": "等降价"})
    ids = [item["商品信息"]["商品ID"] for item in resp.json()["items"]]
    assert ids == ["1001"]

    resp = client.get(
        "/api/results/demo_full_data.jsonl", params={"tags": "等降价,不包邮"}
    )
    assert resp.json()["total_items"] == 2

    resp = client.get(
        "/api/results/demo_full_data.jsonl", params={"tags": "不存在的标签"}
    )
    assert resp.json()["total_items"] == 0

    resp = client.get("/api/results/demo_full_data.jsonl", params={"has_note": True})
    ids = [item["商品信息"]["商品ID"] for item in resp.json()["items"]]
    assert ids == ["1001"]


def test_annotation_partial_update_and_cleanup(tmp_path, monkeypatch):
    _bootstrap_results(tmp_path, monkeypatch)
    client = _make_client()

    client.patch(
        "/api/results/demo_full_data.jsonl/items/1001/annotation",
        json={"note": "先观察", "tags": ["等降价"]},
    )

    resp = client.patch(
        "/api/results/demo_full_data.jsonl/items/1001/annotation",
        json={"note": "已放弃"},
    )
    assert resp.status_code == 200
    assert resp.json()["tags"] == ["等降价"]

    resp = client.patch(
        "/api/results/demo_full_data.jsonl/items/1001/annotation",
        json={"note": "", "tags": []},
    )
    assert resp.status_code == 200

    resp = client.get("/api/results/used-tags")
    assert resp.json()["tags"] == []

    resp = client.patch(
        "/api/results/demo_full_data.jsonl/items/1001/annotation", json={}
    )
    assert resp.status_code == 400

    resp = client.patch(
        "/api/results/demo_full_data.jsonl/items/9999/annotation",
        json={"note": "x"},
    )
    assert resp.status_code == 404


def test_block_item_keeps_annotation(tmp_path, monkeypatch):
    _bootstrap_results(tmp_path, monkeypatch)
    client = _make_client()

    client.patch(
        "/api/results/demo_full_data.jsonl/items/1001/annotation",
        json={"tags": ["卖家信用差"]},
    )
    resp = client.patch(
        "/api/results/demo_full_data.jsonl/items/1001/status",
        json={"status": "hidden"},
    )
    assert resp.status_code == 200

    resp = client.get("/api/results/demo_full_data.jsonl", params={"include_hidden": True})
    items = {item["商品信息"]["商品ID"]: item for item in resp.json()["items"]}
    assert items["1001"]["_status"] == "hidden"
    assert items["1001"]["_user_tags"] == ["卖家信用差"]
