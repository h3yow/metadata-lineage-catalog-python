"""字段冲突：类型冲突与其它拒绝原因必须可区分，旧状态不被污染。"""

from __future__ import annotations

import logging

import pytest

from metadata_catalog.errors import EntityNotFoundError, RejectionReason
from metadata_catalog.store import DatasetReport

from .conftest import log_decision

logger = logging.getLogger("catalog-tests")


def _register(svc, source="s1", **fields_kwargs):
    fields = [{"name": n, "type": t} for n, t in fields_kwargs.items()]
    return svc.register_dataset(DatasetReport(name="t", source=source, fields=fields))


def test_type_conflict_distinct_reason(svc):
    _register(svc, f1="int")
    r = _register(svc, source="s2", f1="string")
    rejection = r.rejections[0]
    log_decision(
        "字段类型冲突",
        "已存在 f1:int，再次上报 f1:string",
        f"reason={rejection.reason.value}, details={rejection.details}",
    )
    assert rejection.reason is RejectionReason.FIELD_TYPE_CONFLICT
    assert rejection.details["existing_type"] == "INT"
    assert rejection.details["reported_type"] == "STRING"
    # 冲突上报被拒绝：旧类型原样保留，且来源不被污染
    f1 = svc.get_dataset("t").fields["f1"]
    assert f1.field_type == "INT"
    assert f1.sources == {"s1"}


def test_type_case_and_whitespace_normalized(svc):
    _register(svc, f1=" BigInt ")
    r = _register(svc, source="s2", f1="bigint")
    log_decision("类型大小写/空白归一化", "' BigInt ' vs 'bigint'", f"merged={r.merged_fields}")
    assert r.ok
    assert svc.get_dataset("t").fields["f1"].field_type == "BIGINT"


@pytest.mark.parametrize("bad_type", ["", "   ", "varchar(10)", "json"])
def test_unsupported_or_missing_type_rejected(svc, bad_type):
    r = svc.register_dataset(DatasetReport(
        name="t", source="s1", fields=[{"name": "f1", "type": bad_type}]))
    log_decision("非法/缺失类型", f"type={bad_type!r}", r.rejections[0].reason.value)
    assert r.rejections[0].reason is RejectionReason.FIELD_TYPE_MISSING
    assert "f1" not in svc.get_dataset("t").fields


def test_removed_field_reported_again_is_distinct_rejection(svc):
    _register(svc, f1="int")
    svc.remove_field("t", "f1", actor="alice", source="s1")
    r = _register(svc, source="s2", f1="int")
    rejection = r.rejections[0]
    log_decision(
        "已删字段重新上报",
        "f1 软删除后又收到 f1:int",
        f"reason={rejection.reason.value}",
    )
    assert rejection.reason is RejectionReason.FIELD_REMOVED_THEN_REPORTED
    # 必须显式复活，不能被静默写回
    assert svc.get_dataset("t").fields["f1"].status.value == "removed"

    svc.restore_field("t", "f1", actor="alice", source="s1")
    r2 = _register(svc, source="s2", f1="int")
    log_decision("复活后再上报", "restore 后 f1:int", f"merged={r2.merged_fields}")
    assert r2.ok
    assert r2.merged_fields == ["f1"]


def test_remove_nonexistent_field_raises(svc):
    _register(svc, f1="int")
    with pytest.raises(EntityNotFoundError):
        svc.remove_field("t", "ghost", actor="a", source="s1")


def test_conflict_and_missing_name_are_separable(svc):
    _register(svc, f1="int")
    r = svc.register_dataset(DatasetReport(name="t", source="s2", fields=[
        {"name": "f1", "type": "double"},   # 冲突
        {"type": "int"},                    # 缺名
    ]))
    reasons = {x.reason for x in r.rejections}
    log_decision("冲突与缺名同时出现", "1 冲突 + 1 缺名", str(reasons))
    assert RejectionReason.FIELD_TYPE_CONFLICT in reasons
    assert RejectionReason.FIELD_NAME_MISSING in reasons
