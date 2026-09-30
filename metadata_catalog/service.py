"""对外服务门面：组合实体注册、血缘与审计，并暴露检索/历史查询。

这是调用方（API、脚本、测试）唯一需要直接使用的类。
"""

from __future__ import annotations

from .audit import AuditEntry, AuditLog
from .lineage import LineageEdge, LineageGraph, LineageNode, LineageResult
from .models import Dataset, NodeRef
from .store import CatalogStore, DatasetReport, RegisterResult


class MetadataCatalogService:
    def __init__(self, audit: AuditLog | None = None) -> None:
        self.audit = audit or AuditLog()
        self.store = CatalogStore(audit=self.audit)
        self.lineage = LineageGraph(self.store, audit=self.audit)

    # ---- 实体 ----

    def register_dataset(self, report: DatasetReport) -> RegisterResult:
        result = self.store.register_dataset(report)
        # 迟到注册可能补齐了悬挂血缘的端点，重验一次
        self.lineage.revalidate_dangling(actor=report.actor, source=report.source)
        return result

    def get_dataset(self, name: str) -> Dataset:
        return self.store.get_dataset(name)

    def remove_field(self, dataset_name: str, field_name: str, *, actor: str, source: str) -> None:
        self.store.remove_field(dataset_name, field_name, actor=actor, source=source)

    def restore_field(self, dataset_name: str, field_name: str, *, actor: str, source: str):
        restored = self.store.restore_field(dataset_name, field_name, actor=actor, source=source)
        # 复活后重新评估挂在该字段上的悬挂边
        self.lineage.revalidate_dangling(actor=actor, source=source)
        return restored

    # ---- 血缘 ----

    def add_dataset_lineage(
        self, upstream: str, downstream: str, *, actor: str = "anonymous", source: str = "unknown"
    ) -> LineageResult:
        return self.lineage.add_edge(
            NodeRef.for_dataset(upstream), NodeRef.for_dataset(downstream),
            actor=actor, source=source,
        )

    def add_field_lineage(
        self,
        upstream_dataset: str,
        upstream_field: str,
        downstream_dataset: str,
        downstream_field: str,
        *,
        actor: str = "anonymous",
        source: str = "unknown",
    ) -> LineageResult:
        return self.lineage.add_edge(
            NodeRef.for_field(upstream_dataset, upstream_field),
            NodeRef.for_field(downstream_dataset, downstream_field),
            actor=actor, source=source,
        )

    def upstream_datasets(self, dataset_name: str, *, max_depth: int | None = None) -> list[LineageNode]:
        return self.lineage.upstream(NodeRef.for_dataset(dataset_name), max_depth=max_depth)

    def downstream_datasets(self, dataset_name: str, *, max_depth: int | None = None) -> list[LineageNode]:
        return self.lineage.downstream(NodeRef.for_dataset(dataset_name), max_depth=max_depth)

    def upstream_fields(
        self, dataset_name: str, field_name: str, *, max_depth: int | None = None
    ) -> list[LineageNode]:
        return self.lineage.upstream(NodeRef.for_field(dataset_name, field_name), max_depth=max_depth)

    def downstream_fields(
        self, dataset_name: str, field_name: str, *, max_depth: int | None = None
    ) -> list[LineageNode]:
        return self.lineage.downstream(NodeRef.for_field(dataset_name, field_name), max_depth=max_depth)

    def all_edges(self, *, include_dangling: bool = True) -> list[LineageEdge]:
        return self.lineage.edges(include_dangling=include_dangling)

    def dangling_edges(self) -> list[LineageEdge]:
        return self.lineage.dangling_edges()

    # ---- 检索 ----

    def search_datasets(
        self,
        *,
        name_contains: str | None = None,
        tags: list[str] | None = None,
        owners: list[str] | None = None,
        source: str | None = None,
    ) -> list[Dataset]:
        return self.store.search(
            name_contains=name_contains, tags=tags, owners=owners, source=source
        )

    # ---- 审计 ----

    def history(self, target: str) -> list[AuditEntry]:
        """查询某实体的变更历史。

        target 形如 ``dataset:orders``、``field:orders.id`` 或
        ``edge:dataset:a->dataset:b``，与审计记录里的 target 一致。
        """
        return self.audit.history(target)

    def dataset_history(self, name: str) -> list[AuditEntry]:
        return self.audit.history(f"dataset:{name}")

    def field_history(self, dataset_name: str, field_name: str) -> list[AuditEntry]:
        return self.audit.history(f"field:{dataset_name}.{field_name}")

    def latest_change(self, target: str) -> AuditEntry | None:
        return self.audit.latest(target)
