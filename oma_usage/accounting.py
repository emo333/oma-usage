"""Clock-injected accounting, independent of GTK and collector transports."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class FocusPoint:
    observer: str
    app: str
    idle: bool
    now: datetime
    mono: float
    boot: float
    unlocked: bool | None


class FocusAccounting:
    def __init__(self, store):
        self.store = store
        self.resolve = lambda name: store.entity('app', 'app-id:' + name)
        self.reset()

    def reset(self):
        self.previous = None
        self.session_credited = False

    def snapshot(self, observer_id, seq, app_id, idle, now, mono, boot, unlocked):
        current = FocusPoint(observer_id, app_id, idle, now, mono, boot, unlocked)
        previous = self.previous
        valid = False
        if previous is not None:
            elapsed = mono - previous.mono
            wall = (now - previous.now).total_seconds()
            awake = boot - previous.boot
            connected = (observer_id == previous.observer and 0 < elapsed <= 15
                         and abs(wall-elapsed) < 1 and abs(awake-elapsed) < 1)
            available = connected and previous.unlocked is not None and current.unlocked is not None
            if available:
                self.store.coverage('app_focus', previous.now, now)
            valid = (available and previous.unlocked is True and current.unlocked is True
                     and not previous.idle and not current.idle)
            if valid:
                if previous.app:
                    entity_id = self.resolve(previous.app)
                    self.store.usage(entity_id, 'app_focus', previous.now, now,
                                     count=0 if self.session_credited else 1)
                    self.session_credited = True
        if not valid or previous is None or previous.app != current.app:
            self.session_credited = False
        self.previous = current


class ServiceAccounting:
    def __init__(self, store, source):
        self.store = store
        self.source = source
        self.reset()

    def reset(self):
        self.previous = None
        self.states = {}

    def update(self, states: dict[str, str], now: datetime, mono: float, boot: float):
        """Apply a reconciled complete state; baseline is never an observed start."""
        previous = self.previous
        valid = False
        if previous:
            before, old_mono, old_boot = previous
            elapsed = mono-old_mono
            valid = 0 <= elapsed <= 75 and abs((now-before).total_seconds()-elapsed) < 1 and abs((boot-old_boot)-elapsed) < 1
            if valid and elapsed > 0:
                self.store.coverage(self.source, before, now)
                for key, state in self.states.items():
                    if state == 'running':
                        entity_id = self.store.entity('service', key)
                        self.store.usage(entity_id, self.source, before, now)
        if valid:
            for key, state in states.items():
                if state == 'running' and key in self.states and self.states[key] != 'running':
                    entity_id = self.store.entity('service', key)
                    self.store.observe(entity_id, now, now)
                    self.store.add_usage(entity_id, self.source, now.date().isoformat(), count=1)
        self.states = states.copy()
        self.previous = (now, mono, boot)
