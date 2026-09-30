"""实体注册、确定性合并与检索。

合并规则（确定性，不依赖上报顺序的偶然结果）：

数据集层：
- 身份键是规范化后的 name（去首尾空白），不存在则创建，存在则更新；
- owners / tags / sources 取并集；description 非空时以"最新非空值"覆盖。

字段层（随数据集批量上报，逐字段独立判定，好的收下、坏的拒绝）：
- 同批次内字段名重复         -> FIELD_DUPLICATED_IN_BATCH
- 字段名缺失                 -> FIELD_NAME_MISSING
- 字段类型缺失/不支持         -> FIELD_TYPE_MISSING
- 已存在且类型不同           -> FIELD_TYPE_CONFLICT（拒绝，保留旧类型）
- 已存在且类型相同           -> 合并（source 取并集，记 FIELD_MERGED）
- 字段曾被显式删除后又上报    -> FIELD_REMOVED_THEN_REPORTED（需要先复活才能再改）
- 新字段                     -> 收下，记 FIELD_ADDED
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .audit import AttrChange, AuditLog, ChangeType
from .errors import EntityNotFoundError, RejectedError, Rejection, RejectionReason
from .models import Dataset, FieldInfo, FieldStatus, validate_field_type


@dataclass(slots=True)
class DatasetReport:
    """一次数据集上报的输入。"""

    name: str
    owner: str | None = None
    owners: list[str] | None = None
    tags: list[str] | None = None
    description: str | None = None
    fields: list[dict[str, object]] | None = None
    actor: str = "anonymous"
    source: str = "unknown"


@dataclass(slots=True)
class RegisterResult:
    dataset: Dataset
    created: bool
    added_fields: list[str] = field(default_factory=list)
    merged_fields: list[str] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)

    @property
    def accepted_fields(self) -> list[str]:
        return [*self.added_fields, *self.merged_fields]

    @property
    def ok(self) -> bool:
        return not self.rejections


def _norm_set(values: object) -> set[str]:
    if not values:
        return set()
    if isinstance(values, str):
        values = [values]
    return {str(v).strip() for v in values if str(v).strip()}


class CatalogStore:
    def __init__(self, audit: AuditLog | None = None) -> None:
        self._datasets: dict[str, Dataset] = {}
        self.audit = audit or AuditLog()

    # ---------------- 数据集 ----------------

    def register_dataset(self, report: DatasetReport) -> RegisterResult:
        name = report.name.strip() if isinstance(report.name, str) else ""
        if not name:
            rejection = Rejection(
                reason=RejectionReason.DATASET_NAME_MISSING,
                message="数据集名称缺失",
                details={"raw_name": str(report.name)},
            )
            self._log_rejection(report, str(report.name), rejection)
            raise RejectedError(rejection)

        existing = self._datasets.get(name)
        result = self._upsert_dataset(name, report, existing)
        self._merge_fields(name, report, result)
        return result

    def _upsert_dataset(
        self, name: str, report: DatasetReport, existing: Dataset | None
    ) -> RegisterResult:
        owners = _norm_set(report.owners)
        if report.owner:
            owners |= _norm_set([report.owner])
        tags = _norm_set(report.tags)

        if existing is None:
            dataset = Dataset(
                name=name,
                owners=set(owners),
                tags=set(tags),
                sources={report.source},
                description=report.description.strip()
                if isinstance(report.description, str) and report.description.strip()
                else None,
            )
            self._datasets[name] = dataset
            self.audit.record(
                report.actor, report.source,
                ChangeType.DATASET_REGISTERED, f"dataset:{name}",
                note=f"新数据集由 source={report.source} 注册",
            )
            return RegisterResult(dataset=dataset, created=True)

        changes: dict[str, AttrChange] = {}
        new_owners = owners - existing.owners
        new_tags = tags - existing.tags
        if new_owners:
            changes["owners"] = AttrChange(
                sorted(existing.owners), sorted(existing.owners | new_owners)
            )
            existing.owners |= new_owners
        if new_tags:
            changes["tags"] = AttrChange(
                sorted(existing.tags), sorted(existing.tags | new_tags)
            )
            existing.tags |= new_tags
        if report.source not in existing.sources:
            existing.sources.add(report.source)
        if (
            isinstance(report.description, str)
            and report.description.strip()
            and report.description.strip() != existing.description
        ):
            changes["description"] = AttrChange(existing.description, report.description.strip())
            existing.description = report.description.strip()
        self.audit.record(
            report.actor, report.source, ChangeType.DATASET_UPDATED,
            f"dataset:{name}", changes=changes,
            note="重复上报：owners/tags/sources 取并集，description 取最新非空值",
        )
        return RegisterResult(dataset=existing, created=False)

    # ---------------- 字段 ----------------

    def _reject(
        self,
        report: DatasetReport,
        reason: RejectionReason,
        message: str,
        subject: str,
        details: dict[str, str],
        result: RegisterResult,
    ) -> None:
        rejection = Rejection(
            reason=reason, message=message, subject=subject, details=dict(details)
        )
        result.rejections.append(rejection)
        self._log_rejection(report, f"field:{subject}", rejection)

    def _log_rejection(
        self, report: DatasetReport, target: str, rejection: Rejection
    ) -> None:
        self.audit.record(
            report.actor, report.source, ChangeType.REJECTION, target,
            note=f"[{rejection.reason.value}] {rejection.message}",
        )

    def _merge_fields(self, name: str, report: DatasetReport, result: RegisterResult) -> None:
        dataset = result.dataset
        seen_in_batch: set[str] = set()
        for raw_field in report.fields or []:
            raw_name = raw_field.get("name") if isinstance(raw_field, dict) else None
            field_name = raw_name.strip() if isinstance(raw_name, str) else ""
            subject = f"{name}.{field_name or '?'}"

            if not field_name:
                self._reject(
                    report, RejectionReason.FIELD_NAME_MISSING, "字段名缺失", subject,
                    {"dataset": name}, result,
                )
                continue
            if field_name in seen_in_batch:
                self._reject(
                    report, RejectionReason.FIELD_DUPLICATED_IN_BATCH,
                    f"同批次内字段重复: {field_name}", subject,
                    {"dataset": name, "field": field_name}, result,
                )
                continue
            seen_in_batch.add(field_name)

            norm_type, type_rejection = validate_field_type(
                raw_field.get("type"), field_name, name
            )
            if type_rejection is not None:
                self._reject(
                    report, type_rejection.reason, type_rejection.message, subject,
                    type_rejection.details, result,
                )
                continue

            current = dataset.fields.get(field_name)
            if current is not None and current.status is FieldStatus.REMOVED:
                self._reject(
                    report, RejectionReason.FIELD_REMOVED_THEN_REPORTED,
                    f"字段 {field_name} 已被删除，重新上报需先复活", subject,
                    {"dataset": name, "field": field_name}, result,
                )
                continue

            if current is None:
                info = FieldInfo(name=field_name, field_type=norm_type or "")
                info.sources.add(report.source)
                desc = raw_field.get("description") if isinstance(raw_field, dict) else None
                if isinstance(desc, str) and desc.strip():
                    info.description = desc.strip()
                dataset.fields[field_name] = info
                result.added_fields.append(field_name)
                self.audit.record(
                    report.actor, report.source, ChangeType.FIELD_ADDED,
                    f"field:{subject}",
                    changes={"type": AttrChange(None, norm_type)},
                    note=f"新字段，类型 {norm_type}，来源 {report.source}",
                )
            elif current.field_type != norm_type:
                self._reject(
                    report, RejectionReason.FIELD_TYPE_CONFLICT,
                    f"字段类型冲突: 已存在 {current.field_type}，上报 {norm_type}",
                    subject,
                    {"dataset": name, "field": field_name,
                     "existing_type": current.field_type, "reported_type": norm_type or ""},
                    result,
                )
            else:
                before = sorted(current.sources)
                current.sources.add(report.source)
                result.merged_fields.append(field_name)
                self.audit.record(
                    report.actor, report.source, ChangeType.FIELD_MERGED,
                    f"field:{subject}",
                    changes={"sources": AttrChange(before, sorted(current.sources))},
                    note="类型一致的重复上报：合并来源",
                )

    def remove_field(self, dataset_name: str, field_name: str, *, actor: str, source: str) -> None:
        dataset = self._require_dataset(dataset_name)
        info = dataset.fields.get(field_name)
        if info is None or info.status is FieldStatus.REMOVED:
            raise EntityNotFoundError("field", f"{dataset_name}.{field_name}")
        info.status = FieldStatus.REMOVED
        self.audit.record(
            actor, source, ChangeType.FIELD_REMOVED,
            f"field:{dataset_name}.{field_name}",
            changes={"status": AttrChange(FieldStatus.ACTIVE.value, FieldStatus.REMOVED.value)},
            note="字段软删除",
        )

    def restore_field(self, dataset_name: str, field_name: str, *, actor: str, source: str) -> FieldInfo:
        dataset = self._require_dataset(dataset_name)
        info = dataset.fields.get(field_name)
        if info is None:
            raise EntityNotFoundError("field", f"{dataset_name}.{field_name}")
        if info.status is FieldStatus.REMOVED:
            info.status = FieldStatus.ACTIVE
            self.audit.record(
                actor, source, ChangeType.FIELD_RESTORED,
                f"field:{dataset_name}.{field_name}",
                changes={"status": AttrChange(FieldStatus.REMOVED.value, FieldStatus.ACTIVE.value)},
                note="字段复活",
            )
        return info

    # ---------------- 查询 ----------------

    def _require_dataset(self, name: str) -> Dataset:
        dataset = self._datasets.get(name)
        if dataset is None:
            raise EntityNotFoundError("dataset", name)
        return dataset

    def get_dataset(self, name: str) -> Dataset:
        return self._require_dataset(name)

    def has_field(self, dataset_name: str, field_name: str) -> bool:
        dataset = self._datasets.get(dataset_name)
        if dataset is None:
            return False
        info = dataset.fields.get(field_name)
        return info is not None and info.status is FieldStatus.ACTIVE

    def search(
        self,
        *,
        name_contains: str | None = None,
        tags: list[str] | None = None,
        owners: list[str] | None = None,
        source: str | None = None,
    ) -> list[Dataset]:
        """多条件过滤：条件之间 AND，tags/owners 列表内部 OR。

        结果按 name 排序，保证同样的输入永远得到同样顺序的输出。
        """
        tag_set = _norm_set(tags)
        owner_set = _norm_set(owners)
        needle = (
            name_contains.strip().lower()
            if isinstance(name_contains, str) and name_contains.strip()
            else None
        )
        src = source.strip() if isinstance(source, str) and source.strip() else None

        matched: list[Dataset] = []
        for dataset in self._datasets.values():
            if needle is not None and needle not in dataset.name.lower():
                continue
            if tag_set and not (tag_set & dataset.tags):
                continue
            if owner_set and not (owner_set & dataset.owners):
                continue
            if src is not None and src not in dataset.sources:
                continue
            matched.append(dataset)
        matched.sort(key=lambda d: d.name)
        return matched

    def all_datasets(self) -> list[Dataset]:
        return sorted(self._datasets.values(), key=lambda d: d.name)
