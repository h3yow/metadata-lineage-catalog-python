"""检索组合条件的稳定性，以及审计可追溯性。"""

from __future__ import annotations

from catalog.models import FieldInfo
from catalog.search import SearchCriteria
from caselog import log_case


def _seed(svc):
    data = [
        ("orders", "alice", ["finance", "daily"]),
        ("orders_archive", "bob", ["finance"]),
        ("clicks", "alice", ["marketing", "daily"]),
        ("users", "carol", ["master"]),
    ]
    for name, owner, tags in data:
        svc.entities.register_dataset(
            name=name, owner=owner, source="s1", tags=frozenset(tags)
        )
    svc.entities.add_field(dataset="orders",
                           field=FieldInfo(name="order_id", data_type="bigint", source="s1"))
    svc.entities.add_field(dataset="orders_archive",
                           field=FieldInfo(name="order_id", data_type="bigint", source="s1"))
    svc.entities.add_field(dataset="users",
                           field=FieldInfo(name="user_id", data_type="bigint", source="s1"))


def test_name_contains_case_insensitive(svc):
    _seed(svc)
    criteria = SearchCriteria(name_contains="ORDER")
    result = [d.name for d in svc.search(criteria)]
    log_case("名称大小写不敏感子串匹配", {"name_contains": "ORDER"},
             "子串包含、lower 比较、按名称排序", result)
    assert result == ["orders", "orders_archive"]


def test_combined_conditions_are_and(svc):
    _seed(svc)
    criteria = SearchCriteria(name_contains="order", tags=frozenset({"daily"}), owner="alice")
    result = [d.name for d in svc.search(criteria)]
    log_case(
        "名称+标签+负责人组合(AND)",
        {"name_contains": "order", "tags": ["daily"], "owner": "alice"},
        "三个条件同时满足才返回；orders_archive owner=bob 被排除",
        result,
    )
    assert result == ["orders"]


def test_tag_filter_is_subset_match(svc):
    _seed(svc)
    criteria = SearchCriteria(tags=frozenset({"finance", "daily"}))
    result = [d.name for d in svc.search(criteria)]
    log_case("多标签为子集匹配", {"tags": ["finance", "daily"]},
             "数据集必须同时包含两个标签", result)
    assert result == ["orders"]


def test_field_name_filter(svc):
    _seed(svc)
    criteria = SearchCriteria(field_name_contains="order_id")
    result = [d.name for d in svc.search(criteria)]
    log_case("按字段名反查数据集", {"field_name_contains": "order_id"},
             "任一字段名包含子串即命中，按数据集名排序", result)
    assert result == ["orders", "orders_archive"]


def test_result_order_is_stable(svc):
    _seed(svc)
    first = [d.name for d in svc.search(SearchCriteria())]
    second = [d.name for d in svc.search(SearchCriteria())]
    log_case("无条件检索结果稳定", {"criteria": "empty"},
             "all_datasets 按名称序，search 再按名称排序", first)
    assert first == second == ["clicks", "orders", "orders_archive", "users"]


def test_audit_tells_who_when_what(svc):
    svc.entities.register_dataset(name="orders", owner="alice", source="hive",
                                  actor="alice", tags=frozenset(["t1"]))
    svc.entities.register_dataset(name="orders", owner="alice", source="spark",
                                  actor="bob", tags=frozenset(["t2"]), description="x")
    svc.entities.add_field(dataset="orders", actor="carol",
                           field=FieldInfo(name="id", data_type="bigint", source="hive"))
    entries = svc.history("dataset:orders")
    log_case(
        "能查到最近一次变更的人/时间/内容",
        {"target": "dataset:orders"},
        "history 按 sequence 升序；changes 记录字段路径与新旧值",
        [(e.sequence, e.actor, e.action, e.changes) for e in entries],
    )
    assert [e.actor for e in entries] == ["alice", "bob"]
    latest = entries[-1]
    assert latest.actor == "bob"
    changed_paths = {path for path, _, _ in latest.changes}
    assert {"tags", "description", "sources"} & changed_paths
    field_entries = svc.history("field:orders.id")
    assert field_entries and field_entries[0].actor == "carol"
    # 全量历史包含所有目标，序号单调
    full = svc.history()
    assert [e.sequence for e in full] == list(range(1, len(full) + 1))
