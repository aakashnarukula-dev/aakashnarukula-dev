"""Cron wiring: fires each enabled schedule at its configured local time."""

from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import Config, Schedule
from .engine import ReminderEngine
from .models import utcnow

log = logging.getLogger(__name__)

#: How often the engine sweeps for due retries and unresolved calls.
TICK_SECONDS = 15


class ReminderScheduler:
    """Owns the APScheduler instance driving reminders and the engine tick."""

    def __init__(self, config: Config, engine: ReminderEngine) -> None:
        self.config = config
        self.engine = engine
        self._scheduler = AsyncIOScheduler(timezone=config.timezone)

    def start(self) -> None:
        for schedule in self.config.schedules:
            if not schedule.enabled:
                log.info("schedule '%s' is disabled — not scheduling", schedule.id)
                continue
            trigger = CronTrigger.from_crontab(schedule.cron, timezone=self.config.timezone)
            self._scheduler.add_job(
                self._run_schedule,
                trigger=trigger,
                args=[schedule.id],
                id=f"reminder:{schedule.id}",
                replace_existing=True,
                # A late fire still matters — a dose is worth calling about.
                misfire_grace_time=300,
                coalesce=True,
                max_instances=1,
            )
            log.info(
                "scheduled '%s' (%s %s) -> %s",
                schedule.id,
                schedule.cron,
                self.config.timezone,
                self.config.recipient(schedule.recipient_id).name,
            )

        self._scheduler.add_job(
            self._tick,
            trigger=IntervalTrigger(seconds=TICK_SECONDS),
            id="engine:tick",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self._scheduler.start()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def next_run_times(self) -> dict[str, str | None]:
        result: dict[str, str | None] = {}
        for schedule in self.config.schedules:
            job = self._scheduler.get_job(f"reminder:{schedule.id}")
            next_run = getattr(job, "next_run_time", None) if job else None
            result[schedule.id] = next_run.isoformat() if next_run else None
        return result

    async def _run_schedule(self, schedule_id: str) -> None:
        schedule: Schedule = self.config.schedule(schedule_id)
        # Truncate to the minute so a duplicate fire maps onto the same run row.
        scheduled_for: datetime = utcnow().replace(second=0, microsecond=0)
        try:
            await self.engine.trigger_schedule(schedule, scheduled_for)
        except Exception:  # never let one bad schedule kill the scheduler thread
            log.exception("schedule '%s' failed to start", schedule_id)

    async def _tick(self) -> None:
        try:
            await self.engine.tick()
        except Exception:
            log.exception("engine tick failed")
