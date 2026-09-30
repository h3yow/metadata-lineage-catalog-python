"""审计日志：记录每次变更的操作人、时间与差异。"""

from __future__ import annotations

from datetime import datetime

from catalog.models import AuditEntry, utc_now


class AuditLog:
    """顺序追加的变更审计日志。"""

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def record(
        self,
        *,
        actor: str,
        action: str,
        target: str,
        changes: tuple[tuple[str, object, object], ...] = (),
        timestamp: datetime | None = None,
    ) -> AuditEntry:
        if timestamp is None:
            timestamp = utc_now()
        entry = AuditEntry(
            sequence=len(self._entries) + 1,
            timestamp=timestamp,
            actor=actor,
            action=action,
            target=target,
            changes=tuple(changes),
        )
        self._entries.append(entry)
        return entry

    def history(self, target: str | None = None) -> list[AuditEntry]:
        """按序号升序返回审计记录；可按目标标识过滤。"""
        if target is None:
            return list(self._entries)
        return [e for e in self._entries if e.target == target]
