"""血缘图：写入、校验与双向展开。

语义约定：
- 边是有向的：upstream（生产者） -> downstream（消费者）。
- 端点必须同构：数据集只连数据集，字段只连字段（字段端点还要求归属数据集存在）。
- 重复边（同端点、同 source）确定性拒绝为 DUPLICATE_EDGE，不会产生第二条。
- 成环的边拒绝为 LINEAGE_CYCLE，使用带 visited 集合的 DFS 判定，绝不死循环。
- 端点当前不存在的边不丢弃：以 DANGLING 状态保留并明确标记；
  待端点补齐后重新校验可转为 ACTIVE。
- 只有 ACTIVE 边参与上下游展开；展开同样带 visited 集合，遇到环会收敛。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .audit import AuditLog, ChangeType
from .errors import EntityNotFoundError, Rejection, RejectionReason
from .models import EntityKind, NodeRef
from .store import CatalogStore


class EdgeStatus:
    ACTIVE = "active"
    DANGLING = "dangling"


@dataclass(frozen=True, slots=True)
class EdgeKey:
    upstream: NodeRef
    downstream: NodeRef
    source: str

    @property
    def identity(self) -> tuple[str, str]:
        return (self.upstream.node_id, self.downstream.node_id)


@dataclass(slots=True)
class LineageEdge:
    upstream: NodeRef
    downstream: NodeRef
    sources: set[str] = field(default_factory=set)
    status: str = EdgeStatus.ACTIVE
    dangling_detail: str = ""

    @property
    def identity(self) -> tuple[str, str]:
        return (self.upstream.node_id, self.downstream.node_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "upstream": self.upstream.node_id,
            "downstream": self.downstream.node_id,
            "sources": sorted(self.sources),
            "status": self.status,
            "dangling_detail": self.dangling_detail,
        }


@dataclass(slots=True)
class LineageResult:
    edge: LineageEdge | None = None
    created: bool = False
    activated: bool = False
    rejected: bool = False
    rejection: Rejection | None = None

    @property
    def ok(self) -> bool:
        return self.rejection is None and not self.rejected


@dataclass(frozen=True, slots=True)
class LineageNode:
    """展开结果中的一个节点，depth 表示距起点的跳数（环上重复访问时仍只记一次）。"""

    ref: NodeRef
    depth: int
    path: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"node": self.ref.node_id, "depth": self.depth, "path": list(self.path)}


class LineageGraph:
    def __init__(self, store: CatalogStore, audit: AuditLog | None = None) -> None:
        self._store = store
        self.audit = audit or store.audit
        # identity -> edge；一个端点对只保留一条边，多来源并入 sources
        self._edges: dict[tuple[str, str], LineageEdge] = {}

    # ---------------- 写入 ----------------

    def add_edge(
        self,
        upstream: NodeRef,
        downstream: NodeRef,
        *,
        actor: str = "anonymous",
        source: str = "unknown",
    ) -> LineageResult:
        # 1) 端点同构校验
        structural = self._check_structure(upstream, downstream)
        if structural is not None:
            self._audit_reject(actor, source, structural)
            return LineageResult(rejected=True, rejection=structural)

        edge = self._edges.get((upstream.node_id, downstream.node_id))

        # 2) 重复边：同端点 + 同 source
        if edge is not None and source in edge.sources:
            rejection = Rejection(
                reason=RejectionReason.DUPLICATE_EDGE,
                message="重复血缘边：相同端点与来源已存在",
                subject=f"{upstream.node_id} -> {downstream.node_id}",
                details={"source": source, "status": edge.status},
            )
            self._audit_reject(actor, source, rejection)
            return LineageResult(edge=edge, rejection=rejection)

        # 3) 存在性校验（悬挂）
        dangling = self._check_dangling(upstream) or self._check_dangling(downstream)
        if dangling is not None:
            if edge is None:
                edge = self._put_edge(upstream, downstream, source, EdgeStatus.DANGLING, dangling.message)
                self.audit.record(
                    actor, source, ChangeType.LINEAGE_MARKED_DANGLING,
                    self._edge_target(upstream, downstream),
                    note=f"血缘边标记为悬挂：{dangling.message}",
                )
                return LineageResult(edge=edge, created=True)
            # 已存在（可能 ACTIVE 或 DANGLING）：补来源，状态重新评估
            edge.sources.add(source)
            return self._reevaluate(edge, actor, source)

        # 4) 成环检测（仅在两端都解析得到时才有意义）
        cycle_path = self._find_cycle_path(upstream, downstream)
        if cycle_path is not None:
            rejection = Rejection(
                reason=RejectionReason.LINEAGE_CYCLE,
                message="该血缘边会形成环，已拒绝",
                subject=f"{upstream.node_id} -> {downstream.node_id}",
                details={"cycle_path": " -> ".join(cycle_path)},
            )
            self._audit_reject(actor, source, rejection)
            return LineageResult(edge=edge, rejection=rejection)

        # 5) 正常写入 / 新来源合并
        if edge is None:
            edge = self._put_edge(upstream, downstream, source, EdgeStatus.ACTIVE, "")
            self.audit.record(
                actor, source, ChangeType.LINEAGE_ADDED,
                self._edge_target(upstream, downstream),
                note=f"血缘边建立：{upstream.node_id} -> {downstream.node_id}",
            )
            return LineageResult(edge=edge, created=True)

        before_status = edge.status
        edge.sources.add(source)
        edge.status = EdgeStatus.ACTIVE
        edge.dangling_detail = ""
        activated = before_status != EdgeStatus.ACTIVE
        if activated:
            self.audit.record(
                actor, source, ChangeType.LINEAGE_RESOLVED,
                self._edge_target(upstream, downstream),
                note="端点已补齐，悬挂血缘边转为有效",
            )
        return LineageResult(edge=edge, created=False, activated=activated)

    def _put_edge(
        self, upstream: NodeRef, downstream: NodeRef, source: str,
        status: str, detail: str,
    ) -> LineageEdge:
        edge = LineageEdge(
            upstream=upstream,
            downstream=downstream,
            sources={source},
            status=status,
            dangling_detail=detail,
        )
        self._edges[edge.identity] = edge
        return edge

    def _reevaluate(self, edge: LineageEdge, actor: str, source: str) -> LineageResult:
        dangling = self._check_dangling(edge.upstream) or self._check_dangling(edge.downstream)
        if dangling is not None:
            edge.status = EdgeStatus.DANGLING
            edge.dangling_detail = dangling.message
            return LineageResult(edge=edge, created=False)
        cycle_path = self._find_cycle_path(edge.upstream, edge.downstream)
        if cycle_path is not None:
            rejection = Rejection(
                reason=RejectionReason.LINEAGE_CYCLE,
                message="该血缘边当前会形成环，保持悬挂状态",
                subject=self._edge_target(edge.upstream, edge.downstream),
                details={"cycle_path": " -> ".join(cycle_path)},
            )
            edge.status = EdgeStatus.DANGLING
            edge.dangling_detail = rejection.message
            self._audit_reject(actor, source, rejection)
            return LineageResult(edge=edge, rejected=True, rejection=rejection)
        before = edge.status
        edge.status = EdgeStatus.ACTIVE
        edge.dangling_detail = ""
        activated = before != EdgeStatus.ACTIVE
        if activated:
            self.audit.record(
                actor, source, ChangeType.LINEAGE_RESOLVED,
                self._edge_target(edge.upstream, edge.downstream),
                note="端点已补齐，血缘边转为有效",
            )
        return LineageResult(edge=edge, activated=activated)

    # ---------------- 重验 ----------------

    def revalidate_dangling(
        self, *, actor: str = "system", source: str = "system"
    ) -> list[LineageResult]:
        """实体补齐后批量重验所有悬挂边：能解析且不成环则转 ACTIVE。"""
        results: list[LineageResult] = []
        for edge in self.dangling_edges():
            results.append(self._reevaluate(edge, actor, source))
        return results

    # ---------------- 校验 ----------------

    def _check_structure(self, upstream: NodeRef, downstream: NodeRef) -> Rejection | None:
        if upstream.kind is not downstream.kind:
            return Rejection(
                reason=RejectionReason.LINEAGE_KIND_MISMATCH,
                message="血缘端点类型不一致：数据集只能连数据集，字段只能连字段",
                subject=f"{upstream.node_id} -> {downstream.node_id}",
                details={"upstream_kind": upstream.kind.value, "downstream_kind": downstream.kind.value},
            )
        if upstream.node_id == downstream.node_id:
            # 自环直接视为成环，给出明确原因
            return Rejection(
                reason=RejectionReason.LINEAGE_CYCLE,
                message="自环血缘：上下游是同一个实体",
                subject=upstream.node_id,
                details={"cycle_path": f"{upstream.node_id} -> {downstream.node_id}"},
            )
        return None

    def _check_dangling(self, ref: NodeRef) -> Rejection | None:
        try:
            dataset = self._store.get_dataset(ref.dataset)
        except EntityNotFoundError:
            return Rejection(
                reason=RejectionReason.DANGLING_REFERENCE,
                message=f"端点引用的数据集不存在: {ref.dataset}",
                subject=ref.node_id,
                details={"missing": f"dataset:{ref.dataset}"},
            )
        if ref.kind is EntityKind.FIELD:
            info = dataset.fields.get(ref.field or "")
            if info is None:
                return Rejection(
                    reason=RejectionReason.DANGLING_REFERENCE,
                    message=f"端点引用的字段不存在: {ref.dataset}.{ref.field}",
                    subject=ref.node_id,
                    details={"missing": ref.node_id},
                )
            from .models import FieldStatus

            if info.status is FieldStatus.REMOVED:
                return Rejection(
                    reason=RejectionReason.DANGLING_REFERENCE,
                    message=f"端点引用的字段已被删除: {ref.dataset}.{ref.field}",
                    subject=ref.node_id,
                    details={"missing": ref.node_id, "status": FieldStatus.REMOVED.value},
                )
        return None

    def _find_cycle_path(self, upstream: NodeRef, downstream: NodeRef) -> list[str] | None:
        """假设加入 downstream -> ... 的新边后，沿下游方向能否走回 upstream。

        即：若 downstream 已经能（沿 ACTIVE 边）到达 upstream，则成环。
        返回环上的节点路径；DFS 带 visited，环上自动收敛，不会死循环。
        """
        target = upstream.node_id
        # BFS + 父指针：返回最短的简单环路径；visited 保证环上也会收敛
        parent: dict[str, str | None] = {downstream.node_id: None}
        frontier = deque([downstream.node_id])
        while frontier:
            current = frontier.popleft()
            for edge in self._active_out(current):
                nxt = edge.downstream.node_id
                if nxt == target:
                    # 从 downstream 回溯到 current，再首尾补上 upstream
                    back: list[str] = []
                    node: str | None = current
                    while node is not None:
                        back.append(node)
                        node = parent[node]
                    back.reverse()
                    return [upstream.node_id, *back, target]
                if nxt in parent:
                    continue
                parent[nxt] = current
                frontier.append(nxt)
        return None

    def _active_out(self, node_id: str) -> list[LineageEdge]:
        return [
            e for e in self._edges.values()
            if e.status == EdgeStatus.ACTIVE and e.upstream.node_id == node_id
        ]

    def _audit_reject(self, actor: str, source: str, rejection: Rejection) -> None:
        self.audit.record(
            actor, source, ChangeType.REJECTION,
            rejection.subject or "lineage",
            note=f"[{rejection.reason.value}] {rejection.message}",
        )

    @staticmethod
    def _edge_target(upstream: NodeRef, downstream: NodeRef) -> str:
        return f"edge:{upstream.node_id}->{downstream.node_id}"

    # ---------------- 查询 ----------------

    def upstream(self, node: NodeRef, *, max_depth: int | None = None) -> list[LineageNode]:
        """沿反方向展开所有上游（BFS，结果按 (depth, node_id) 稳定排序）。"""
        return self._expand(node, reverse=True, max_depth=max_depth)

    def downstream(self, node: NodeRef, *, max_depth: int | None = None) -> list[LineageNode]:
        """沿正方向展开所有下游。"""
        return self._expand(node, reverse=False, max_depth=max_depth)

    def _expand(self, start: NodeRef, *, reverse: bool, max_depth: int | None) -> list[LineageNode]:
        visited: dict[str, LineageNode] = {}
        queue: deque[tuple[NodeRef, int, tuple[str, ...]]] = deque(
            [(start, 0, (start.node_id,))]
        )
        seen: set[str] = {start.node_id}
        while queue:
            ref, depth, path = queue.popleft()
            if depth > 0:
                visited[ref.node_id] = LineageNode(ref=ref, depth=depth, path=path)
            if max_depth is not None and depth >= max_depth:
                continue
            for edge in self._edges.values():
                if edge.status != EdgeStatus.ACTIVE:
                    continue
                if reverse:
                    if edge.downstream.node_id != ref.node_id:
                        continue
                    nxt = edge.upstream
                else:
                    if edge.upstream.node_id != ref.node_id:
                        continue
                    nxt = edge.downstream
                if nxt.node_id in seen:
                    continue
                seen.add(nxt.node_id)
                queue.append((nxt, depth + 1, (*path, nxt.node_id)))
        return sorted(visited.values(), key=lambda n: (n.depth, n.ref.node_id))

    def direct_upstreams(self, node: NodeRef) -> list[LineageEdge]:
        result = [
            e for e in self._edges.values()
            if e.status == EdgeStatus.ACTIVE and e.downstream.node_id == node.node_id
        ]
        return sorted(result, key=lambda e: e.upstream.node_id)

    def direct_downstreams(self, node: NodeRef) -> list[LineageEdge]:
        result = [
            e for e in self._edges.values()
            if e.status == EdgeStatus.ACTIVE and e.upstream.node_id == node.node_id
        ]
        return sorted(result, key=lambda e: e.downstream.node_id)

    def edges(self, *, include_dangling: bool = True) -> list[LineageEdge]:
        edges = list(self._edges.values())
        if not include_dangling:
            edges = [e for e in edges if e.status == EdgeStatus.ACTIVE]
        return sorted(edges, key=lambda e: (e.upstream.node_id, e.downstream.node_id))

    def dangling_edges(self) -> list[LineageEdge]:
        return sorted(
            (e for e in self._edges.values() if e.status == EdgeStatus.DANGLING),
            key=lambda e: (e.upstream.node_id, e.downstream.node_id),
        )
