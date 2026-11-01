"""In-memory budgets for hints tied to a single transient event."""

from __future__ import annotations

from dataclasses import dataclass, field
import uuid


@dataclass
class TransientHints:
    event_id: str = ""
    started_at: float = 0.0
    uses: dict[tuple[str, str], int] = field(default_factory=dict)

    def begin(self, at: float) -> None:
        self.event_id = uuid.uuid4().hex
        self.started_at = at
        self.uses.clear()

    def clear(self) -> None:
        self.event_id = ""
        self.started_at = 0.0
        self.uses.clear()

    def valid(self, *, now: float, ttl_minutes: int) -> bool:
        age = now - self.started_at
        return bool(self.event_id) and self.started_at > 0 and 0 <= age < ttl_minutes * 60

    def take(self, kind: str, scope: str, *, limit: int, ttl_minutes: int, now: float) -> bool:
        if limit <= 0 or not self.valid(now=now, ttl_minutes=ttl_minutes):
            return False
        # Missing session identifiers share one bounded bucket, not a bypass.
        key = (kind, str(scope or ""))
        used = self.uses.get(key, 0)
        if used >= limit:
            return False
        self.uses[key] = used + 1
        return True
