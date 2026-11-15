"""Atomic user and company budget reservations with receipt settlement."""

from dataclasses import dataclass
from threading import RLock
from uuid import uuid4

from poppy_controls.metered.backend import units


class DeniedError(Exception):
    pass


@dataclass(frozen=True)
class Hold:
    id: str
    scopes: tuple
    estimate: int
    owner: object


class AtomicLedger:
    """All budget mutations use one reentrant lock."""

    def __init__(self):
        self.lock = RLock()
        self.totals = {}
        self.holds = {}

    def restore(self, totals):
        with self.lock:
            self.totals = {key: units(value) for key, value in totals.items()}

    def restore_hold(self, scopes, estimate):
        """Already committed work is not a new admission, even if limits changed."""
        units(estimate)
        with self.lock:
            hold = Hold(uuid4().hex, tuple(scopes), estimate, self)
            self.holds[hold.id] = hold
            return hold

    def spent(self, key):
        with self.lock:
            return self.totals.get(key, 0)

    def reserved(self, key):
        with self.lock:
            return sum(h.estimate for h in self.holds.values() if key in h.scopes)

    def reserve(self, limits, estimate):
        units(estimate)
        limits = {key: units(value) for key, value in limits.items()}
        with self.lock:
            for key, limit in limits.items():
                if self.spent(key) + self.reserved(key) + estimate > limit:
                    raise DeniedError(f"Budget exhausted: {key}")
            hold = Hold(uuid4().hex, tuple(limits), estimate, self)
            self.holds[hold.id] = hold
            return hold

    def settle(self, token, actual):
        units(actual)
        if not isinstance(token, Hold) or token.owner is not self:
            raise ValueError("Reservation belongs to another ledger")
        with self.lock:
            hold = self.holds.get(token.id)
            if hold is None:
                return
            if hold is not token:
                raise ValueError("Invalid reservation")
            for key in hold.scopes:
                self.totals[key] = self.spent(key) + actual
            del self.holds[token.id]

    def cancel(self, token):
        if not isinstance(token, Hold) or token.owner is not self:
            raise ValueError("Reservation belongs to another ledger")
        with self.lock:
            hold = self.holds.get(token.id)
            if hold is not None and hold is not token:
                raise ValueError("Invalid reservation")
            self.holds.pop(token.id, None)
