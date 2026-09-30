"""HTTP API 端到端：状态码、结构化错误体与完整链路。"""

from __future__ import annotations

import pytest

from catalog.api import create_app
from caselog import log_case


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    return TestClient(create_app())


def test_register_and_conflict_error_body(client):
    r1 = client.post("/datasets", json={"name": "orders", "owner": "alice", "source": "hive"})
    assert r1.status_code == 201 and r1.json()["created"] is True
    r2 = client.post("/datasets", json={"name": "orders", "owner": "bob", "source": "spark"})
    body = r2.json()
    log_case(
        "HTTP 层冲突返回结构化错误",
        {"first": ("orders", "alice", "hive"), "second": ("orders", "bob", "spark")},
        "422 + error=source_conflict，details 带双方 owner/source",
        {"status": r2.status_code, "body": body},
    )
    assert r2.status_code == 422
    assert body["error"] == "source_conflict"
    assert body["details"]["existing_owner"] == "alice"
    assert body["message"]


def test_field_type_conflict_over_http(client):
    client.post("/datasets", json={"name": "t", "owner": "a", "source": "s"})
    client.post("/fields", json={"dataset": "t",
                                 "field": {"name": "id", "data_type": "bigint", "source": "s"}})
    r = client.post("/fields", json={"dataset": "t",
                                    "field": {"name": "id", "data_type": "string", "source": "s2"}})
    log_case("HTTP 字段类型冲突", {"existing": "bigint", "incoming": "string"},
             "error=field_type_conflict 与 source_conflict 可区分", r.json()["error"])
    assert r.status_code == 422 and r.json()["error"] == "field_type_conflict"


def test_lineage_cycle_lifecycle_over_http(client):
    for n in ("a", "b", "c"):
        client.post("/datasets", json={"name": n, "owner": "x", "source": "s"})

    def edge(u, v, source="s"):
        return client.post("/lineage/edges", json={
            "upstream_kind": "dataset", "upstream_name": u,
            "downstream_kind": "dataset", "downstream_name": v,
            "source": source,
        })

    assert edge("a", "b").status_code == 201
    assert edge("b", "c").status_code == 201
    r = edge("c", "a", "s2")
    flags = r.json()["flags"]
    log_case("HTTP 成环边带 cycle 标记", {"edges": ["a->b", "b->c", "c->a"]},
             "环边保留且 flags 含 cycle", flags)
    assert "cycle" in flags

    dup = edge("a", "b", "s2").json()["flags"]
    log_case("HTTP 重复边标记", {"edge": "a->b", "sources": ["s", "s2"]},
             "duplicate + kind_conflict，边数不增加", dup)
    assert {"duplicate", "kind_conflict"} <= set(dup)

    edges = client.get("/lineage/edges").json()["edges"]
    assert len(edges) == 3

    r_missing = edge("a", "ghost")
    log_case("HTTP 悬空端点拒绝", {"edge": "a->ghost"},
             "error=edge_endpoint_not_found", r_missing.json()["error"])
    assert r_missing.status_code == 422 and r_missing.json()["error"] == "edge_endpoint_not_found"

    self_loop = edge("a", "a")
    assert self_loop.status_code == 422 and self_loop.json()["error"] == "edge_self_loop"

    cycles = client.get("/lineage/cycles").json()["cycles"]
    assert [[x["name"] for x in c] for c in cycles] == [["a", "b", "c"]]

    up = client.get("/lineage/dataset/c/upstream").json()["results"]
    down = client.get("/lineage/dataset/a/downstream").json()["results"]
    log_case(
        "HTTP 双向血缘展开",
        {"upstream_of": "c", "downstream_of": "a"},
        "BFS visited 防死循环，distance 从 1 开始",
        {"up": [(x["name"], x["distance"]) for x in up],
         "down": [(x["name"], x["distance"]) for x in down]},
    )
    assert [(x["name"], x["distance"]) for x in up] == [("b", 1), ("a", 2)]
    assert [(x["name"], x["distance"]) for x in down] == [("b", 1), ("c", 2)]


def test_search_and_audit_endpoints(client):
    client.post("/datasets", json={"name": "orders", "owner": "alice", "source": "hive",
                                   "tags": ["finance"]})
    client.post("/datasets", json={"name": "clicks", "owner": "alice", "source": "hive",
                                   "tags": ["marketing"]})
    r = client.post("/search", json={"owner": "alice", "tags": ["finance"]})
    names = [d["name"] for d in r.json()["datasets"]]
    log_case("HTTP 组合检索", {"owner": "alice", "tags": ["finance"]}, "AND 过滤", names)
    assert names == ["orders"]

    audit = client.get("/audit", params={"target": "dataset:orders"}).json()["entries"]
    assert audit and audit[0]["actor"] == "hive"
    assert audit[0]["changes"][0]["path"] == "owner"
