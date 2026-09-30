"""血缘关系：写入、双向查询、环检测与异常标记。

血缘语义：
- 边 upstream -> downstream 表示“上游产出的数据被下游消费”。
- 写入时两端实体必须存在，否则拒绝（EDGE_ENDPOINT_NOT_FOUND），不会登记悬空边。
- 自环（同一实体连自己）直接拒绝（EDGE_SELF_LOOP）。
- 重复边（同上游、同下游、同方向）：不会重复插入，而是在原边上叠加 DUPLICATE
  标记并留审计；如果重复上报来自不同 source，再叠加 KIND_CONFLICT 标记
  （边本身不丢，判定依据可解释）。
- 会成环的新边：不拒绝、不静默丢掉，写入后叠加 CYCLE 标记。
- 双向展开（upstream_of / downstream_of）使用 visited 集合 + 可选深度上限，
  遇到环不会死循环；结果按 (距离, 名称) 稳定排序，并携带每条路径边上的标记。
"""

from __future__ import annotations

from collections import deque

from catalog.audit import AuditLog
from catalog.entities import EntityRegistry
from catalog.errors import CatalogError, RejectReason
from catalog.models import EdgeFlag, EntityRef, LineageEdge

# 展开时的默认最大跳数，防止极端图造成无界遍历
DEFAULT_MAX_DEPTH = 50


class LineageGraph:
    """有向血缘图（upstream -> downstream）。"""

    def __init__(self, entities: EntityRegistry, audit: AuditLog) -> None:
        self._entities = entities
        self._audit = audit
        # key = (upstream, downstream)，同方向同端点天然唯一
        self._edges: dict[tuple[EntityRef, EntityRef], LineageEdge] = {}

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def add_edge(
        self,
        *,
        upstream: EntityRef,
        downstream: EntityRef,
        source: str,
        actor: str | None = None,
    ) -> LineageEdge:
        actor = actor or source

        if upstream == downstream:
            raise CatalogError(
                RejectReason.EDGE_SELF_LOOP,
                f"自环血缘被拒绝：{upstream.name!r} -> {downstream.name!r}",
                details={"upstream": str(upstream), "downstream": str(downstream)},
            )
        missing = []
        if not self._entities.exists(upstream):
            missing.append(("upstream", upstream))
        if not self._entities.exists(downstream):
            missing.append(("downstream", downstream))
        if missing:
            raise CatalogError(
                RejectReason.EDGE_ENDPOINT_NOT_FOUND,
                "血缘端点不存在：" + ", ".join(f"{side}={ref.name}" for side, ref in missing),
                details={
                    "missing": [{"side": s, "kind": r.kind.value, "name": r.name} for s, r in missing],
                    "source": source,
                },
            )

        key = (upstream, downstream)
        existing = self._edges.get(key)
        if existing is not None:
            added: list[EdgeFlag] = []
            if EdgeFlag.DUPLICATE not in existing.flags:
                existing.flags.add(EdgeFlag.DUPLICATE)
                added.append(EdgeFlag.DUPLICATE)
            if existing.source != source and EdgeFlag.KIND_CONFLICT not in existing.flags:
                existing.flags.add(EdgeFlag.KIND_CONFLICT)
                added.append(EdgeFlag.KIND_CONFLICT)
            # 重复边可能恰好补成一个环；统一重算所有边的 CYCLE 标记
            self.refresh_cycle_flags()
            if EdgeFlag.CYCLE in existing.flags:
                added.append(EdgeFlag.CYCLE)
            self._audit.record(
                actor=actor,
                action="add_edge_duplicate",
                target=f"edge:{upstream.name}->{downstream.name}",
                changes=(
                    ("existing_source", existing.source, source),
                    ("flags_added", [], [f.value for f in added]),
                ),
            )
            return existing

        edge = LineageEdge(upstream=upstream, downstream=downstream, source=source)
        self._edges[key] = edge
        # 全量重算：参与环的每条边都会带上 CYCLE（而不仅是闭合边）
        self.refresh_cycle_flags()
        self._audit.record(
            actor=actor,
            action="add_edge",
            target=f"edge:{upstream.name}->{downstream.name}",
            changes=(
                ("upstream", None, upstream.name),
                ("downstream", None, downstream.name),
                ("cycle", None, EdgeFlag.CYCLE in edge.flags),
            ),
        )
        return edge

    def mark_dangling_for_missing(self) -> list[LineageEdge]:
        """扫描全图，给端点已不存在的边叠加 DANGLING 标记；返回被标记的边。

        不删除任何边；若实体重新出现（重新注册同名实体），标记会被清除。
        """
        affected: list[LineageEdge] = []
        for edge in self._edges.values():
            endpoints_exist = self._entities.exists(edge.upstream) and self._entities.exists(
                edge.downstream
            )
            if not endpoints_exist:
                if EdgeFlag.DANGLING not in edge.flags:
                    edge.flags.add(EdgeFlag.DANGLING)
                    affected.append(edge)
            elif EdgeFlag.DANGLING in edge.flags:
                edge.flags.discard(EdgeFlag.DANGLING)
        return affected

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def upstream_of(
        self, ref: EntityRef, *, max_depth: int = DEFAULT_MAX_DEPTH
    ) -> list[tuple[EntityRef, int, frozenset[EdgeFlag]]]:
        """返回所有上游（谁的数据流到了 ref）。结果稳定、不重复、环安全。"""
        reverse: dict[EntityRef, set[EntityRef]] = {}
        for (u, d), edge in self._edges.items():
            reverse.setdefault(d, set()).add(u)
        return self._walk_directed(ref, reverse, forward=False, max_depth=max_depth)

    def downstream_of(
        self, ref: EntityRef, *, max_depth: int = DEFAULT_MAX_DEPTH
    ) -> list[tuple[EntityRef, int, frozenset[EdgeFlag]]]:
        """返回所有下游（ref 的数据流向了谁）。结果稳定、不重复、环安全。"""
        forward: dict[EntityRef, set[EntityRef]] = {}
        for (u, d), edge in self._edges.items():
            forward.setdefault(u, set()).add(d)
        return self._walk_directed(ref, forward, forward=True, max_depth=max_depth)

    def _walk_directed(
        self,
        start: EntityRef,
        adjacency: dict[EntityRef, set[EntityRef]],
        *,
        forward: bool,
        max_depth: int,
    ) -> list[tuple[EntityRef, int, frozenset[EdgeFlag]]]:
        visited: dict[EntityRef, int] = {start: 0}
        flag_acc: dict[EntityRef, set[EdgeFlag]] = {}
        queue: deque[tuple[EntityRef, int]] = deque([(start, 0)])
        while queue:
            node, dist = queue.popleft()
            if dist >= max_depth:
                continue
            for nxt in sorted(adjacency.get(node, ()), key=lambda r: r.name):
                if nxt in visited:
                    continue
                visited[nxt] = dist + 1
                edge_key = (node, nxt) if forward else (nxt, node)
                edge = self._edges.get(edge_key)
                if edge is not None and edge.flags:
                    flag_acc.setdefault(nxt, set()).update(edge.flags)
                queue.append((nxt, dist + 1))
        result = [
            (node, dist, frozenset(flag_acc.get(node, ())))
            for node, dist in visited.items()
            if node != start
        ]
        result.sort(key=lambda item: (item[1], item[0].kind.value, item[0].name))
        return result

    def find_cycles(self) -> list[list[EntityRef]]:
        """用带着色的 DFS 找出所有简单环（Tarjan 思路的轻量版本）。

        返回去重后的环（每个环节点序列以最小元素为起点，便于稳定展示）。
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[EntityRef, int] = {}
        stack: list[EntityRef] = []
        raw_cycles: set[tuple[EntityRef, ...]] = set()

        adjacency: dict[EntityRef, set[EntityRef]] = {}
        nodes: set[EntityRef] = set()
        for (u, d) in self._edges:
            adjacency.setdefault(u, set()).add(d)
            nodes.add(u)
            nodes.add(d)

        def dfs(node: EntityRef) -> None:
            color[node] = GRAY
            stack.append(node)
            for nxt in sorted(adjacency.get(node, ()), key=lambda r: r.name):
                c = color.get(nxt, WHITE)
                if c == GRAY:
                    idx = stack.index(nxt)
                    cycle = stack[idx:]
                    # 规范化：以最小节点为起点
                    min_idx = min(range(len(cycle)), key=lambda i: cycle[i].name)
                    normalized = tuple(cycle[min_idx:] + cycle[:min_idx])
                    raw_cycles.add(normalized)
                elif c == WHITE:
                    dfs(nxt)
            stack.pop()
            color[node] = BLACK

        for node in sorted(nodes, key=lambda r: r.name):
            if color.get(node, WHITE) == WHITE:
                dfs(node)
        return [list(c) for c in sorted(raw_cycles, key=lambda tpl: tuple(r.name for r in tpl))]

    def refresh_cycle_flags(self) -> list[LineageEdge]:
        """根据当前图重新计算所有边的 CYCLE 标记，返回处于环上的边。"""
        on_cycle: set[tuple[EntityRef, EntityRef]] = set()
        for cycle in self.find_cycles():
            for i, node in enumerate(cycle):
                nxt = cycle[(i + 1) % len(cycle)]
                on_cycle.add((node, nxt))
        affected: list[LineageEdge] = []
        for key, edge in self._edges.items():
            if key in on_cycle:
                if EdgeFlag.CYCLE not in edge.flags:
                    edge.flags.add(EdgeFlag.CYCLE)
                    affected.append(edge)
            else:
                edge.flags.discard(EdgeFlag.CYCLE)
        return affected

    def edges(self) -> list[LineageEdge]:
        """所有边，按 (上游名, 下游名) 稳定排序。"""
        return [
            self._edges[k]
            for k in sorted(self._edges, key=lambda kv: (kv[0].name, kv[1].name))
        ]
