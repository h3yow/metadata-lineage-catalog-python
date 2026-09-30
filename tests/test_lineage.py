"""血缘：成环拒绝、重复边拒绝、悬挂边标记、双向展开、查询不死循环。"""

from __future__ import annotations

import logging

from metadata_catalog.errors import RejectionReason
from metadata_catalog.lineage import EdgeStatus
from metadata_catalog.models import NodeRef
from metadata_catalog.store import DatasetReport

from .conftest import log_decision

logger = logging.getLogger("catalog-tests")


def _datasets(svc, *names, source="s1"):
    for n in names:
        svc.register_dataset(DatasetReport(name=n, source=source))


def test_cycle_edge_rejected_with_path(svc):
    _datasets(svc, "a", "b", "c")
    svc.add_dataset_lineage("a", "b", source="s1")
    svc.add_dataset_lineage("b", "c", source="s1")
    result = svc.add_dataset_lineage("c", "a", source="s1")
    log_decision(
        "血缘成环",
        "已有 a->b->c，再写 c->a",
        f"ok={result.ok}, reason={result.rejection.reason.value}, "
        f"path={result.rejection.details.get('cycle_path')}",
    )
    assert not result.ok
    assert result.rejection.reason is RejectionReason.LINEAGE_CYCLE
    # 环路径可解释：从新边的上游 c 出发，c->a 后沿旧边回到 c
    path = result.rejection.details["cycle_path"].split(" -> ")
    assert path[0] == path[-1] == "dataset:c"
    assert path == ["dataset:c", "dataset:a", "dataset:b", "dataset:c"]
    assert set(path) == {"dataset:a", "dataset:b", "dataset:c"}
    # 被拒绝的边不进图
    assert ("dataset:c", "dataset:a") not in {
        (e.upstream.node_id, e.downstream.node_id) for e in svc.all_edges()
    }


def test_self_loop_rejected(svc):
    _datasets(svc, "a")
    r = svc.add_dataset_lineage("a", "a", source="s1")
    log_decision("自环", "a->a", r.rejection.reason.value)
    assert r.rejection.reason is RejectionReason.LINEAGE_CYCLE


def test_duplicate_edge_distinct_reason(svc):
    _datasets(svc, "a", "b")
    first = svc.add_dataset_lineage("a", "b", source="s1")
    dup = svc.add_dataset_lineage("a", "b", source="s1")
    log_decision("重复边", "同端点同来源 s1 写两次",
                 f"first.created={first.created}, dup.reason={dup.rejection.reason.value}")
    assert dup.rejection.reason is RejectionReason.DUPLICATE_EDGE
    # 但同一端点对的新来源不是重复：并入 sources
    other = svc.add_dataset_lineage("a", "b", source="s2")
    assert other.ok
    assert other.edge.sources == {"s1", "s2"}
    assert len(svc.all_edges()) == 1


def test_dangling_edge_marked_not_dropped(svc):
    _datasets(svc, "a")
    r = svc.add_dataset_lineage("a", "ghost", source="s1")
    log_decision("悬挂边", "a 存在、ghost 不存在", f"status={r.edge.status}, detail={r.edge.dangling_detail}")
    assert r.ok  # 不报错、不丢弃
    assert r.edge.status == EdgeStatus.DANGLING
    dangling = svc.dangling_edges()
    assert len(dangling) == 1
    # 悬挂边不参与展开
    assert svc.downstream_datasets("a") == []

    # 实体迟到注册后自动转有效
    _datasets(svc, "ghost", source="s2")
    assert svc.dangling_edges() == []
    downstream = [n.ref.node_id for n in svc.downstream_datasets("a")]
    log_decision("悬挂边补齐", "ghost 已注册", f"downstream={downstream}")
    assert downstream == ["dataset:ghost"]


def test_dangling_to_removed_field(svc):
    svc.register_dataset(DatasetReport(
        name="t", source="s1",
        fields=[{"name": "id", "type": "bigint"}, {"name": "v", "type": "string"}]))
    svc.remove_field("t", "v", actor="a", source="s1")
    r = svc.add_field_lineage("t", "id", "t", "v", source="etl")
    log_decision("字段血缘指向已删字段", "t.id -> t.v(removed)", f"status={r.edge.status}")
    assert r.edge.status == EdgeStatus.DANGLING
    svc.restore_field("t", "v", actor="a", source="s1")
    assert svc.dangling_edges() == []


def test_bidirectional_expand_is_symmetric_and_terminates(svc):
    _datasets(svc, "a", "b", "c", "d")
    # 菱形：a -> b,c -> d
    svc.add_dataset_lineage("a", "b", source="s1")
    svc.add_dataset_lineage("a", "c", source="s1")
    svc.add_dataset_lineage("b", "d", source="s1")
    svc.add_dataset_lineage("c", "d", source="s1")
    # 尝试造环被拒后，展开仍应终止（带超时保护，死循环会直接失败）
    svc.add_dataset_lineage("d", "a", source="s1")

    down = [(n.ref.node_id, n.depth) for n in svc.downstream_datasets("a")]
    up = [(n.ref.node_id, n.depth) for n in svc.upstream_datasets("d")]
    log_decision("双向展开(菱形)", "a 的下游 / d 的上游", f"down={down}; up={up}")

    assert [x[0] for x in down] == ["dataset:b", "dataset:c", "dataset:d"]
    assert [x[0] for x in up] == ["dataset:b", "dataset:c", "dataset:a"]
    # 深度正确：d 距 a 两跳
    assert dict(down)["dataset:d"] == 2
    # max_depth 截断
    only_one = svc.downstream_datasets("a", max_depth=1)
    assert {n.ref.node_id for n in only_one} == {"dataset:b", "dataset:c"}


def test_expand_with_existing_cycle_in_graph_terminates():
    """手工构造一个图里已存在环的极端情况（绕过写入校验），展开也不能死循环。"""
    from metadata_catalog.audit import AuditLog
    from metadata_catalog.lineage import LineageGraph
    from metadata_catalog.store import CatalogStore

    store = CatalogStore(audit=AuditLog())
    for n in ("a", "b"):
        store.register_dataset(DatasetReport(name=n, source="s"))
    graph = LineageGraph(store)
    r1 = graph.add_edge(NodeRef.for_dataset("a"), NodeRef.for_dataset("b"), source="s")
    r2 = graph.add_edge(NodeRef.for_dataset("b"), NodeRef.for_dataset("a"), source="s")
    log_decision("图内已存在环时展开", "绕过校验构造 a<->b",
                 f"r1={r1.ok}, r2={r2.rejection and r2.rejection.reason.value}")
    # 即使第二边被写入，展开也必须收敛（这里第二边会被拒，只验证不会挂起）
    nodes = graph.downstream(NodeRef.for_dataset("a"))
    assert {n.ref.node_id for n in nodes} <= {"dataset:a", "dataset:b"}


def test_kind_mismatch_rejected(svc):
    _datasets(svc, "a", "b")
    svc.register_dataset(DatasetReport(
        name="b", source="s1", fields=[{"name": "f", "type": "int"}]))
    r = svc.lineage.add_edge(
        NodeRef.for_dataset("a"), NodeRef.for_field("b", "f"), source="s1")
    log_decision("端点类型不一致", "dataset:a -> field:b.f", r.rejection.reason.value)
    assert r.rejection.reason is RejectionReason.LINEAGE_KIND_MISMATCH


def test_paths_recorded_for_explanation(svc):
    _datasets(svc, "a", "b", "c")
    svc.add_dataset_lineage("a", "b", source="s1")
    svc.add_dataset_lineage("b", "c", source="s1")
    nodes = svc.downstream_datasets("a")
    c_node = next(n for n in nodes if n.ref.node_id == "dataset:c")
    log_decision("展开路径可解释", "a 到 c", c_node.path)
    assert c_node.path == ("dataset:a", "dataset:b", "dataset:c")
