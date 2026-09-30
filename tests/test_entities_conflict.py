"""字段冲突与不合法上报：拒绝原因必须能被区分，且不静默忽略。"""

from __future__ import annotations

import pytest

from catalog.errors import CatalogError, RejectReason
from catalog.models import FieldInfo
from caselog import log_case


def _register(svc, name="orders", owner="alice", source="hive"):
    svc.entities.register_dataset(name=name, owner=owner, source=source)


def test_empty_dataset_name_rejected(svc):
    inputs = {"name": "  ", "owner": "alice", "source": "hive"}
    with pytest.raises(CatalogError) as ei:
        svc.entities.register_dataset(**inputs)
    log_case("空白数据集名称", inputs, "name 去空白后为空 -> DATASET_NAME_EMPTY", ei.value.reason.value)
    assert ei.value.reason is RejectReason.DATASET_NAME_EMPTY


def test_empty_field_name_rejected(svc):
    _register(svc)
    inputs = {"dataset": "orders", "field": ("", "bigint", "hive")}
    with pytest.raises(CatalogError) as ei:
        svc.entities.add_field(dataset="orders",
                               field=FieldInfo(name="", data_type="bigint", source="hive"))
    log_case("字段名缺失", inputs, "字段 name 为空 -> FIELD_NAME_EMPTY", ei.value.reason.value)
    assert ei.value.reason is RejectReason.FIELD_NAME_EMPTY


def test_empty_field_type_rejected(svc):
    _register(svc)
    inputs = {"dataset": "orders", "field": ("id", "  ", "hive")}
    with pytest.raises(CatalogError) as ei:
        svc.entities.add_field(dataset="orders",
                               field=FieldInfo(name="id", data_type="  ", source="hive"))
    log_case("字段类型缺失", inputs, "字段 data_type 为空 -> FIELD_TYPE_EMPTY", ei.value.reason.value)
    assert ei.value.reason is RejectReason.FIELD_TYPE_EMPTY


def test_field_type_conflict_rejected_and_keeps_old_type(svc):
    _register(svc)
    svc.entities.add_field(dataset="orders",
                           field=FieldInfo(name="id", data_type="bigint", source="hive"))
    inputs = {"field": "id", "existing_type": "bigint", "incoming": ("string", "spark")}
    with pytest.raises(CatalogError) as ei:
        svc.entities.add_field(dataset="orders",
                               field=FieldInfo(name="id", data_type="string", source="spark"))
    log_case(
        "字段类型冲突",
        inputs,
        "字段 data_type 不可变：bigint != string -> FIELD_TYPE_CONFLICT，保留旧类型",
        {"reason": ei.value.reason.value, "details": ei.value.details},
    )
    assert ei.value.reason is RejectReason.FIELD_TYPE_CONFLICT
    assert ei.value.details["existing_type"] == "bigint"
    assert ei.value.details["incoming_type"] == "string"
    assert svc.entities.find_field("orders", "id").data_type == "bigint"


def test_snapshot_missing_existing_field_is_atomically_rejected(svc):
    _register(svc)
    svc.entities.add_field(dataset="orders", field=FieldInfo(name="id", data_type="bigint", source="hive"))
    svc.entities.add_field(dataset="orders", field=FieldInfo(name="amt", data_type="decimal", source="hive"))
    snapshot = [FieldInfo(name="id", data_type="bigint", source="hive")]  # 缺 amt
    with pytest.raises(CatalogError) as ei:
        svc.entities.update_dataset(name="orders", source="hive", fields_snapshot=snapshot)
    log_case(
        "全量快照缺字段必须整体拒绝",
        {"existing_fields": ["id", "amt"], "snapshot": ["id"]},
        "快照缺少既有字段 -> FIELD_MISSING_ON_UPDATE，整次更新原子失败，不静默丢字段",
        {"reason": ei.value.reason.value, "missing": ei.value.details["missing_fields"]},
    )
    assert ei.value.reason is RejectReason.FIELD_MISSING_ON_UPDATE
    assert ei.value.details["missing_fields"] == ["amt"]
    # 原子性：原有字段一个都没被动过
    assert set(svc.entities.get_dataset("orders").fields) == {"id", "amt"}


def test_snapshot_type_conflict_rejected(svc):
    _register(svc)
    svc.entities.add_field(dataset="orders", field=FieldInfo(name="id", data_type="bigint", source="hive"))
    with pytest.raises(CatalogError) as ei:
        svc.entities.update_dataset(
            name="orders", source="hive",
            fields_snapshot=[FieldInfo(name="id", data_type="int", source="hive")],
        )
    log_case(
        "快照内类型冲突",
        {"existing": "bigint", "snapshot": "int"},
        "快照字段类型与登记不一致 -> FIELD_TYPE_CONFLICT",
        ei.value.reason.value,
    )
    assert ei.value.reason is RejectReason.FIELD_TYPE_CONFLICT


def test_snapshot_duplicate_field_in_batch(svc):
    _register(svc)
    with pytest.raises(CatalogError) as ei:
        svc.entities.update_dataset(
            name="orders", source="hive",
            fields_snapshot=[
                FieldInfo(name="id", data_type="bigint", source="hive"),
                FieldInfo(name="id", data_type="bigint", source="hive"),
            ],
        )
    log_case("快照内字段重复", {"snapshot": ["id", "id"]},
             "同一批次出现重复字段名 -> FIELD_DUPLICATE_IN_BATCH", ei.value.reason.value)
    assert ei.value.reason is RejectReason.FIELD_DUPLICATE_IN_BATCH


def test_unknown_source_cannot_update(svc):
    _register(svc)
    with pytest.raises(CatalogError) as ei:
        svc.entities.update_dataset(name="orders", source="unknown-system", owner="x")
    log_case("未上报过的来源不能更新",
             {"known_sources": ["hive"], "incoming": "unknown-system"},
             "source 不在 sources 中 -> SOURCE_CONFLICT", ei.value.reason.value)
    assert ei.value.reason is RejectReason.SOURCE_CONFLICT


def test_missing_dataset_and_field_are_distinct(svc):
    _register(svc)
    with pytest.raises(CatalogError) as ei1:
        svc.entities.get_dataset("ghost")
    with pytest.raises(CatalogError) as ei2:
        svc.entities.find_field("orders", "ghost")
    log_case(
        "数据集不存在与字段不存在原因码不同",
        {"dataset": "ghost", "field": "orders.ghost"},
        "DATASET_NOT_FOUND 与 FIELD_NOT_FOUND 必须可区分",
        (ei1.value.reason.value, ei2.value.reason.value),
    )
    assert ei1.value.reason is RejectReason.DATASET_NOT_FOUND
    assert ei2.value.reason is RejectReason.FIELD_NOT_FOUND
