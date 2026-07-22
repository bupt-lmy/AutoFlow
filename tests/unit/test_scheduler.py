"""Persistent Cron scheduling behavior tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from axonflow.config.models import TriggerConfig
from axonflow.core.scheduler import Scheduler


def test_cron_trigger_validates_expression_and_timezone() -> None:
    trigger = TriggerConfig(
        type="cron",
        cron="*/15 * * * *",
        timezone="Asia/Shanghai",
        input="Refresh the report",
    )

    assert trigger.cron == "*/15 * * * *"
    assert trigger.timezone == "Asia/Shanghai"

    with pytest.raises(ValidationError, match="Invalid cron expression"):
        TriggerConfig(type="cron", cron="not-a-cron")
    with pytest.raises(ValidationError, match="Unknown timezone"):
        TriggerConfig(type="cron", cron="0 * * * *", timezone="Mars/Olympus")


def test_scheduler_upserts_and_removes_live_workflow_job() -> None:
    scheduler = Scheduler()
    scheduler.upsert_job(
        "hourly-report",
        "0 * * * *",
        input_data="Generate report",
        timezone="Asia/Shanghai",
    )
    job = scheduler._jobs["hourly-report"]

    assert job.input_data == "Generate report"
    assert job.timezone == "Asia/Shanghai"
    assert job.next_run > datetime.now(UTC) - timedelta(seconds=1)

    job.next_run = datetime.now(UTC) - timedelta(seconds=1)
    assert job.should_run(datetime.now(UTC)) is True
    job.is_running = True
    assert job.should_run(datetime.now(UTC)) is False

    scheduler.upsert_job("hourly-report", "*/5 * * * *", input_data="Updated")
    assert scheduler._jobs["hourly-report"].cron_expr == "*/5 * * * *"
    assert scheduler._jobs["hourly-report"].input_data == "Updated"

    scheduler.remove_job("hourly-report")
    assert "hourly-report" not in scheduler._jobs
