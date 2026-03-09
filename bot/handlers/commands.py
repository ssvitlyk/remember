import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import dateparser
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from sqlalchemy import select

from bot.calendar_kb import build_calendar, build_time_picker, build_weekday_picker
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

# --- Main menu ---

MAIN_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="➕ Нове нагадування", callback_data="new_reminder")],
    [InlineKeyboardButton(text="📋 Мої нагадування", callback_data="my_list")],
    [InlineKeyboardButton(text="🕐 Змінити часовий пояс", callback_data="change_tz")],
])


def _remind_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏰ Одноразове", callback_data="type_once")],
        [InlineKeyboardButton(text="🔁 Щоденне", callback_data="type_daily")],
        [InlineKeyboardButton(text="📅 Щотижневе", callback_data="type_weekly")],
        [InlineKeyboardButton(text="⚙️ Cron-вираз", callback_data="type_cron")],
        [InlineKeyboardButton(text="« Назад", callback_data="main_menu")],
    ])


# --- FSM States ---

class SetTZ(StatesGroup):
    waiting = State()


class NewReminder(StatesGroup):
    waiting_text = State()
    waiting_date = State()     # calendar date pick (once)
    waiting_time = State()     # time pick (once, daily)
    waiting_weekday = State()  # weekday pick (weekly)
    waiting_weekly_time = State()  # time after weekday (weekly)
    waiting_cron = State()


# --- Helpers ---

async def _get_user(telegram_id: int) -> User | None:
    async with get_session() as session:
        return (
            await session.execute(select(User).where(User.telegram_id == telegram_id))
        ).scalar_one_or_none()


async def _send_main_menu(message: Message, text: str = "Що хочеш зробити?") -> None:
    await message.answer(text, reply_markup=MAIN_MENU)


async def _ensure_user(callback: CallbackQuery) -> User | None:
    user = await _get_user(callback.from_user.id)
    if not user:
        await callback.message.edit_text(
            "Спочатку встанови часовий пояс:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🕐 Обрати часовий пояс", callback_data="change_tz")],
            ]),
        )
        return None
    return user


# --- /start & timezone ---

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await _get_user(message.from_user.id)
    if user:
        await _send_main_menu(message, f"Привіт! Часовий пояс: <b>{user.timezone}</b>")
    else:
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=tz)] for tz in TIMEZONES],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await message.answer("Привіт! Обери свій часовий пояс:", reply_markup=kb)
        await state.set_state(SetTZ.waiting)


@router.callback_query(F.data == "change_tz")
async def cb_change_tz(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=tz)] for tz in TIMEZONES],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await callback.message.answer("Обери часовий пояс:", reply_markup=kb)
    await state.set_state(SetTZ.waiting)


@router.message(SetTZ.waiting)
async def set_timezone(message: Message, state: FSMContext) -> None:
    tz_name = message.text.strip() if message.text else ""
    try:
        ZoneInfo(tz_name)
    except (KeyError, ValueError):
        await message.answer("Невідомий часовий пояс. Спробуй ще раз (напр. Europe/Kyiv).")
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
        f"Часовий пояс: <b>{tz_name}</b>",
        reply_markup=ReplyKeyboardRemove(),
    )
    await _send_main_menu(message)


# --- Main menu callback ---

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await callback.message.edit_text("Що хочеш зробити?", reply_markup=MAIN_MENU)


# --- New reminder flow ---

@router.callback_query(F.data == "new_reminder")
async def cb_new_reminder(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _ensure_user(callback)
    if not user:
        return
    await callback.answer()
    await callback.message.edit_text("Обери тип нагадування:", reply_markup=_remind_type_kb())


@router.callback_query(F.data.startswith("type_"))
async def cb_remind_type(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _ensure_user(callback)
    if not user:
        return
    await callback.answer()
    rtype = callback.data.replace("type_", "")
    await state.update_data(remind_type=rtype)

    if rtype == "cron":
        await callback.message.edit_text(
            "Введи текст нагадування та cron-вираз через новий рядок:\n\n"
            "<code>Стендап\n0 9 * * 1-5</code>",
        )
        await state.set_state(NewReminder.waiting_cron)
    else:
        await callback.message.edit_text("Введи текст нагадування:")
        await state.set_state(NewReminder.waiting_text)


@router.message(NewReminder.waiting_text)
async def on_reminder_text(message: Message, state: FSMContext) -> None:
    text = message.text.strip() if message.text else ""
    if not text:
        await message.answer("Текст не може бути порожнім. Спробуй ще:")
        return

    data = await state.get_data()
    rtype = data.get("remind_type", "once")
    await state.update_data(remind_text=text)

    if rtype == "once":
        today = date.today()
        await message.answer(
            "📅 Обери дату:",
            reply_markup=build_calendar(today.year, today.month),
        )
        await state.set_state(NewReminder.waiting_date)
    elif rtype == "daily":
        await message.answer(
            "🕐 О котрій годині щодня?",
            reply_markup=build_time_picker(),
        )
        await state.set_state(NewReminder.waiting_time)
    elif rtype == "weekly":
        await message.answer(
            "📅 В який день тижня?",
            reply_markup=build_weekday_picker(),
        )
        await state.set_state(NewReminder.waiting_weekday)


# --- Calendar navigation ---

@router.callback_query(F.data.startswith("cal_prev_"))
async def cb_cal_prev(callback: CallbackQuery) -> None:
    await callback.answer()
    _, _, _, year, month = callback.data.split("_")
    year, month = int(year), int(month)
    month -= 1
    if month < 1:
        month = 12
        year -= 1
    await callback.message.edit_reply_markup(reply_markup=build_calendar(year, month))


@router.callback_query(F.data.startswith("cal_next_"))
async def cb_cal_next(callback: CallbackQuery) -> None:
    await callback.answer()
    _, _, _, year, month = callback.data.split("_")
    year, month = int(year), int(month)
    month += 1
    if month > 12:
        month = 1
        year += 1
    await callback.message.edit_reply_markup(reply_markup=build_calendar(year, month))


@router.callback_query(F.data == "cal_ignore")
async def cb_cal_ignore(callback: CallbackQuery) -> None:
    await callback.answer()


# --- Calendar date picked ---

@router.callback_query(F.data.startswith("cal_day_"))
async def cb_cal_day(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    parts = callback.data.split("_")  # cal_day_YYYY_MM_DD
    year, month, day = int(parts[2]), int(parts[3]), int(parts[4])
    await state.update_data(picked_date=f"{year}-{month:02d}-{day:02d}")

    await callback.message.edit_text(
        f"📅 Дата: <b>{day:02d}.{month:02d}.{year}</b>\n\n"
        "🕐 Обери час або введи свій (формат <code>HH:MM</code>):",
        reply_markup=build_time_picker(),
    )
    await state.set_state(NewReminder.waiting_time)


# --- Time picked (button) ---

@router.callback_query(F.data.startswith("time_"), NewReminder.waiting_time)
async def cb_time_pick(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    time_str = callback.data.replace("time_", "")
    await _process_time_input(callback.message, state, callback.from_user.id, time_str, edit=True)


# --- Time entered (text) ---

@router.message(NewReminder.waiting_time)
async def on_reminder_time(message: Message, state: FSMContext) -> None:
    raw = message.text.strip() if message.text else ""
    await _process_time_input(message, state, message.from_user.id, raw, edit=False)


async def _process_time_input(
    msg: Message, state: FSMContext, telegram_id: int, raw: str, *, edit: bool
) -> None:
    user = await _get_user(telegram_id)
    if not user:
        await state.clear()
        await msg.answer("Помилка. Натисни /start")
        return

    data = await state.get_data()
    rtype = data.get("remind_type", "once")
    text = data.get("remind_text", "Нагадування")

    cron_expr: str | None = None
    fire_at: datetime | None = None

    if rtype == "once":
        picked_date = data.get("picked_date")
        if picked_date:
            # Calendar flow: date from calendar + time from input
            try:
                t = datetime.strptime(raw, "%H:%M")
            except ValueError:
                reply = "Невірний формат. Введи час як <code>HH:MM</code> або обери кнопкою:"
                if edit:
                    await msg.edit_text(reply, reply_markup=build_time_picker())
                else:
                    await msg.answer(reply, reply_markup=build_time_picker())
                return
            tz = ZoneInfo(user.timezone)
            local_dt = datetime.strptime(f"{picked_date} {raw}", "%Y-%m-%d %H:%M")
            local_dt = local_dt.replace(tzinfo=tz)
            fire_at = local_dt.astimezone(timezone.utc)
        else:
            # Fallback: dateparser
            parsed = dateparser.parse(
                raw,
                settings={
                    "PREFER_DATES_FROM": "future",
                    "TIMEZONE": user.timezone,
                    "RETURN_AS_TIMEZONE_AWARE": True,
                },
            )
            if not parsed:
                await msg.answer("Не вдалося розпізнати час. Спробуй інший формат:")
                return
            fire_at = parsed.astimezone(timezone.utc)

    elif rtype == "daily":
        try:
            t = datetime.strptime(raw, "%H:%M")
            cron_expr = f"{t.minute} {t.hour} * * *"
        except ValueError:
            reply = "Невірний формат. Обери час кнопкою або введи <code>HH:MM</code>:"
            if edit:
                await msg.edit_text(reply, reply_markup=build_time_picker())
            else:
                await msg.answer(reply, reply_markup=build_time_picker())
            return

    await _save_reminder(msg, state, user, text, fire_at, cron_expr, edit=edit)


# --- Weekday picked ---

@router.callback_query(F.data.startswith("wday_"), NewReminder.waiting_weekday)
async def cb_weekday_pick(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    dow = int(callback.data.replace("wday_", ""))
    await state.update_data(picked_weekday=dow)
    await callback.message.edit_text(
        "🕐 О котрій годині?",
        reply_markup=build_time_picker(),
    )
    await state.set_state(NewReminder.waiting_weekly_time)


@router.callback_query(F.data.startswith("time_"), NewReminder.waiting_weekly_time)
async def cb_weekly_time_pick(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    time_str = callback.data.replace("time_", "")
    await _process_weekly_time(callback.message, state, callback.from_user.id, time_str, edit=True)


@router.message(NewReminder.waiting_weekly_time)
async def on_weekly_time(message: Message, state: FSMContext) -> None:
    raw = message.text.strip() if message.text else ""
    await _process_weekly_time(message, state, message.from_user.id, raw, edit=False)


async def _process_weekly_time(
    msg: Message, state: FSMContext, telegram_id: int, raw: str, *, edit: bool
) -> None:
    user = await _get_user(telegram_id)
    if not user:
        await state.clear()
        await msg.answer("Помилка. Натисни /start")
        return

    data = await state.get_data()
    text = data.get("remind_text", "Нагадування")
    dow = data.get("picked_weekday", 0)

    try:
        t = datetime.strptime(raw, "%H:%M")
    except ValueError:
        reply = "Невірний формат. Обери час кнопкою або введи <code>HH:MM</code>:"
        if edit:
            await msg.edit_text(reply, reply_markup=build_time_picker())
        else:
            await msg.answer(reply, reply_markup=build_time_picker())
        return

    cron_expr = f"{t.minute} {t.hour} * * {dow}"
    await _save_reminder(msg, state, user, text, None, cron_expr, edit=edit)


# --- Cron input ---

@router.message(NewReminder.waiting_cron)
async def on_reminder_cron(message: Message, state: FSMContext) -> None:
    user = await _get_user(message.from_user.id)
    if not user:
        await state.clear()
        await message.answer("Помилка. Натисни /start")
        return

    raw = message.text.strip() if message.text else ""
    lines = raw.split("\n", 1)
    if len(lines) < 2:
        await message.answer(
            "Введи текст і cron через новий рядок:\n<code>Текст\n0 9 * * 1-5</code>"
        )
        return

    text = lines[0].strip()
    cron_expr = lines[1].strip()
    if not text:
        text = "Нагадування"

    await _save_reminder(message, state, user, text, None, cron_expr)


# --- Save reminder ---

async def _save_reminder(
    msg: Message,
    state: FSMContext,
    user: User,
    text: str,
    fire_at: datetime | None,
    cron_expr: str | None,
    *,
    edit: bool = False,
) -> None:
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

    await state.clear()
    tz = ZoneInfo(user.timezone)

    if fire_at:
        local_time = fire_at.astimezone(tz).strftime("%d.%m.%Y %H:%M")
        confirm = f"✅ Нагадування #{rid} — <b>{local_time}</b>\n{text}"
    else:
        confirm = f"✅ Нагадування #{rid} — <code>{cron_expr}</code>\n{text}"

    if edit:
        await msg.edit_text(confirm, reply_markup=MAIN_MENU)
    else:
        await msg.answer(confirm, reply_markup=MAIN_MENU)


# --- List reminders ---

@router.callback_query(F.data == "my_list")
async def cb_list(callback: CallbackQuery) -> None:
    user = await _ensure_user(callback)
    if not user:
        return
    await callback.answer()

    async with get_session() as session:
        result = await session.execute(
            select(Reminder).where(
                Reminder.user_id == user.id, Reminder.is_active == True  # noqa: E712
            )
        )
        reminders = list(result.scalars())

    if not reminders:
        await callback.message.edit_text(
            "Немає активних нагадувань.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Меню", callback_data="main_menu")],
            ]),
        )
        return

    tz = ZoneInfo(user.timezone)
    lines = []
    buttons = []
    for r in reminders:
        if r.cron_expr:
            lines.append(f"#{r.id} 🔁 {r.text} — <code>{r.cron_expr}</code>")
        elif r.fire_at:
            local = r.fire_at.astimezone(tz).strftime("%d.%m.%Y %H:%M")
            lines.append(f"#{r.id} ⏰ {r.text} — {local}")
        buttons.append([InlineKeyboardButton(
            text=f"🗑 Видалити #{r.id}",
            callback_data=f"del_{r.id}",
        )])

    buttons.append([InlineKeyboardButton(text="« Меню", callback_data="main_menu")])

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


# --- Delete reminder ---

@router.callback_query(F.data.startswith("del_"))
async def cb_delete(callback: CallbackQuery) -> None:
    user = await _ensure_user(callback)
    if not user:
        return

    try:
        rid = int(callback.data.replace("del_", ""))
    except (ValueError, TypeError):
        await callback.answer("Помилка")
        return

    async with get_session() as session:
        reminder = await session.get(Reminder, rid)
        if not reminder or reminder.user_id != user.id:
            await callback.answer("Не знайдено")
            return
        reminder.is_active = False

    cancel_reminder(rid)
    await callback.answer(f"Видалено #{rid}")
    await cb_list(callback)


# --- Keep command support ---

@router.message(Command("remind"))
async def cmd_remind(message: Message, state: FSMContext) -> None:
    user = await _get_user(message.from_user.id)
    if not user:
        await message.answer("Спочатку натисни /start")
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

    if "cron:" in raw:
        parts = raw.split("cron:", 1)
        text = parts[0].strip()
        cron_expr = parts[1].strip()
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
    elif " weekly " in f" {raw.lower()} " or "щотижня" in raw.lower():
        for kw in ("weekly", "щотижня"):
            idx = raw.lower().find(kw)
            if idx >= 0:
                text = raw[:idx].strip()
                break
        now_local = datetime.now(ZoneInfo(user.timezone))
        cron_expr = f"0 9 * * {now_local.weekday()}"
    else:
        parsed = dateparser.parse(
            raw,
            settings={
                "PREFER_DATES_FROM": "future",
                "TIMEZONE": user.timezone,
                "RETURN_AS_TIMEZONE_AWARE": True,
            },
        )
        if parsed:
            text = raw
            fire_at = parsed.astimezone(timezone.utc)
        else:
            await message.answer("Не вдалося розпізнати час.")
            return

    if not text:
        text = "Нагадування"

    await _save_reminder(message, state, user, text, fire_at, cron_expr)


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    user = await _get_user(message.from_user.id)
    if not user:
        await message.answer("Спочатку натисни /start")
        return

    async with get_session() as session:
        result = await session.execute(
            select(Reminder).where(
                Reminder.user_id == user.id, Reminder.is_active == True  # noqa: E712
            )
        )
        reminders = list(result.scalars())

    if not reminders:
        await message.answer("Немає активних нагадувань.", reply_markup=MAIN_MENU)
        return

    tz = ZoneInfo(user.timezone)
    lines = []
    buttons = []
    for r in reminders:
        if r.cron_expr:
            lines.append(f"#{r.id} 🔁 {r.text} — <code>{r.cron_expr}</code>")
        elif r.fire_at:
            local = r.fire_at.astimezone(tz).strftime("%d.%m.%Y %H:%M")
            lines.append(f"#{r.id} ⏰ {r.text} — {local}")
        buttons.append([InlineKeyboardButton(
            text=f"🗑 Видалити #{r.id}",
            callback_data=f"del_{r.id}",
        )])
    buttons.append([InlineKeyboardButton(text="« Меню", callback_data="main_menu")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.message(Command("delete"))
async def cmd_delete(message: Message) -> None:
    user = await _get_user(message.from_user.id)
    if not user:
        await message.answer("Спочатку натисни /start")
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
            await message.answer("Не знайдено.")
            return
        reminder.is_active = False

    cancel_reminder(rid)
    await message.answer(f"🗑 Видалено #{rid}", reply_markup=MAIN_MENU)
