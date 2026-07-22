"""Cron 定时调度器"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import structlog
from croniter import croniter

logger = structlog.get_logger()


@dataclass
class ScheduledJob:
    """定时任务"""

    workflow_id: str
    cron_expr: str
    input_data: str
    timezone: str = "UTC"
    is_running: bool = False
    last_run: datetime | None = None
    next_run: datetime = field(init=False)
    _cron: croniter = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.reschedule(datetime.now(UTC))

    def reschedule(self, after: datetime) -> None:
        """Calculate the next UTC fire time using the configured local timezone."""
        local_after = after.astimezone(ZoneInfo(self.timezone))
        self._cron = croniter(self.cron_expr, local_after)
        self.next_run = self._cron.get_next(datetime).astimezone(UTC)

    def should_run(self, now: datetime) -> bool:
        """判断当前时间是否应该触发"""
        return not self.is_running and now >= self.next_run


class Scheduler:
    """定时任务调度器"""

    def __init__(self) -> None:
        self._jobs: dict[str, ScheduledJob] = {}
        self._running = False
        self._run_callback = None  # 由 Engine 注入，实际执行工作流的回调

    def set_run_callback(self, callback) -> None:
        """设置工作流执行回调

        callback 签名: async def run(workflow_id: str, input_data: str) -> None
        """
        self._run_callback = callback

    def upsert_job(
        self,
        workflow_id: str,
        cron_expr: str,
        input_data: str = "",
        timezone: str = "UTC",
    ) -> None:
        """Add or update a durable workflow schedule without restarting the engine."""
        existing = self._jobs.get(workflow_id)
        if existing is None:
            self._jobs[workflow_id] = ScheduledJob(
                workflow_id=workflow_id,
                cron_expr=cron_expr,
                input_data=input_data,
                timezone=timezone,
            )
        else:
            existing.cron_expr = cron_expr
            existing.input_data = input_data
            existing.timezone = timezone
            if not existing.is_running:
                existing.reschedule(datetime.now(UTC))
        logger.info(
            "scheduler.job_upserted",
            workflow_id=workflow_id,
            cron=cron_expr,
            timezone=timezone,
        )

    def add_job(
        self,
        workflow_id: str,
        cron_expr: str,
        input_data: str = "",
        timezone: str = "UTC",
    ) -> None:
        """Backward-compatible alias for schedule registration."""
        self.upsert_job(workflow_id, cron_expr, input_data, timezone)

    def remove_job(self, workflow_id: str) -> None:
        if self._jobs.pop(workflow_id, None) is not None:
            logger.info("scheduler.job_removed", workflow_id=workflow_id)

    async def start(self) -> None:
        """启动调度循环"""
        self._running = True
        logger.info("scheduler.started", jobs=len(self._jobs))

        while self._running:
            now = datetime.now(UTC)
            for job in list(self._jobs.values()):
                if job.should_run(now):
                    job.is_running = True
                    asyncio.create_task(self._execute_job(job))
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """停止调度器"""
        self._running = False
        logger.info("scheduler.stopped")

    async def _execute_job(self, job: ScheduledJob) -> None:
        """执行定时任务"""
        logger.info(
            "scheduler.job_running",
            workflow_id=job.workflow_id,
        )
        try:
            if self._run_callback:
                await self._run_callback(job.workflow_id, job.input_data)
            else:
                logger.warning("scheduler.no_callback")
        except Exception as e:
            logger.error(
                "scheduler.job_failed",
                workflow_id=job.workflow_id,
                error=str(e),
            )
        finally:
            job.is_running = False
            job.last_run = datetime.now(UTC)
            job.reschedule(job.last_run)
