"""HTTP 端到端：错误码、拒绝原因区分、血缘与检索接口。"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

import main as web

logger = logging.getLogger("catalog-tests")


@pytest.fixture()
def client():
    web.service = web.MetadataCatalogService()
    return TestClient(web.app)


def _register(client, **kw):
    body = {"name": "t", "actor": "a", "source": "s1", **kw}
    return client.post("/datasets", json=body)


def test_register_and_get(client):
    resp = _register(client, owners=["alice"], tags=["trade"],
                     fields=[{"name": "id", "type": "bigint"}])
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] is True
    assert data["added_fields"] == ["id"]

    got = client.get("/datasets/t").json()
    assert got["owners"] == ["alice"]
    assert got["fields"][0]["field_type"] == "BIGINT"
    logger.info("用例[HTTP注册] 输入=owners/tags/fields => 判定=%s", got)


def test_missing_dataset_name_422(client):
    resp = client.post("/datasets", json={"name": "  "})
    body = resp.json()
    logger.info("用例[HTTP无名数据集] 输入={'name':'  '} => 判定=%s", body)
    assert resp.status_code == 422
    assert body["rejections"][0]["reason"] == "dataset_name_missing"


def test_field_conflict_reported_per_field(client):
    _register(client, fields=[{"name": "id", "type": "bigint"}])
    resp = _register(client, source="s2", fields=[
        {"name": "ok", "type": "int"},
        {"name": "id", "type": "string"},
    ])
    data = resp.json()
    logger.info("用例[HTTP部分拒绝] => added=%s rejections=%s",
                data["added_fields"], data["rejections"])
    assert data["added_fields"] == ["ok"]
    reasons = {r["reason"] for r in data["rejections"]}
    assert reasons == {"field_type_conflict"}


def test_lineage_cycle_and_duplicate_status(client):
    for n in ("a", "b", "c"):
        _register(client, name=n)
    assert client.post("/lineage/datasets", json={"upstream": "a", "downstream": "b"}).status_code == 200
    assert client.post("/lineage/datasets", json={"upstream": "b", "downstream": "c"}).status_code == 200

    dup = client.post("/lineage/datasets", json={"upstream": "a", "downstream": "b"})
    cyc = client.post("/lineage/datasets", json={"upstream": "c", "downstream": "a"})
    logger.info("用例[HTTP重复边] => %s %s", dup.status_code, dup.json()["rejection"]["reason"])
    logger.info("用例[HTTP成环] => %s %s path=%s",
                cyc.status_code, cyc.json()["rejection"]["reason"],
                cyc.json()["rejection"]["details"]["cycle_path"])
    assert dup.status_code == 409
    assert dup.json()["rejection"]["reason"] == "duplicate_edge"
    assert cyc.status_code == 409
    assert cyc.json()["rejection"]["reason"] == "lineage_cycle"


def test_dangling_edge_listed_and_resolved(client):
    _register(client, name="a")
    resp = client.post("/lineage/datasets", json={"upstream": "a", "downstream": "x"})
    assert resp.json()["edge"]["status"] == "dangling"
    edges = client.get("/lineage/edges").json()
    assert edges["count"] == 1
    _register(client, name="x", source="s2")
    active_only = client.get("/lineage/edges", params={"include_dangling": False}).json()
    assert active_only["edges"][0]["status"] == "active"
    logger.info("用例[HTTP悬挂补齐] => edge=%s", active_only["edges"][0])


def test_search_and_history_endpoints(client):
    _register(client, name="orders", owners=["alice"], tags=["trade"])
    _register(client, name="payments", owners=["bob"], tags=["trade"])
    found = client.get("/datasets", params=[("tag", "trade"), ("owner", "alice")]).json()
    assert [d["name"] for d in found["datasets"]] == ["orders"]

    hist = client.get("/history/dataset/orders").json()
    assert hist["entries"][0]["action"] == "dataset_registered"
    logger.info("用例[HTTP检索+历史] => names=%s first_action=%s",
                [d["name"] for d in found["datasets"]], hist["entries"][0]["action"])


def test_unknown_dataset_404(client):
    resp = client.get("/datasets/nope")
    assert resp.status_code == 404
    assert resp.json()["rejections"][0]["reason"] == "entity_not_found"
