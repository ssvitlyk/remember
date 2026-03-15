import logging
from datetime import datetime, timedelta, timezone

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

# Track reminders pending acknowledgement: {telegram_id: {reminder_id, ...}}
_pending_ack: dict[int, set[int]] = {}


def _priority_prefix(r: Reminder) -> str:
    if r.is_urgent and r.is_important:
        return "🔴 ТЕРМІНОВО! "
    elif r.is_urgent:
        return "🟠 Терміново: "
    elif r.is_important:
        return "🔵 Важливо: "
    return ""


def _format_notification(r: Reminder, nag: bool = False) -> str:
    """Build a visually distinct notification based on priority."""
    bell = "🔔🔔" if nag else "🔔"
    nag_hint = "\n\n<i>Натисни «Прочитано» щоб зупинити</i>" if nag else ""

    if r.is_urgent and r.is_important:
        return (
            f"🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥\n"
            f"{bell} <b>ТЕРМІНОВО!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{r.text}\n"
            f"🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥"
            f"{nag_hint}"
        )
    elif r.is_urgent:
        return (
            f"🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧\n"
            f"{bell} <b>Терміново</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{r.text}\n"
            f"🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧"
            f"{nag_hint}"
        )
    elif r.is_important:
        return (
            f"🟦🟦🟦🟦🟦🟦🟦🟦🟦🟦\n"
            f"{bell} <b>Важливо</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{r.text}\n"
            f"🟦🟦🟦🟦🟦🟦🟦🟦🟦🟦"
            f"{nag_hint}"
        )
    else:
        return (
            f"⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜\n"
            f"{bell} Нагадування\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{r.text}\n"
            f"⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜"
            f"{nag_hint}"
        )


def _ack_kb(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Прочитано", callback_data=f"ack_{reminder_id}"),
            InlineKeyboardButton(text="⏰ +1 год", callback_data=f"snooze_{reminder_id}"),
        ],
    ])


def _grouped_nag_kb(reminders: list[Reminder]) -> InlineKeyboardMarkup:
    """Build keyboard with per-reminder ack/snooze + ack-all button."""
    rows = []
    for r in reminders:
        label = r.text[:20] + "…" if len(r.text) > 20 else r.text
        rows.append([
            InlineKeyboardButton(text=f"✅ {label}", callback_data=f"ack_{r.id}"),
            InlineKeyboardButton(text="⏰", callback_data=f"snooze_{r.id}"),
        ])
    if len(reminders) > 1:
        rows.append([
            InlineKeyboardButton(text="✅✅ Всі прочитані", callback_data="ack_all"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def snooze_reminder(reminder_id: int) -> None:
    """Stop nagging and reschedule reminder +1 hour from now."""
    _remove_pending(reminder_id)
    fire_at = datetime.now(timezone.utc) + timedelta(hours=1)
    scheduler.add_job(
        fire_reminder,
        trigger=DateTrigger(run_date=fire_at),
        args=[reminder_id],
        id=f"reminder_{reminder_id}",
        replace_existing=True,
    )


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
            await _bot.send_message(
                user.telegram_id,
                _format_notification(reminder),
                reply_markup=_ack_kb(reminder.id),
            )
        except Exception:
            logger.exception("Failed to send reminder %d", reminder_id)
            return

        # Add to pending ack and start sweep for this user
        _pending_ack.setdefault(user.telegram_id, set()).add(reminder_id)
        sweep_id = f"nag_sweep_{user.telegram_id}"
        if not scheduler.get_job(sweep_id):
            scheduler.add_job(
                _nag_sweep,
                trigger=IntervalTrigger(seconds=ACK_INTERVAL_SEC),
                args=[user.telegram_id],
                id=sweep_id,
                replace_existing=True,
            )

        if not reminder.cron_expr:
            reminder.is_active = False
            await session.commit()


async def _nag_sweep(telegram_id: int) -> None:
    """Send grouped nag for all pending reminders of a user."""
    assert _bot and _session_factory
    rids = _pending_ack.get(telegram_id)
    if not rids:
        _stop_sweep(telegram_id)
        return

    async with _session_factory() as session:
        reminders = []
        for rid in list(rids):
            r = await session.get(Reminder, rid)
            if r:
                reminders.append(r)
            else:
                rids.discard(rid)

        if not reminders:
            _stop_sweep(telegram_id)
            return

        # Single reminder — individual format
        if len(reminders) == 1:
            r = reminders[0]
            try:
                await _bot.send_message(
                    telegram_id,
                    _format_notification(r, nag=True),
                    reply_markup=_ack_kb(r.id),
                )
            except Exception:
                logger.exception("Failed to nag reminder %d", r.id)
            return

        # Multiple reminders — grouped message
        reminders.sort(key=lambda r: (
            not (r.is_urgent and r.is_important),
            not r.is_urgent,
            not r.is_important,
        ))

        lines = [f"🔔🔔 <b>Непрочитані нагадування ({len(reminders)}):</b>\n"]
        for r in reminders:
            pri = _priority_prefix(r)
            lines.append(f"  {pri}{r.text}")
        lines.append("\n<i>Натисни «Прочитано» щоб зупинити</i>")

        try:
            await _bot.send_message(
                telegram_id,
                "\n".join(lines),
                reply_markup=_grouped_nag_kb(reminders),
            )
        except Exception:
            logger.exception("Failed to send grouped nag to %d", telegram_id)


def acknowledge_reminder(reminder_id: int) -> None:
    """Stop nagging for this reminder."""
    _remove_pending(reminder_id)


def acknowledge_all(telegram_id: int) -> list[int]:
    """Acknowledge all pending reminders for a user. Returns list of acked IDs."""
    rids = list(_pending_ack.pop(telegram_id, set()))
    _stop_sweep(telegram_id)
    return rids


def _remove_pending(reminder_id: int) -> None:
    """Remove a single reminder from pending ack tracking."""
    for tg_id, rids in list(_pending_ack.items()):
        rids.discard(reminder_id)
        if not rids:
            del _pending_ack[tg_id]
            _stop_sweep(tg_id)


def _stop_sweep(telegram_id: int) -> None:
    try:
        scheduler.remove_job(f"nag_sweep_{telegram_id}")
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
    _remove_pending(reminder_id)


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
