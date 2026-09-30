"""CatalogService：聚合实体、血缘、检索与审计的对外门面。"""

from __future__ import annotations

from catalog.audit import AuditLog
from catalog.entities import EntityRegistry
from catalog.lineage import LineageGraph
from catalog.models import EntityRef
from catalog.search import SearchCriteria, search_datasets


class CatalogService:
    """元数据目录服务门面。"""

    def __init__(self) -> None:
        self.audit = AuditLog()
        self.entities = EntityRegistry(self.audit)
        self.lineage = LineageGraph(self.entities, self.audit)

    def history(self, target: str | None = None):
        """查询变更历史；target 形如 'dataset:orders' / 'field:orders.id' /
        'edge:a->b'，None 表示全部。"""
        return self.audit.history(target)

    def search(self, criteria: SearchCriteria):
        """组合条件检索数据集。"""
        return search_datasets(self.entities, criteria)

    def lineage_between(self, ref: EntityRef, direction: str, *, max_depth: int = 50):
        """direction='upstream' | 'downstream' 的便捷封装。"""
        if direction == "upstream":
            return self.lineage.upstream_of(ref, max_depth=max_depth)
        if direction == "downstream":
            return self.lineage.downstream_of(ref, max_depth=max_depth)
        raise ValueError(f"未知血缘方向: {direction!r}，应为 'upstream' 或 'downstream'")
