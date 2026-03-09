import logging
from datetime import datetime, timezone

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.db.models import Reminder, User

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

_bot: Bot | None = None
_session_factory: async_sessionmaker | None = None


async def fire_reminder(reminder_id: int) -> None:
    assert _bot and _session_factory
    async with _session_factory() as session:
        reminder = await session.get(Reminder, reminder_id)
        if not reminder or not reminder.is_active:
            return
        user = await session.get(User, reminder.user_id)
        if not user:
            return
        try:
            await _bot.send_message(user.telegram_id, f"🔔 {reminder.text}")
        except Exception:
            logger.exception("Failed to send reminder %d", reminder_id)
            return
        if not reminder.cron_expr:
            reminder.is_active = False
            await session.commit()


def schedule_reminder(reminder: Reminder) -> None:
    job_id = f"reminder_{reminder.id}"
    if reminder.cron_expr:
        trigger = CronTrigger.from_crontab(reminder.cron_expr)
    elif reminder.fire_at:
        fire_at = reminder.fire_at
        if fire_at.tzinfo is None:
            fire_at = fire_at.replace(tzinfo=timezone.utc)
        if fire_at <= datetime.now(timezone.utc):
            return
        trigger = DateTrigger(run_date=fire_at)
    else:
        return
    scheduler.add_job(
        fire_reminder,
        trigger=trigger,
        args=[reminder.id],
        id=job_id,
        replace_existing=True,
    )


def cancel_reminder(reminder_id: int) -> None:
    job_id = f"reminder_{reminder_id}"
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass


async def start_scheduler(bot: Bot, session_factory: async_sessionmaker) -> None:
    global _bot, _session_factory
    _bot = bot
    _session_factory = session_factory

    async with session_factory() as session:
        result = await session.execute(
            select(Reminder).where(Reminder.is_active == True)  # noqa: E712
        )
        for reminder in result.scalars():
            schedule_reminder(reminder)
            logger.info("Restored reminder %d", reminder.id)

    scheduler.start()
    logger.info("Scheduler started")
