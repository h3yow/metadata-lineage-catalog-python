"""重复上报：同一实体被不同来源多次注册时，合并结果必须确定且可解释。"""

from __future__ import annotations

import logging

from metadata_catalog.errors import RejectedError, RejectionReason
from metadata_catalog.store import DatasetReport

from .conftest import log_decision

logger = logging.getLogger("catalog-tests")


def test_same_dataset_reported_twice_merges_union(svc):
    r1 = svc.register_dataset(DatasetReport(
        name="orders", owners=["alice"], tags=["trade"], source="mysql",
        fields=[{"name": "id", "type": "bigint"}], actor="alice",
    ))
    r2 = svc.register_dataset(DatasetReport(
        name=" orders ", owners=["bob"], tags=["pii"], source="kafka",
        fields=[{"name": "id", "type": "BIGINT"}], actor="bob",
    ))
    log_decision(
        "重复注册同名数据集",
        "第一次 mysql/alice/[trade]，第二次 kafka/bob/[pii]，字段 id 均为 BIGINT",
        f"created={r1.created},{r2.created}; merged={r2.merged_fields}",
    )

    assert r1.created is True
    assert r2.created is False
    assert r2.merged_fields == ["id"]
    d = svc.get_dataset("orders")
    # 集合取并集，与上报先后无关
    assert d.owners == {"alice", "bob"}
    assert d.tags == {"trade", "pii"}
    assert d.sources == {"mysql", "kafka"}
    assert d.fields["id"].sources == {"mysql", "kafka"}


def test_merge_order_independence(svc):
    """两个来源以相反顺序各报一次，最终状态必须一致。"""
    def report_order(first: str, second: str):
        from metadata_catalog.audit import AuditLog
        from metadata_catalog.service import MetadataCatalogService
        from tests.conftest import TickClock

        s = MetadataCatalogService(audit=AuditLog(clock=TickClock()))
        for src in (first, second):
            s.register_dataset(DatasetReport(
                name="t", owners=[f"owner_{src}"], tags=[f"tag_{src}"], source=src,
                fields=[{"name": "f1", "type": "int"}], actor="a",
            ))
        d = s.get_dataset("t")
        return sorted(d.owners), sorted(d.tags), sorted(d.sources)

    ab = report_order("mysql", "kafka")
    ba = report_order("kafka", "mysql")
    log_decision("合并顺序无关性", "mysql->kafka vs kafka->mysql", f"{ab} == {ba}")
    assert ab == ba


def test_latest_nonempty_description_wins(svc):
    svc.register_dataset(DatasetReport(name="t", description="第一个描述", source="s1"))
    svc.register_dataset(DatasetReport(name="t", description=None, source="s2"))
    svc.register_dataset(DatasetReport(name="t", description="最新描述", source="s3"))
    d = svc.get_dataset("t")
    log_decision("description 覆盖规则", "非空->空->非空", f"最终={d.description!r}")
    assert d.description == "最新描述"


def test_missing_dataset_name_is_distinct_rejection(svc):
    with __import__("pytest").raises(RejectedError) as exc:
        svc.register_dataset(DatasetReport(name="   ", source="s1"))
    log_decision("数据集名缺失", "name='   '", exc.value.rejection.reason.value)
    assert exc.value.rejection.reason is RejectionReason.DATASET_NAME_MISSING


def test_duplicate_field_same_type_from_new_source_accepted(svc):
    svc.register_dataset(DatasetReport(
        name="t", source="s1", fields=[{"name": "f", "type": "int"}]))
    r = svc.register_dataset(DatasetReport(
        name="t", source="s2", fields=[{"name": "f", "type": "int"}]))
    log_decision("同类型重复字段", "f:int 来自 s2", f"merged={r.merged_fields}")
    assert r.ok
    assert r.merged_fields == ["f"]
    assert svc.get_dataset("t").fields["f"].sources == {"s1", "s2"}


def test_batch_partial_acceptance(svc):
    """一个批次里好字段收下、坏字段拒绝，拒绝原因逐条可区分。"""
    svc.register_dataset(DatasetReport(
        name="t", source="s1", fields=[{"name": "f1", "type": "int"}]))
    r = svc.register_dataset(DatasetReport(name="t", source="s2", fields=[
        {"name": "f2", "type": "int"},           # 新增，应收下
        {"name": "f1", "type": "string"},        # 类型冲突，拒绝
        {"name": "", "type": "int"},             # 名字缺失，拒绝
        {"name": "f3", "type": ""},              # 类型缺失，拒绝
        {"name": "f2", "type": "int"},           # 同批次重复，拒绝
    ]))
    reasons = sorted(x.reason.value for x in r.rejections)
    log_decision(
        "批次部分接受",
        "5 个字段：1 新增/1 类型冲突/1 无名/1 无类型/1 批次重复",
        f"added={r.added_fields}, reasons={reasons}",
    )
    assert r.added_fields == ["f2"]
    assert reasons == sorted([
        RejectionReason.FIELD_TYPE_CONFLICT.value,
        RejectionReason.FIELD_NAME_MISSING.value,
        RejectionReason.FIELD_TYPE_MISSING.value,
        RejectionReason.FIELD_DUPLICATED_IN_BATCH.value,
    ])
    # 旧字段类型不被坏上报污染
    assert svc.get_dataset("t").fields["f1"].field_type == "INT"
