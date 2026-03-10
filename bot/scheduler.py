import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.db.models import Reminder, User

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

_bot: Bot | None = None
_session_factory: async_sessionmaker | None = None

ACK_INTERVAL_SEC = 120  # repeat every 2 minutes until acknowledged


def _priority_prefix(r: Reminder) -> str:
    if r.is_urgent and r.is_important:
        return "🔴 ТЕРМІНОВО! "
    elif r.is_urgent:
        return "🟠 Терміново: "
    elif r.is_important:
        return "🔵 Важливо: "
    return ""


def _ack_kb(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Прочитано", callback_data=f"ack_{reminder_id}")],
    ])


async def fire_reminder(reminder_id: int) -> None:
    assert _bot and _session_factory
    async with _session_factory() as session:
        reminder = await session.get(Reminder, reminder_id)
        if not reminder or not reminder.is_active:
            return
        user = await session.get(User, reminder.user_id)
        if not user:
            return
        pri = _priority_prefix(reminder)
        try:
            await _bot.send_message(
                user.telegram_id,
                f"🔔 {pri}{reminder.text}",
                reply_markup=_ack_kb(reminder.id),
            )
        except Exception:
            logger.exception("Failed to send reminder %d", reminder_id)
            return

        # Schedule repeat every 2 min until acknowledged
        nag_job_id = f"nag_{reminder_id}"
        if not scheduler.get_job(nag_job_id):
            scheduler.add_job(
                _nag_reminder,
                trigger=IntervalTrigger(seconds=ACK_INTERVAL_SEC),
                args=[reminder_id],
                id=nag_job_id,
                replace_existing=True,
            )

        if not reminder.cron_expr:
            reminder.is_active = False
            await session.commit()


async def _nag_reminder(reminder_id: int) -> None:
    """Re-send reminder until user acknowledges."""
    assert _bot and _session_factory
    async with _session_factory() as session:
        reminder = await session.get(Reminder, reminder_id)
        if not reminder:
            _stop_nag(reminder_id)
            return
        user = await session.get(User, reminder.user_id)
        if not user:
            _stop_nag(reminder_id)
            return
        pri = _priority_prefix(reminder)
        try:
            await _bot.send_message(
                user.telegram_id,
                f"🔔🔔 {pri}{reminder.text}\n\n<i>Натисни «Прочитано» щоб зупинити</i>",
                reply_markup=_ack_kb(reminder.id),
            )
        except Exception:
            logger.exception("Failed to nag reminder %d", reminder_id)


def acknowledge_reminder(reminder_id: int) -> None:
    """Stop nagging for this reminder."""
    _stop_nag(reminder_id)


def _stop_nag(reminder_id: int) -> None:
    nag_job_id = f"nag_{reminder_id}"
    try:
        scheduler.remove_job(nag_job_id)
    except Exception:
        pass


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
    _stop_nag(reminder_id)


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
