"""检索组合过滤的稳定性，以及"谁、什么时候、改了什么"的审计追踪。"""

from __future__ import annotations

import logging

from metadata_catalog.audit import ChangeType
from metadata_catalog.store import DatasetReport

from .conftest import log_decision

logger = logging.getLogger("catalog-tests")


def _mk(svc, name, *, owners=None, tags=None, source="s1"):
    svc.register_dataset(DatasetReport(
        name=name, owners=owners or [], tags=tags or [], source=source))


def test_combined_filters_are_and_and_stable(svc):
    _mk(svc, "zebra", owners=["alice"], tags=["trade", "pii"])
    _mk(svc, "alpha", owners=["bob"], tags=["trade"])
    _mk(svc, "middle", owners=["alice", "bob"], tags=["trade", "finance"])

    # 条件之间 AND：name 子串 + tag + owner
    r1 = svc.search_datasets(name_contains="e", tags=["trade"], owners=["alice"])
    names1 = [d.name for d in r1]
    r2 = svc.search_datasets(name_contains="e", tags=["trade"], owners=["alice"])
    log_decision("组合过滤", "name~'e' AND tag=trade AND owner=alice（查两次）",
                 f"{names1} / {[d.name for d in r2]}")
    assert names1 == ["middle", "zebra"]  # 字母序稳定
    assert names1 == [d.name for d in r2]  # 可重复


def test_tag_list_is_or_between_tags(svc):
    _mk(svc, "a", tags=["trade"])
    _mk(svc, "b", tags=["finance"])
    _mk(svc, "c", tags=["other"])
    names = [d.name for d in svc.search_datasets(tags=["trade", "finance"])]
    log_decision("标签列表内部 OR", "tags=[trade, finance]", names)
    assert names == ["a", "b"]


def test_filter_by_source(svc):
    _mk(svc, "a", source="mysql")
    _mk(svc, "b", source="kafka")
    names = [d.name for d in svc.search_datasets(source="kafka")]
    log_decision("按来源过滤", "source=kafka", names)
    assert names == ["b"]


def test_name_match_is_case_insensitive_and_trimmed(svc):
    _mk(svc, "Orders_Fact")
    assert [d.name for d in svc.search_datasets(name_contains="orders")] == ["Orders_Fact"]
    assert [d.name for d in svc.search_datasets(name_contains="  FACT ")] == ["Orders_Fact"]
    log_decision("名称模糊匹配", "'orders' / '  FACT '", "命中 Orders_Fact")


def test_audit_records_who_when_what(svc, clock):
    r1 = svc.register_dataset(DatasetReport(
        name="orders", owners=["alice"], tags=["trade"], source="mysql",
        fields=[{"name": "id", "type": "bigint"}], actor="alice"))
    r2 = svc.register_dataset(DatasetReport(
        name="orders", owners=["bob"], source="kafka",
        fields=[{"name": "id", "type": "bigint"}, {"name": "v", "type": "string"}],
        actor="bob"))
    log_decision(
        "审计写入",
        "alice 注册 + bob 补 owner/字段",
        f"dataset seqs={[e.seq for e in svc.dataset_history('orders')]}",
    )
    assert r1.created and not r2.created

    hist = svc.dataset_history("orders")
    assert [e.actor for e in hist] == ["alice", "bob"]
    assert hist[0].action is ChangeType.DATASET_REGISTERED
    assert hist[1].action is ChangeType.DATASET_UPDATED
    # "改了什么"：owners 的旧值/新值
    assert hist[1].changes["owners"].old == ["alice"]
    assert hist[1].changes["owners"].new == ["alice", "bob"]
    # "什么时候"：时钟单调
    assert hist[0].timestamp < hist[1].timestamp

    # 字段级历史：id 是先 added 后 merged，v 是 added
    id_hist = svc.field_history("orders", "id")
    assert [e.action for e in id_hist] == [ChangeType.FIELD_ADDED, ChangeType.FIELD_MERGED]
    v_hist = svc.field_history("orders", "v")
    assert v_hist[0].actor == "bob"


def test_audit_records_rejections(svc):
    svc.register_dataset(DatasetReport(
        name="t", source="s1", fields=[{"name": "f1", "type": "int"}]))
    svc.register_dataset(DatasetReport(
        name="t", source="s2", fields=[{"name": "f1", "type": "string"}], actor="carol"))
    hist = svc.field_history("t", "f1")
    rejection_entries = [e for e in hist if e.note.startswith("[field_type_conflict]")]
    log_decision("拒绝进审计", "carol 上报类型冲突",
                 [e.note for e in rejection_entries])
    assert len(rejection_entries) == 1
    assert rejection_entries[0].actor == "carol"


def test_latest_change_summary(svc):
    svc.register_dataset(DatasetReport(name="t", owner="alice", source="s1"))
    svc.register_dataset(DatasetReport(name="t", owner="bob", source="s2"))
    latest = svc.latest_change("dataset:t")
    log_decision("最近变更", "两次注册后取 latest",
                 f"actor={latest.actor}, changes={list(latest.changes)}")
    assert latest.actor == "anonymous"
    assert "owners" in latest.changes
