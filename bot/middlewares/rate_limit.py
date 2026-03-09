import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self, limit: int = 20, window: float = 60.0) -> None:
        self._limit = limit
        self._window = window
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        uid = event.from_user.id if event.from_user else 0
        now = time.monotonic()
        q = self._hits[uid]
        while q and now - q[0] > self._window:
            q.popleft()
        if len(q) >= self._limit:
            return  # silently drop
        q.append(now)
        return await handler(event, data)
