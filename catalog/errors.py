"""错误与拒绝原因定义。

所有不合法上报都必须给出可区分的拒绝原因（RejectReason），
而不是被静默忽略。CatalogError 携带原因码与可读说明。
"""

from __future__ import annotations

from enum import Enum


class RejectReason(str, Enum):
    """上报被拒绝的原因码。"""

    # --- 数据集相关 ---
    DATASET_NOT_FOUND = "dataset_not_found"
    DATASET_NAME_EMPTY = "dataset_name_empty"
    DATASET_ALREADY_EXISTS = "dataset_already_exists"
    SOURCE_CONFLICT = "source_conflict"

    # --- 字段相关 ---
    FIELD_NAME_EMPTY = "field_name_empty"
    FIELD_TYPE_EMPTY = "field_type_empty"
    FIELD_NOT_FOUND = "field_not_found"
    FIELD_TYPE_CONFLICT = "field_type_conflict"
    FIELD_DUPLICATE_IN_BATCH = "field_duplicate_in_batch"
    FIELD_MISSING_ON_UPDATE = "field_missing_on_update"

    # --- 血缘相关 ---
    EDGE_ENDPOINT_NOT_FOUND = "edge_endpoint_not_found"
    EDGE_SELF_LOOP = "edge_self_loop"
    EDGE_DUPLICATE = "edge_duplicate"
    EDGE_CYCLE = "edge_cycle"
    EDGE_KIND_CONFLICT = "edge_kind_conflict"
    EDGE_NOT_FOUND = "edge_not_found"


class CatalogError(Exception):
    """目录服务业务异常，携带结构化拒绝原因。"""

    def __init__(self, reason: RejectReason, message: str, *, details: dict | None = None):
        self.reason = reason
        self.message = message
        self.details: dict = details or {}
        super().__init__(f"[{reason.value}] {message}")
