"""Inline keyboard calendar widget for Telegram."""

import calendar
from datetime import date, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

DAYS_UA = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
MONTHS_UA = [
    "", "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
    "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень",
]

IGNORE = "cal_ignore"
PREFIX = "cal"


def build_calendar(year: int, month: int) -> InlineKeyboardMarkup:
    """Build a calendar inline keyboard for the given month."""
    rows: list[list[InlineKeyboardButton]] = []

    # Header: < Month Year >
    rows.append([
        InlineKeyboardButton(text="«", callback_data=f"{PREFIX}_prev_{year}_{month}"),
        InlineKeyboardButton(text=f"{MONTHS_UA[month]} {year}", callback_data=IGNORE),
        InlineKeyboardButton(text="»", callback_data=f"{PREFIX}_next_{year}_{month}"),
    ])

    # Day-of-week headers
    rows.append([
        InlineKeyboardButton(text=d, callback_data=IGNORE) for d in DAYS_UA
    ])

    # Date grid
    today = date.today()
    cal = calendar.monthcalendar(year, month)
    for week in cal:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data=IGNORE))
            else:
                d = date(year, month, day)
                if d < today:
                    # Past dates — show but disabled
                    row.append(InlineKeyboardButton(text="·", callback_data=IGNORE))
                elif d == today:
                    row.append(InlineKeyboardButton(
                        text=f"[{day}]",
                        callback_data=f"{PREFIX}_day_{year}_{month}_{day}",
                    ))
                else:
                    row.append(InlineKeyboardButton(
                        text=str(day),
                        callback_data=f"{PREFIX}_day_{year}_{month}_{day}",
                    ))
        rows.append(row)

    # Cancel
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="main_menu")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_time_picker() -> InlineKeyboardMarkup:
    """Quick time selection + custom input hint."""
    presets = [
        ("08:00", "08:00"), ("09:00", "09:00"), ("10:00", "10:00"),
        ("12:00", "12:00"), ("14:00", "14:00"), ("15:00", "15:00"),
        ("17:00", "17:00"), ("18:00", "18:00"), ("20:00", "20:00"),
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(presets), 3):
        rows.append([
            InlineKeyboardButton(text=p[0], callback_data=f"time_{p[1]}")
            for p in presets[i:i + 3]
        ])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_weekday_picker() -> InlineKeyboardMarkup:
    """Pick a day of week for weekly reminders."""
    days = [
        ("Понеділок", 0), ("Вівторок", 1), ("Середа", 2),
        ("Четвер", 3), ("П'ятниця", 4), ("Субота", 5), ("Неділя", 6),
    ]
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"wday_{num}")]
        for name, num in days
    ]
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
