from collections import OrderedDict, deque
from collections.abc import Callable
from time import monotonic


class RateLimiter:
    """Sliding window per user across all chats; timestamps only, no text.

    Atomic on one asyncio event loop: allow() has no await. Inactive entries
    expire and active-user storage is bounded without evicting valid limits.
    """

    def __init__(
        self,
        requests: int,
        period_seconds: float,
        *,
        clock: Callable[[], float] = monotonic,
        max_users: int = 10000,
    ) -> None:
        if requests <= 0 or period_seconds <= 0 or max_users <= 0:
            raise ValueError("Rate limiter settings must be positive")
        self.requests = requests
        self.period_seconds = period_seconds
        self.clock = clock
        self.max_users = max_users
        self._users: OrderedDict[int, deque[float]] = OrderedDict()

    def allow(self, user_id: int) -> bool:
        now = self.clock()
        cutoff = now - self.period_seconds
        while self._users:
            first = next(iter(self._users))
            if self._users[first][-1] > cutoff:
                break
            self._users.pop(first)
        window = self._users.get(user_id)
        if window is None:
            if len(self._users) >= self.max_users:
                return False
            window = self._users[user_id] = deque()
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= self.requests:
            return False
        window.append(now)
        self._users.move_to_end(user_id)
        return True
