"""Process-local runtime switches — spec 10 (/pause, /resume).

The poller asks this object before each round. It is intentionally in memory: a
restart always resumes polling, which is the safe default for a subscription
service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.db.models import utcnow


@dataclass(slots=True)
class RuntimeState:
    paused: bool = False
    paused_at: datetime | None = None

    def pause(self) -> bool:
        if self.paused:
            return False
        self.paused = True
        self.paused_at = utcnow()
        return True

    def resume(self) -> bool:
        if not self.paused:
            return False
        self.paused = False
        self.paused_at = None
        return True

    @property
    def status(self) -> str:
        return "paused" if self.paused else "running"
