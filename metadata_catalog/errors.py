"""错误类型与可区分的拒绝原因。

所有不合法上报都必须带上明确的 ``RejectionReason``，
调用方据此区分"字段缺失""类型冲突""血缘成环"等不同情况，
而不是只拿到一个模糊的失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RejectionReason(str, Enum):
    """上报被拒绝的原因分类。"""

    # ---- 通用 ----
    INVALID_ARGUMENT = "invalid_argument"
    ENTITY_NOT_FOUND = "entity_not_found"

    # ---- 数据集/字段注册 ----
    DATASET_NAME_MISSING = "dataset_name_missing"
    FIELD_NAME_MISSING = "field_name_missing"
    FIELD_TYPE_MISSING = "field_type_missing"
    FIELD_TYPE_CONFLICT = "field_type_conflict"
    FIELD_DUPLICATED_IN_BATCH = "field_duplicated_in_batch"
    FIELD_REMOVED_THEN_REPORTED = "field_removed_then_reported"

    # ---- 血缘 ----
    LINEAGE_ENDPOINT_MISSING = "lineage_endpoint_missing"
    LINEAGE_KIND_MISMATCH = "lineage_kind_mismatch"
    DUPLICATE_EDGE = "duplicate_edge"
    LINEAGE_CYCLE = "lineage_cycle"
    DANGLING_REFERENCE = "dangling_reference"


@dataclass(frozen=True, slots=True)
class Rejection:
    """单条被拒绝的上报。

    details 保留判定依据，方便日志里解释"为什么被拒"。
    """

    reason: RejectionReason
    message: str
    subject: str | None = None
    details: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason.value,
            "message": self.message,
            "subject": self.subject,
            "details": dict(self.details),
        }


class CatalogError(Exception):
    """目录服务基础异常。"""


class EntityNotFoundError(CatalogError):
    """查询的实体不存在。"""

    def __init__(self, entity_kind: str, entity_id: str) -> None:
        self.entity_kind = entity_kind
        self.entity_id = entity_id
        super().__init__(f"{entity_kind} 不存在: {entity_id}")


class RejectedError(CatalogError):
    """单次强约束写入被整体拒绝（例如成环）。"""

    def __init__(self, rejection: Rejection) -> None:
        self.rejection = rejection
        super().__init__(rejection.message)
