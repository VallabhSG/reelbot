"""Hard spend caps. Every paid call is checked before it is made and recorded after it succeeds.

Costs are estimates from the per-model prices in the series YAML; keep those in sync with your
provider dashboards (Higgsfield bills failed requests at $0, so only successes are recorded).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .db import DB
from .series import Budget


class BudgetExceeded(RuntimeError):
    pass


class BudgetGuard:
    def __init__(self, db: DB, budget: Budget, tz: str):
        self.db = db
        self.budget = budget
        self.tz = ZoneInfo(tz)

    def month_start_utc(self, now: datetime | None = None) -> str:
        local = (now or datetime.now(self.tz)).astimezone(self.tz)
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(UTC).isoformat(timespec="seconds")

    def month_spend(self) -> float:
        return self.db.spend_since(self.month_start_utc())

    def check(self, estimate: float, job_id: int | None) -> None:
        month = self.month_spend()
        if month + estimate > self.budget.monthly_usd:
            raise BudgetExceeded(
                f"monthly cap ${self.budget.monthly_usd:.2f} would be exceeded "
                f"(spent ${month:.2f}, next call ~${estimate:.2f})"
            )
        if job_id is not None:
            job_total = self.db.spend_for_job(job_id)
            if job_total + estimate > self.budget.per_reel_usd:
                raise BudgetExceeded(
                    f"per-reel cap ${self.budget.per_reel_usd:.2f} would be exceeded for job {job_id} "
                    f"(spent ${job_total:.2f}, next call ~${estimate:.2f})"
                )

    def record(self, provider: str, description: str, amount: float, job_id: int | None) -> None:
        if amount > 0:
            self.db.record_spend(provider, description, amount, job_id)
