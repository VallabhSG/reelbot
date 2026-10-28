from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from reelbot.budget import BudgetExceeded, BudgetGuard
from reelbot.series import Budget, Series


def test_monthly_cap_blocks_next_call(db):
    g = BudgetGuard(db, Budget(monthly_usd=1.0, per_reel_usd=10), "Asia/Kolkata")
    g.record("higgsfield", "video", 0.9, job_id=None)
    g.check(0.1, None)  # exactly at cap is fine
    with pytest.raises(BudgetExceeded, match="monthly cap"):
        g.check(0.11, None)


def test_per_reel_cap(db):
    g = BudgetGuard(db, Budget(monthly_usd=100, per_reel_usd=1.0), "Asia/Kolkata")
    g.record("higgsfield", "video", 0.8, job_id=7)
    g.check(0.8, job_id=8)  # other jobs unaffected
    with pytest.raises(BudgetExceeded, match="per-reel cap"):
        g.check(0.3, job_id=7)


def test_zero_cost_not_recorded(db):
    g = BudgetGuard(db, Budget(), "Asia/Kolkata")
    g.record("elevenlabs", "music", 0.0, job_id=1)
    assert db.spend_for_job(1) == 0


def test_month_start_uses_local_timezone(db):
    g = BudgetGuard(db, Budget(), "Asia/Kolkata")
    # 00:30 IST on Nov 1 is still Oct 31 in UTC; the month must start at Nov 1 00:00 IST
    now = datetime(2026, 11, 1, 0, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert g.month_start_utc(now) == "2026-10-31T18:30:00+00:00"


def test_series_rejects_config_over_per_reel_cap(series):
    raw = series.model_dump()
    raw["budget"]["per_reel_usd"] = 0.10
    with pytest.raises(ValidationError, match="per_reel_usd"):
        Series.model_validate(raw)
