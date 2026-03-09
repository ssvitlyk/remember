import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand, MenuButtonCommands
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from bot.config import settings
from bot.db.engine import async_session, engine
from bot.db.models import Base
from bot.handlers.commands import router as commands_router
from bot.middlewares.rate_limit import RateLimitMiddleware
from bot.scheduler import scheduler, start_scheduler

logger = logging.getLogger(__name__)


async def on_startup(bot: Bot) -> None:
    # Create tables (dev convenience; use alembic in prod)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await bot.set_my_commands([
        BotCommand(command="start", description="Головне меню"),
        BotCommand(command="remind", description="Створити нагадування"),
        BotCommand(command="list", description="Мої нагадування"),
    ])
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    await start_scheduler(bot, async_session)

    if settings.WEBHOOK_HOST:
        await bot.set_webhook(
            f"{settings.WEBHOOK_HOST}{settings.WEBHOOK_PATH}",
            secret_token=settings.WEBHOOK_SECRET,
        )
        logger.info("Webhook set: %s%s", settings.WEBHOOK_HOST, settings.WEBHOOK_PATH)


async def on_shutdown(bot: Bot) -> None:
    scheduler.shutdown(wait=True)
    if settings.WEBHOOK_HOST:
        await bot.delete_webhook()
    await engine.dispose()
    logger.info("Shutdown complete")


def _build_dp() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(commands_router)
    dp.message.middleware(RateLimitMiddleware(settings.RATE_LIMIT))
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    return dp


async def _run_polling() -> None:
    """Run in long-polling mode (dev, no WEBHOOK_HOST set)."""
    bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = _build_dp()
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Starting in polling mode")
    await dp.start_polling(bot)


def _run_webhook() -> None:
    """Run as aiohttp webhook server."""
    bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = _build_dp()

    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=settings.WEBHOOK_SECRET).register(
        app, path=settings.WEBHOOK_PATH
    )
    setup_application(app, dp, bot=bot)

    logger.info("Starting webhook server on %s:%d", settings.LISTEN_HOST, settings.LISTEN_PORT)
    web.run_app(app, host=settings.LISTEN_HOST, port=settings.LISTEN_PORT)


def main() -> None:
    logging.basicConfig(
        level=settings.LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if settings.WEBHOOK_HOST:
        _run_webhook()
    else:
        asyncio.run(_run_polling())


if __name__ == "__main__":
    main()
