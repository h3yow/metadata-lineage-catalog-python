"""血缘：成环、重复边、悬空端点与双向展开。"""

from __future__ import annotations

import pytest

from catalog.errors import CatalogError, RejectReason
from catalog.models import EdgeFlag, EntityKind, EntityRef
from caselog import log_case


def _d(svc, *names):
    for n in names:
        svc.entities.register_dataset(name=n, owner="alice", source="s1")
    return [EntityRef(EntityKind.DATASET, n) for n in names]


def test_cycle_edge_is_flagged_not_dropped(svc):
    a, b, c = _d(svc, "a", "b", "c")
    svc.lineage.add_edge(upstream=a, downstream=b, source="s1")
    svc.lineage.add_edge(upstream=b, downstream=c, source="s1")
    closing = svc.lineage.add_edge(upstream=c, downstream=a, source="s1")
    log_case(
        "闭合环的边被标记 CYCLE 而不是拒绝/丢弃",
        {"edges": ["a->b", "b->c", "c->a"]},
        "加入 c->a 前 downstream(a) 已可达 upstream(c) -> 会成环；边保留并打 CYCLE",
        {"flags": sorted(f.value for f in closing.flags), "edge_count": len(svc.lineage.edges())},
    )
    assert EdgeFlag.CYCLE in closing.flags
    assert len(svc.lineage.edges()) == 3
    cycles = svc.lineage.find_cycles()
    assert [[r.name for r in cyc] for cyc in cycles] == [["a", "b", "c"]]


def test_traversal_on_cycle_terminates_and_is_stable(svc):
    a, b, c = _d(svc, "a", "b", "c")
    svc.lineage.add_edge(upstream=a, downstream=b, source="s1")
    svc.lineage.add_edge(upstream=b, downstream=c, source="s1")
    svc.lineage.add_edge(upstream=c, downstream=a, source="s1")
    up1 = svc.lineage.upstream_of(a, max_depth=100)
    up2 = svc.lineage.upstream_of(a, max_depth=100)
    down1 = svc.lineage.downstream_of(a, max_depth=100)
    log_case(
        "环上双向展开不死循环、结果稳定",
        {"start": "a", "graph": "a->b->c->a", "max_depth": 100},
        "BFS + visited 集合：每个节点只出现一次，按 (距离, 名称) 排序",
        {"upstream": [(r.name, d) for r, d, _ in up1],
         "downstream": [(r.name, d) for r, d, _ in down1]},
    )
    assert up1 == up2
    assert [(r.name, d) for r, d, _ in up1] == [("c", 1), ("b", 2)]
    assert [(r.name, d) for r, d, _ in down1] == [("b", 1), ("c", 2)]
    # 到达环上节点时应能看到 CYCLE 标记透传
    assert any(EdgeFlag.CYCLE in flags for _, _, flags in down1)


def test_duplicate_edge_is_flagged_not_inserted_twice(svc):
    a, b = _d(svc, "a", "b")
    svc.lineage.add_edge(upstream=a, downstream=b, source="s1")
    dup = svc.lineage.add_edge(upstream=a, downstream=b, source="s2")
    log_case(
        "重复边叠加 DUPLICATE，跨来源再加 KIND_CONFLICT",
        {"edge": "a->b", "sources": ["s1", "s2"]},
        "(upstream,downstream) 相同即重复；不新增边；不同来源视为语义冲突标记",
        sorted(f.value for f in dup.flags),
    )
    assert EdgeFlag.DUPLICATE in dup.flags
    assert EdgeFlag.KIND_CONFLICT in dup.flags
    assert len(svc.lineage.edges()) == 1


def test_same_source_duplicate_only_flagged_duplicate(svc):
    a, b = _d(svc, "a", "b")
    svc.lineage.add_edge(upstream=a, downstream=b, source="s1")
    dup = svc.lineage.add_edge(upstream=a, downstream=b, source="s1")
    log_case("同来源重复边不应打 KIND_CONFLICT", {"edge": "a->b", "sources": ["s1", "s1"]},
             "来源相同 -> 仅 DUPLICATE", sorted(f.value for f in dup.flags))
    assert dup.flags == {EdgeFlag.DUPLICATE}


def test_self_loop_rejected(svc):
    (a,) = _d(svc, "a")
    with pytest.raises(CatalogError) as ei:
        svc.lineage.add_edge(upstream=a, downstream=a, source="s1")
    log_case("自环被拒绝", {"upstream": "a", "downstream": "a"},
             "上下游为同一实体 -> EDGE_SELF_LOOP", ei.value.reason.value)
    assert ei.value.reason is RejectReason.EDGE_SELF_LOOP


def test_edge_to_missing_endpoint_rejected(svc):
    (a,) = _d(svc, "a")
    ghost = EntityRef(EntityKind.DATASET, "ghost")
    with pytest.raises(CatalogError) as ei:
        svc.lineage.add_edge(upstream=a, downstream=ghost, source="s1")
    log_case(
        "指向不存在实体的血缘写入被拒绝",
        {"upstream": "a", "downstream": "ghost"},
        "端点必须先存在 -> EDGE_ENDPOINT_NOT_FOUND，details 指明缺失端",
        {"reason": ei.value.reason.value, "missing": ei.value.details["missing"]},
    )
    assert ei.value.reason is RejectReason.EDGE_ENDPOINT_NOT_FOUND
    assert ei.value.details["missing"][0]["side"] == "downstream"


def test_dangling_mark_after_entity_gone(svc):
    a, b = _d(svc, "a", "b")
    edge = svc.lineage.add_edge(upstream=a, downstream=b, source="s1")
    # 通过正式删除接口移除实体；关联边不删，只等待悬空标记
    svc.entities.delete_dataset(name="b", actor="alice")
    marked = svc.lineage.mark_dangling_for_missing()
    log_case(
        "端点消失后边被标记 DANGLING 而不是丢失",
        {"edge": "a->b", "removed": "b"},
        "扫描端点存在性：下游不存在 -> DANGLING；边仍可枚举",
        {"marked": [f"{e.upstream.name}->{e.downstream.name}" for e in marked]},
    )
    assert EdgeFlag.DANGLING in edge.flags
    assert edge in marked
    assert len(svc.lineage.edges()) == 1
    # 实体重新注册后，悬空标记可以清除
    svc.entities.register_dataset(name="b", owner="alice", source="s1")
    still = svc.lineage.mark_dangling_for_missing()
    assert EdgeFlag.DANGLING not in edge.flags
    assert edge not in still


def test_field_level_lineage_both_directions(svc):
    svc.entities.register_dataset(name="t1", owner="x", source="s1")
    svc.entities.register_dataset(name="t2", owner="x", source="s1")
    svc.entities.add_field(dataset="t1", field=__import__("catalog").models.FieldInfo(
        name="id", data_type="bigint", source="s1"))
    svc.entities.add_field(dataset="t2", field=__import__("catalog").models.FieldInfo(
        name="uid", data_type="bigint", source="s1"))
    f1 = EntityRef(EntityKind.FIELD, "t1.id")
    f2 = EntityRef(EntityKind.FIELD, "t2.uid")
    svc.lineage.add_edge(upstream=f1, downstream=f2, source="s1")
    up = svc.lineage.upstream_of(f2)
    down = svc.lineage.downstream_of(f1)
    log_case("字段级血缘双向查询", {"edge": "t1.id->t2.uid"},
             "字段引用名 dataset.field；上下游均可展开",
             {"upstream": [r.name for r, _, _ in up], "downstream": [r.name for r, _, _ in down]})
    assert [r.name for r, _, _ in up] == ["t1.id"]
    assert [r.name for r, _, _ in down] == ["t2.uid"]


def test_two_separate_cycles_all_found(svc):
    refs = _d(svc, "a", "b", "c", "d")
    a, b, c, d = refs
    svc.lineage.add_edge(upstream=a, downstream=b, source="s1")
    svc.lineage.add_edge(upstream=b, downstream=a, source="s1")
    svc.lineage.add_edge(upstream=c, downstream=d, source="s1")
    svc.lineage.add_edge(upstream=d, downstream=c, source="s1")
    cycles = sorted(sorted(r.name for r in cyc) for cyc in svc.lineage.find_cycles())
    log_case("多个独立环都能被找出", {"edges": ["a<->b", "c<->d"]},
             "着色 DFS 找全部简单环并规范化", cycles)
    assert cycles == [["a", "b"], ["c", "d"]]
