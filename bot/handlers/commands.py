import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import dateparser
from aiogram import Bot, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from sqlalchemy import select

from bot.db.engine import get_session
from bot.db.models import Reminder, User
from bot.scheduler import cancel_reminder, schedule_reminder

logger = logging.getLogger(__name__)
router = Router()

TIMEZONES = [
    "Europe/Kyiv",
    "Europe/London",
    "Europe/Berlin",
    "US/Eastern",
    "US/Pacific",
    "Asia/Tokyo",
]


class SetTZ(StatesGroup):
    waiting = State()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    async with get_session() as session:
        user = (
            await session.execute(
                select(User).where(User.telegram_id == message.from_user.id)
            )
        ).scalar_one_or_none()

    if user:
        await message.answer(
            f"Твій часовий пояс: <b>{user.timezone}</b>\n"
            "Щоб змінити — /start",
            reply_markup=ReplyKeyboardRemove(),
        )

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=tz)] for tz in TIMEZONES],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer("Обери свій часовий пояс:", reply_markup=kb)
    await state.set_state(SetTZ.waiting)


@router.message(SetTZ.waiting)
async def set_timezone(message: Message, state: FSMContext) -> None:
    tz_name = message.text.strip() if message.text else ""
    try:
        ZoneInfo(tz_name)
    except (KeyError, ValueError):
        await message.answer("Невідомий часовий пояс. Спробуй ще раз або введи вручну (напр. Europe/Kyiv).")
        return

    async with get_session() as session:
        user = (
            await session.execute(
                select(User).where(User.telegram_id == message.from_user.id)
            )
        ).scalar_one_or_none()
        if user:
            user.timezone = tz_name
        else:
            session.add(User(telegram_id=message.from_user.id, timezone=tz_name))

    await state.clear()
    await message.answer(
        f"Часовий пояс встановлено: <b>{tz_name}</b>",
        reply_markup=ReplyKeyboardRemove(),
    )


async def _get_user(telegram_id: int) -> User | None:
    async with get_session() as session:
        return (
            await session.execute(select(User).where(User.telegram_id == telegram_id))
        ).scalar_one_or_none()


@router.message(Command("remind"))
async def cmd_remind(message: Message) -> None:
    user = await _get_user(message.from_user.id)
    if not user:
        await message.answer("Спочатку встанови часовий пояс: /start")
        return

    raw = message.text.replace("/remind", "", 1).strip() if message.text else ""
    if not raw:
        await message.answer(
            "Використання:\n"
            "<code>/remind Купити молоко завтра о 9:00</code>\n"
            "<code>/remind Стендап daily 09:00</code>\n"
            "<code>/remind Звіт cron:0 9 * * 1-5</code>"
        )
        return

    cron_expr: str | None = None
    fire_at: datetime | None = None
    text = raw

    # Check for explicit cron
    if "cron:" in raw:
        parts = raw.split("cron:", 1)
        text = parts[0].strip()
        cron_expr = parts[1].strip()
    # Check for "daily HH:MM"
    elif " daily " in f" {raw} ":
        idx = raw.lower().find("daily")
        text = raw[:idx].strip()
        time_part = raw[idx + 5:].strip()
        hour, minute = 9, 0
        if time_part:
            try:
                t = datetime.strptime(time_part, "%H:%M")
                hour, minute = t.hour, t.minute
            except ValueError:
                pass
        cron_expr = f"{minute} {hour} * * *"
    # Check for "weekly" / "щотижня"
    elif " weekly " in f" {raw.lower()} " or "щотижня" in raw.lower():
        for kw in ("weekly", "щотижня"):
            idx = raw.lower().find(kw)
            if idx >= 0:
                text = raw[:idx].strip()
                break
        now_local = datetime.now(ZoneInfo(user.timezone))
        cron_expr = f"0 9 * * {now_local.weekday()}"
    else:
        # One-shot: parse with dateparser
        parsed = dateparser.parse(
            raw,
            settings={
                "PREFER_DATES_FROM": "future",
                "TIMEZONE": user.timezone,
                "RETURN_AS_TIMEZONE_AWARE": True,
            },
        )
        if parsed:
            # Extract text: everything before the date-like part
            # Simple heuristic: use the full raw as text if we can't split well
            text = raw
            fire_at = parsed.astimezone(timezone.utc)
        else:
            await message.answer("Не вдалося розпізнати час. Спробуй інший формат.")
            return

    if not text:
        text = "Нагадування"

    async with get_session() as session:
        reminder = Reminder(
            user_id=user.id,
            text=text,
            fire_at=fire_at,
            cron_expr=cron_expr,
        )
        session.add(reminder)
        await session.flush()
        schedule_reminder(reminder)
        rid = reminder.id

    tz = ZoneInfo(user.timezone)
    if fire_at:
        local_time = fire_at.astimezone(tz).strftime("%d.%m.%Y %H:%M")
        await message.answer(f"✅ Нагадування #{rid} створено на <b>{local_time}</b>")
    else:
        await message.answer(f"✅ Повторюване нагадування #{rid} створено: <code>{cron_expr}</code>")


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    user = await _get_user(message.from_user.id)
    if not user:
        await message.answer("Спочатку встанови часовий пояс: /start")
        return

    async with get_session() as session:
        result = await session.execute(
            select(Reminder).where(
                Reminder.user_id == user.id, Reminder.is_active == True  # noqa: E712
            )
        )
        reminders = list(result.scalars())

    if not reminders:
        await message.answer("Немає активних нагадувань.")
        return

    tz = ZoneInfo(user.timezone)
    lines = []
    for r in reminders:
        if r.cron_expr:
            lines.append(f"#{r.id} 🔁 {r.text} — <code>{r.cron_expr}</code>")
        elif r.fire_at:
            local = r.fire_at.astimezone(tz).strftime("%d.%m.%Y %H:%M")
            lines.append(f"#{r.id} ⏰ {r.text} — {local}")
    await message.answer("\n".join(lines))


@router.message(Command("delete"))
async def cmd_delete(message: Message) -> None:
    user = await _get_user(message.from_user.id)
    if not user:
        await message.answer("Спочатку встанови часовий пояс: /start")
        return

    raw = message.text.replace("/delete", "", 1).strip() if message.text else ""
    try:
        rid = int(raw)
    except (ValueError, TypeError):
        await message.answer("Використання: <code>/delete 123</code>")
        return

    async with get_session() as session:
        reminder = await session.get(Reminder, rid)
        if not reminder or reminder.user_id != user.id:
            await message.answer("Нагадування не знайдено.")
            return
        reminder.is_active = False

    cancel_reminder(rid)
    await message.answer(f"🗑 Нагадування #{rid} видалено.")
