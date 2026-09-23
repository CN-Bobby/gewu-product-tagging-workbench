from __future__ import annotations

import pytest

from backend.keyword_research.config import EngineConfig
from backend.keyword_research import rate_limiter as rate_limiter_module
from backend.keyword_research.rate_limiter import RateLimiter


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _use_fake_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    clock = _FakeClock()
    monkeypatch.setattr(rate_limiter_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(rate_limiter_module.time, "sleep", clock.sleep)
    return clock


def test_engine_and_rate_limiter_defaults() -> None:
    config = EngineConfig()
    limiter = RateLimiter()

    assert (config.window_max_calls, config.window_seconds,
            config.min_call_interval) == (60, 60.0, 1.0)
    assert (limiter.max_calls, limiter.window_seconds,
            limiter.min_interval) == (60, 60.0, 1.0)


def test_default_limiter_enforces_one_second_between_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _use_fake_clock(monkeypatch)
    limiter = RateLimiter()
    limiter.record_call()

    assert limiter.next_available_in() == pytest.approx(1.0)
    assert limiter.wait_until_allowed() == pytest.approx(1.0)
    assert clock.now == pytest.approx(101.0)
    limiter.record_call()
    assert limiter.next_available_in() == pytest.approx(1.0)


def test_limiter_enforces_rolling_window_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_fake_clock(monkeypatch)
    limiter = RateLimiter(max_calls=2, window_seconds=10.0, min_interval=0.5)
    limiter.record_call()
    limiter.wait_until_allowed()
    limiter.record_call()

    # The interval has elapsed, but the first call still occupies the window.
    assert limiter.calls_in_window == 2
    assert limiter.next_available_in() == pytest.approx(9.5)
    assert limiter.wait_until_allowed() == pytest.approx(9.5)
    limiter.record_call()
    assert limiter.calls_in_window == 2
