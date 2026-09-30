"""领域模型：数据集、字段、血缘边与审计记录（仅类型定义）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def utc_now() -> datetime:
    """统一的 UTC 时间戳来源。"""
    return datetime.now(timezone.utc)


class EntityKind(str, Enum):
    """血缘端点的实体层级。"""

    DATASET = "dataset"
    FIELD = "field"


class EdgeFlag(str, Enum):
    """血缘边的异常标记。一条边可同时带多个标记。"""

    DANGLING = "dangling"  # 端点曾存在后被删除 / 指向不存在实体
    CYCLE = "cycle"  # 该边参与成环
    DUPLICATE = "duplicate"  # 重复上报
    KIND_CONFLICT = "kind_conflict"  # 同名重复上报但语义冲突


@dataclass(frozen=True)
class EntityRef:
    """血缘端点的稳定引用。"""

    kind: EntityKind
    name: str


@dataclass
class FieldInfo:
    """字段元数据。"""

    name: str
    data_type: str
    source: str
    description: str = ""
    tags: frozenset[str] = frozenset()
    sources: frozenset[str] = frozenset()  # 所有上报过该字段的来源
    registered_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass
class DatasetInfo:
    """数据集元数据。"""

    name: str
    owner: str
    source: str
    description: str = ""
    tags: frozenset[str] = frozenset()
    fields: dict[str, FieldInfo] = field(default_factory=dict)
    sources: frozenset[str] = frozenset()  # 所有上报过该数据集的来源
    registered_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(eq=False)
class LineageEdge:
    """一条血缘边：upstream -> downstream（上游产出被下游消费）。

    边一旦建立其端点与来源不变；flags 可变，用于给异常边打标记
    （成环/悬空/重复/语义冲突），而不是把边静默删除。
    相等性与哈希只取决于 (upstream, downstream)，即同方向同端点天然去重。
    """

    upstream: EntityRef
    downstream: EntityRef
    source: str
    registered_at: datetime = field(default_factory=utc_now)
    flags: set[EdgeFlag] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not isinstance(self.flags, set):
            self.flags = set(self.flags)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LineageEdge):
            return NotImplemented
        return self.upstream == other.upstream and self.downstream == other.downstream

    def __hash__(self) -> int:
        return hash((self.upstream, self.downstream))


@dataclass(frozen=True)
class AuditEntry:
    """一次变更的审计记录：谁、什么时候、改了什么。"""

    sequence: int
    timestamp: datetime
    actor: str
    action: str  # register_dataset / update_dataset / add_edge ...
    target: str  # 受影响实体的可读标识
    changes: tuple[tuple[str, object, object], ...]  # (字段路径, 旧值, 新值)
