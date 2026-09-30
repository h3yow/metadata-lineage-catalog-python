"""实体注册与更新：数据集、字段的注册、合并与拒绝规则。

合并规则（确定性、可解释）：
- 名称为空 / 类型为空等不合法上报 -> 立即拒绝（区分原因码）。
- 同一来源重复上报：若关键字段一致则幂等；owner 可被同来源更新（后写覆盖）。
- 不同来源重复上报：
    * owner 不一致 -> SOURCE_CONFLICT 拒绝；
    * tags 取并集；description 非空者先写为准（不互相覆盖）；
    * sources 累积记录所有上报来源。
- 字段 data_type 不可变：任何来源上报不同类型 -> FIELD_TYPE_CONFLICT 拒绝。
- 全量字段快照更新时：既有字段缺失 -> FIELD_MISSING_ON_UPDATE，整次上报原子拒绝，
  不允许静默丢字段。
"""

from __future__ import annotations

from catalog.audit import AuditLog
from catalog.errors import CatalogError, RejectReason
from catalog.models import DatasetInfo, FieldInfo, utc_now


def _validate_field_payload(payload: FieldInfo, dataset_name: str) -> None:
    if not payload.name or not payload.name.strip():
        raise CatalogError(
            RejectReason.FIELD_NAME_EMPTY,
            f"数据集 {dataset_name!r} 上报了名称为空的字段",
            details={"dataset": dataset_name},
        )
    if not payload.data_type or not payload.data_type.strip():
        raise CatalogError(
            RejectReason.FIELD_TYPE_EMPTY,
            f"字段 {payload.name!r} 缺少类型声明",
            details={"dataset": dataset_name, "field": payload.name},
        )


class EntityRegistry:
    """维护所有数据集与字段，并执行确定性的合并/拒绝规则。"""

    def __init__(self, audit: AuditLog) -> None:
        self._datasets: dict[str, DatasetInfo] = {}
        self._audit = audit

    # ------------------------------------------------------------------ #
    # 数据集
    # ------------------------------------------------------------------ #
    def register_dataset(
        self,
        *,
        name: str,
        owner: str,
        source: str,
        description: str = "",
        tags: frozenset[str] | None = None,
        actor: str | None = None,
    ) -> tuple[DatasetInfo, bool]:
        """注册数据集；已存在时按合并规则处理。返回 (数据集, 是否新建)。"""
        if not name or not name.strip():
            raise CatalogError(
                RejectReason.DATASET_NAME_EMPTY,
                "数据集名称不能为空",
            )
        actor = actor or source
        tags = tags or frozenset()
        existing = self._datasets.get(name)
        if existing is None:
            ds = DatasetInfo(
                name=name,
                owner=owner,
                source=source,
                description=description or "",
                tags=frozenset(tags),
                sources=frozenset({source}),
            )
            self._datasets[name] = ds
            self._audit.record(
                actor=actor,
                action="register_dataset",
                target=f"dataset:{name}",
                changes=(
                    ("owner", None, owner),
                    ("source", None, source),
                    ("description", None, description),
                    ("tags", None, sorted(tags)),
                ),
            )
            return ds, True

        # --- 已存在：执行确定性合并 ---
        if owner != existing.owner and source != existing.source:
            raise CatalogError(
                RejectReason.SOURCE_CONFLICT,
                (
                    f"数据集 {name!r} 已由来源 {existing.source!r} 上报，"
                    f"owner={existing.owner!r}；来源 {source!r} 上报 owner={owner!r}，"
                    "跨来源不允许变更 owner"
                ),
                details={
                    "dataset": name,
                    "existing_owner": existing.owner,
                    "existing_source": existing.source,
                    "incoming_owner": owner,
                    "incoming_source": source,
                },
            )

        changes: list[tuple[str, object, object]] = []
        if owner != existing.owner:  # 仅可能是同来源更新
            changes.append(("owner", existing.owner, owner))
            existing.owner = owner
        merged_tags = existing.tags | frozenset(tags)
        if merged_tags != existing.tags:
            changes.append(("tags", sorted(existing.tags), sorted(merged_tags)))
            existing.tags = merged_tags
        if description and not existing.description:
            changes.append(("description", existing.description, description))
            existing.description = description
        if source not in existing.sources:
            changes.append(("sources", sorted(existing.sources), sorted(existing.sources | {source})))
            existing.sources = existing.sources | {source}
        if changes:
            existing.updated_at = utc_now()
            self._audit.record(
                actor=actor,
                action="merge_dataset_report",
                target=f"dataset:{name}",
                changes=tuple(changes),
            )
        else:
            # 完全一致的重复上报：幂等，仍留审计痕迹，changes 为空
            self._audit.record(
                actor=actor,
                action="register_dataset_idempotent",
                target=f"dataset:{name}",
                changes=(),
            )
        return existing, False

    def update_dataset(
        self,
        *,
        name: str,
        source: str,
        owner: str | None = None,
        description: str | None = None,
        tags: frozenset[str] | None = None,
        fields_snapshot: list[FieldInfo] | None = None,
        actor: str | None = None,
    ) -> DatasetInfo:
        """更新数据集元数据，并可用全量快照对齐字段。

        只有该数据集的首个（权威）来源可以提交快照。快照必须包含全部既有字段，
        且字段类型不得冲突，否则整次更新被原子拒绝。
        """
        ds = self.get_dataset(name)
        actor = actor or source
        if source not in ds.sources:
            raise CatalogError(
                RejectReason.SOURCE_CONFLICT,
                f"来源 {source!r} 从未上报过数据集 {name!r}，不能更新它",
                details={"dataset": name, "source": source, "known_sources": sorted(ds.sources)},
            )

        changes: list[tuple[str, object, object]] = []
        if owner is not None and owner != ds.owner:
            changes.append(("owner", ds.owner, owner))
            ds.owner = owner
        if description is not None and description != ds.description:
            changes.append(("description", ds.description, description))
            ds.description = description
        if tags is not None:
            merged = ds.tags | frozenset(tags)
            if merged != ds.tags:
                changes.append(("tags", sorted(ds.tags), sorted(merged)))
                ds.tags = merged

        # 字段快照：先在临时副本上完成全部校验，全部通过才落库
        if fields_snapshot is not None:
            incoming: dict[str, FieldInfo] = {}
            for payload in fields_snapshot:
                _validate_field_payload(payload, name)
                if payload.name in incoming:
                    raise CatalogError(
                        RejectReason.FIELD_DUPLICATE_IN_BATCH,
                        f"快照中字段 {payload.name!r} 出现多次",
                        details={"dataset": name, "field": payload.name},
                    )
                existing_field = ds.fields.get(payload.name)
                if existing_field is not None and payload.data_type != existing_field.data_type:
                    raise CatalogError(
                        RejectReason.FIELD_TYPE_CONFLICT,
                        (
                            f"字段 {name}.{payload.name} 类型冲突："
                            f"已登记 {existing_field.data_type!r}，"
                            f"来源 {source!r} 上报 {payload.data_type!r}"
                        ),
                        details={
                            "dataset": name,
                            "field": payload.name,
                            "existing_type": existing_field.data_type,
                            "incoming_type": payload.data_type,
                            "source": source,
                        },
                    )
                incoming[payload.name] = payload

            missing = sorted(set(ds.fields) - set(incoming))
            if missing:
                raise CatalogError(
                    RejectReason.FIELD_MISSING_ON_UPDATE,
                    (
                        f"来源 {source!r} 的快照缺少既有字段 {missing}，"
                        "全量快照不允许静默丢弃字段"
                    ),
                    details={"dataset": name, "missing_fields": missing, "source": source},
                )

            # 校验通过，对齐字段
            for fname, payload in incoming.items():
                if fname in ds.fields:
                    old = ds.fields[fname]
                    merged_tags = old.tags | payload.tags
                    new_desc = old.description or payload.description
                    if (
                        merged_tags != old.tags
                        or new_desc != old.description
                        or source not in old.sources
                    ):
                        changes.append(
                            (
                                f"field:{fname}",
                                {
                                    "tags": sorted(old.tags),
                                    "description": old.description,
                                    "sources": sorted(old.sources),
                                },
                                {
                                    "tags": sorted(merged_tags),
                                    "description": new_desc,
                                    "sources": sorted(old.sources | {source}),
                                },
                            )
                        )
                    old.tags = merged_tags
                    old.description = new_desc
                    old.sources = old.sources | {source}
                    old.updated_at = utc_now()
                else:
                    new_field = FieldInfo(
                        name=fname,
                        data_type=payload.data_type,
                        source=source,
                        description=payload.description,
                        tags=frozenset(payload.tags),
                        sources=frozenset({source}),
                    )
                    ds.fields[fname] = new_field
                    changes.append(
                        (
                            f"field:{fname}",
                            None,
                            {"type": payload.data_type, "source": source},
                        )
                    )

        if changes:
            ds.updated_at = utc_now()
            self._audit.record(
                actor=actor,
                action="update_dataset",
                target=f"dataset:{name}",
                changes=tuple(changes),
            )
        return ds

    # ------------------------------------------------------------------ #
    # 字段
    # ------------------------------------------------------------------ #
    def add_field(
        self,
        *,
        dataset: str,
        field: FieldInfo,
        actor: str | None = None,
    ) -> tuple[FieldInfo, bool]:
        """向数据集登记单个字段。返回 (字段, 是否新建)。"""
        ds = self.get_dataset(dataset)
        _validate_field_payload(field, dataset)
        actor = actor or field.source
        existing = ds.fields.get(field.name)
        if existing is None:
            new_field = FieldInfo(
                name=field.name,
                data_type=field.data_type,
                source=field.source,
                description=field.description,
                tags=frozenset(field.tags),
                sources=frozenset({field.source}),
            )
            ds.fields[field.name] = new_field
            ds.updated_at = utc_now()
            self._audit.record(
                actor=actor,
                action="add_field",
                target=f"field:{dataset}.{field.name}",
                changes=(("data_type", None, field.data_type), ("source", None, field.source)),
            )
            return new_field, True

        if field.data_type != existing.data_type:
            raise CatalogError(
                RejectReason.FIELD_TYPE_CONFLICT,
                (
                    f"字段 {dataset}.{field.name} 类型冲突："
                    f"已登记 {existing.data_type!r}（来源 {existing.source!r}），"
                    f"来源 {field.source!r} 上报 {field.data_type!r}"
                ),
                details={
                    "dataset": dataset,
                    "field": field.name,
                    "existing_type": existing.data_type,
                    "existing_source": existing.source,
                    "incoming_type": field.data_type,
                    "incoming_source": field.source,
                },
            )

        # 同类型重复上报：幂等合并标签/描述/来源
        changes: list[tuple[str, object, object]] = []
        merged_tags = existing.tags | field.tags
        if merged_tags != existing.tags:
            changes.append(("tags", sorted(existing.tags), sorted(merged_tags)))
            existing.tags = merged_tags
        if field.description and not existing.description:
            changes.append(("description", existing.description, field.description))
            existing.description = field.description
        if field.source not in existing.sources:
            changes.append(
                ("sources", sorted(existing.sources), sorted(existing.sources | {field.source}))
            )
            existing.sources = existing.sources | {field.source}
        if changes:
            existing.updated_at = utc_now()
            self._audit.record(
                actor=actor,
                action="merge_field_report",
                target=f"field:{dataset}.{field.name}",
                changes=tuple(changes),
            )
        else:
            self._audit.record(
                actor=actor,
                action="add_field_idempotent",
                target=f"field:{dataset}.{field.name}",
                changes=(),
            )
        return existing, False

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def get_dataset(self, name: str) -> DatasetInfo:
        ds = self._datasets.get(name)
        if ds is None:
            raise CatalogError(
                RejectReason.DATASET_NOT_FOUND,
                f"数据集 {name!r} 不存在",
                details={"dataset": name},
            )
        return ds

    def find_field(self, dataset: str, field_name: str) -> FieldInfo:
        ds = self.get_dataset(dataset)
        f = ds.fields.get(field_name)
        if f is None:
            raise CatalogError(
                RejectReason.FIELD_NOT_FOUND,
                f"字段 {dataset}.{field_name} 不存在",
                details={"dataset": dataset, "field": field_name},
            )
        return f

    def all_datasets(self) -> list[DatasetInfo]:
        return [self._datasets[k] for k in sorted(self._datasets)]

    def delete_dataset(self, name: str, *, actor: str | None = None) -> None:
        """删除数据集（含字段）。关联血缘边不会被删除，而应由血缘层扫描后
        打上 DANGLING 标记，保证历史血缘不静默丢失。"""
        ds = self.get_dataset(name)
        del self._datasets[name]
        self._audit.record(
            actor=actor or ds.source,
            action="delete_dataset",
            target=f"dataset:{name}",
            changes=(("fields", sorted(ds.fields), []),),
        )

    def exists(self, ref) -> bool:
        """EntityRef -> 实体当前是否存在。"""
        from catalog.models import EntityKind  # 局部导入避免环

        if ref.kind is EntityKind.DATASET:
            return ref.name in self._datasets
        # field 引用形如 "dataset.field"
        if "." not in ref.name:
            return False
        ds_name, field_name = ref.name.split(".", 1)
        ds = self._datasets.get(ds_name)
        return ds is not None and field_name in ds.fields
