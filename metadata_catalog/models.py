"""实体数据模型：数据集、字段、血缘边。

模型本身只承载状态，合并/拒绝规则放在 store 层，
这样模型可以安全地在 API 与存储之间传递。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .errors import Rejection, RejectionReason


class EntityKind(str, Enum):
    DATASET = "dataset"
    FIELD = "field"


class FieldStatus(str, Enum):
    ACTIVE = "active"
    REMOVED = "removed"


# 允许的字段类型；统一转大写比较，避免 int/INT 被当成冲突
ALLOWED_FIELD_TYPES: frozenset[str] = frozenset(
    {
        "STRING",
        "INT",
        "BIGINT",
        "DOUBLE",
        "DECIMAL",
        "BOOLEAN",
        "DATE",
        "TIMESTAMP",
        "BINARY",
    }
)


def normalize_field_type(raw: str) -> str:
    return raw.strip().upper()


@dataclass(slots=True)
class FieldInfo:
    """数据集下的一个字段。"""

    name: str
    field_type: str
    sources: set[str] = field(default_factory=set)
    status: FieldStatus = FieldStatus.ACTIVE
    description: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "field_type": self.field_type,
            "sources": sorted(self.sources),
            "status": self.status.value,
            "description": self.description,
        }


@dataclass(slots=True)
class Dataset:
    """一个数据集实体。身份键是 name（规范化后大小写敏感、去首尾空白）。"""

    name: str
    owners: set[str] = field(default_factory=set)
    tags: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    description: str | None = None
    fields: dict[str, FieldInfo] = field(default_factory=dict)

    def get_field(self, field_name: str) -> FieldInfo | None:
        return self.fields.get(field_name)

    def to_dict(self, include_removed: bool = False) -> dict[str, object]:
        fields = [
            f.to_dict()
            for f in self.fields.values()
            if include_removed or f.status is FieldStatus.ACTIVE
        ]
        fields.sort(key=lambda f: f["name"])
        return {
            "name": self.name,
            "owners": sorted(self.owners),
            "tags": sorted(self.tags),
            "sources": sorted(self.sources),
            "description": self.description,
            "fields": fields,
        }


@dataclass(frozen=True, slots=True)
class NodeRef:
    """血缘端点：可以是数据集或字段。"""

    kind: EntityKind
    dataset: str
    field: str | None = None

    def __post_init__(self) -> None:
        if self.kind is EntityKind.FIELD and not self.field:
            raise ValueError("字段端点必须给出 field 名称")

    @property
    def node_id(self) -> str:
        if self.kind is EntityKind.FIELD:
            return f"field:{self.dataset}.{self.field}"
        return f"dataset:{self.dataset}"

    @classmethod
    def for_field(cls, dataset: str, field_name: str) -> NodeRef:
        return cls(EntityKind.FIELD, dataset, field_name)

    @classmethod
    def for_dataset(cls, name: str) -> NodeRef:
        return cls(EntityKind.DATASET, name, None)


def validate_field_type(raw: object, field_name: str, dataset: str) -> tuple[str | None, Rejection | None]:
    """校验字段类型，返回 (规范化类型, 拒绝)。二者恰有一个非 None。"""
    if not isinstance(raw, str) or not raw.strip():
        return None, Rejection(
            reason=RejectionReason.FIELD_TYPE_MISSING,
            message="字段类型缺失",
            subject=f"{dataset}.{field_name}",
            details={"dataset": dataset, "field": field_name},
        )
    normalized = normalize_field_type(raw)
    if normalized not in ALLOWED_FIELD_TYPES:
        return None, Rejection(
            reason=RejectionReason.FIELD_TYPE_MISSING,
            message=f"不支持的字段类型: {raw!r}",
            subject=f"{dataset}.{field_name}",
            details={"dataset": dataset, "field": field_name, "raw_type": str(raw)},
        )
    return normalized, None
