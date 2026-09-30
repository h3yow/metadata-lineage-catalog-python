"""变更审计：记录每次写入是谁、什么时候、改了什么。

审计日志只增不改；时钟可注入，方便测试得到确定的时间戳顺序。
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC
from enum import Enum


class ChangeType(str, Enum):
    DATASET_REGISTERED = "dataset_registered"
    DATASET_UPDATED = "dataset_updated"
    FIELD_ADDED = "field_added"
    FIELD_MERGED = "field_merged"
    FIELD_REMOVED = "field_removed"
    FIELD_RESTORED = "field_restored"
    LINEAGE_ADDED = "lineage_added"
    LINEAGE_DUPLICATE = "lineage_duplicate"
    LINEAGE_MARKED_DANGLING = "lineage_marked_dangling"
    LINEAGE_RESOLVED = "lineage_resolved"
    REJECTION = "rejection"


@dataclass(frozen=True, slots=True)
class AttrChange:
    old: object
    new: object


@dataclass(frozen=True, slots=True)
class AuditEntry:
    seq: int
    timestamp: str
    actor: str
    source: str
    action: ChangeType
    target: str
    changes: dict[str, AttrChange] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "actor": self.actor,
            "source": self.source,
            "action": self.action.value,
            "target": self.target,
            "changes": {k: {"old": v.old, "new": v.new} for k, v in self.changes.items()},
            "note": self.note,
        }


Clock = Callable[[], str]


def system_clock() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()


class AuditLog:
    """追加写的审计日志，带 target 二级索引。"""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock: Clock = clock or system_clock
        self._entries: list[AuditEntry] = []
        self._by_target: dict[str, deque[AuditEntry]] = defaultdict(deque)

    def record(
        self,
        actor: str,
        source: str,
        action: ChangeType,
        target: str,
        changes: dict[str, AttrChange] | None = None,
        note: str = "",
    ) -> AuditEntry:
        entry = AuditEntry(
            seq=len(self._entries) + 1,
            timestamp=self._clock(),
            actor=actor,
            source=source,
            action=action,
            target=target,
            changes=dict(changes or {}),
            note=note,
        )
        self._entries.append(entry)
        self._by_target[target].append(entry)
        return entry

    def history(self, target: str) -> list[AuditEntry]:
        """按时间正序返回某个实体的全部变更。"""
        return list(self._by_target.get(target, ()))

    def latest(self, target: str) -> AuditEntry | None:
        bucket = self._by_target.get(target)
        return bucket[-1] if bucket else None

    def all_entries(self) -> list[AuditEntry]:
        return list(self._entries)
