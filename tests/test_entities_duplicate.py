"""重复上报：合并规则必须确定、幂等、可解释。"""

from __future__ import annotations

from catalog.errors import CatalogError, RejectReason
from catalog.models import FieldInfo
from caselog import log_case


def test_same_source_duplicate_is_idempotent(svc):
    inputs = {"name": "orders", "owner": "alice", "source": "hive"}
    ds1, c1 = svc.entities.register_dataset(**inputs)
    ds2, c2 = svc.entities.register_dataset(**inputs)
    log_case(
        "同来源完全相同的重复上报应幂等",
        inputs,
        "第二次上报 owner/source/tags/description 与首次完全一致 -> 不改数据，仅留审计",
        {"created": (c1, c2), "edge": False},
    )
    assert c1 is True and c2 is False
    assert ds1 is ds2
    assert svc.entities.all_datasets() == [ds1]


def test_different_sources_same_owner_merge_tags(svc):
    inputs1 = {"name": "orders", "owner": "alice", "source": "hive", "tags": ["finance"]}
    inputs2 = {"name": "orders", "owner": "alice", "source": "spark", "tags": ["daily"],
               "description": "订单主表"}
    svc.entities.register_dataset(tags=frozenset(["finance"]), **{k: v for k, v in inputs1.items() if k != "tags"})
    ds, created = svc.entities.register_dataset(
        name="orders", owner="alice", source="spark",
        tags=frozenset(["daily"]), description="订单主表",
    )
    log_case(
        "不同来源、相同 owner 重复上报应合并",
        {"first": inputs1, "second": inputs2},
        "跨来源且 owner 一致 -> tags 取并集、description 先写非空者为准、sources 累积",
        {"created": created, "tags": sorted(ds.tags), "sources": sorted(ds.sources)},
    )
    assert created is False
    assert ds.tags == frozenset({"finance", "daily"})
    assert ds.sources == frozenset({"hive", "spark"})
    assert ds.description == "订单主表"


def test_different_sources_owner_conflict_rejected(svc):
    svc.entities.register_dataset(name="orders", owner="alice", source="hive")
    inputs = {"name": "orders", "owner": "bob", "source": "spark"}
    try:
        svc.entities.register_dataset(**inputs)
        raised = None
    except CatalogError as e:
        raised = e
    log_case(
        "不同来源上报不同 owner 必须拒绝",
        inputs,
        "跨来源 owner 不一致 -> SOURCE_CONFLICT，拒绝原因可与类型冲突区分",
        None if raised is None else raised.reason.value,
    )
    assert raised is not None
    assert raised.reason is RejectReason.SOURCE_CONFLICT
    # 被拒绝后数据保持原状
    assert svc.entities.get_dataset("orders").owner == "alice"


def test_duplicate_field_same_type_merges_tags(svc):
    svc.entities.register_dataset(name="orders", owner="alice", source="hive")
    f1 = FieldInfo(name="id", data_type="bigint", source="hive", tags={"pii"})
    f2 = FieldInfo(name="id", data_type="bigint", source="spark", tags={"pk"})
    svc.entities.add_field(dataset="orders", field=f1)
    field, created = svc.entities.add_field(dataset="orders", field=f2)
    log_case(
        "同名字段同类型重复上报应合并标签",
        {"field1": ("id", "bigint", "hive"), "field2": ("id", "bigint", "spark")},
        "字段类型不可变；类型一致 -> tags 并集、sources 累积、不新增字段",
        {"created": created, "tags": sorted(field.tags), "sources": sorted(field.sources)},
    )
    assert created is False
    assert field.tags == frozenset({"pii", "pk"})
    assert list(svc.entities.get_dataset("orders").fields) == ["id"]


def test_same_source_can_update_owner(svc):
    svc.entities.register_dataset(name="orders", owner="alice", source="hive")
    ds, _ = svc.entities.register_dataset(name="orders", owner="alice2", source="hive")
    log_case(
        "同一来源可以更新 owner",
        {"name": "orders", "owner_first": "alice", "owner_second": "alice2", "source": "hive"},
        "source 与首次相同 -> 允许后写覆盖 owner",
        ds.owner,
    )
    assert ds.owner == "alice2"


def test_audit_records_who_when_what(svc):
    svc.entities.register_dataset(name="orders", owner="alice", source="hive", actor="alice",
                                  tags=frozenset(["a"]))
    svc.entities.register_dataset(name="orders", owner="alice", source="spark", actor="bob",
                                  tags=frozenset(["b"]))
    history = svc.history("dataset:orders")
    log_case(
        "每次变更都可查到操作人、时间和差异",
        {"reports": [("alice", "hive"), ("bob", "spark")]},
        "审计日志顺序追加 actor/timestamp/changes",
        [(e.actor, e.action, e.changes) for e in history],
    )
    assert [e.actor for e in history] == ["alice", "bob"]
    assert all(e.timestamp is not None for e in history)
    merge_entry = history[1]
    assert any(path == "tags" for path, _, _ in merge_entry.changes)
