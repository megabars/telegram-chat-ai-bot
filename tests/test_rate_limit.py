from app.utils.rate_limit import RateLimiter


def test_sliding_window_and_users():
    now = [0.0]
    limiter = RateLimiter(2, 60, clock=lambda: now[0])
    assert limiter.allow(1)
    now[0] = 10
    assert limiter.allow(1)
    assert not limiter.allow(1)
    assert limiter.allow(2)
    now[0] = 60
    assert limiter.allow(1)
    assert not limiter.allow(1)
    now[0] = 70
    assert limiter.allow(1)


def test_inactive_users_expire_and_capacity_fails_closed():
    now = [0.0]
    limiter = RateLimiter(1, 60, clock=lambda: now[0], max_users=2)
    assert limiter.allow(1)
    assert limiter.allow(2)
    assert not limiter.allow(3)
    assert not limiter.allow(1)
    now[0] = 60
    assert limiter.allow(3)
    assert len(limiter._users) == 1
