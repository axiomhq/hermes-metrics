# SPDX-License-Identifier: Apache-2.0 OR MIT
"""The hand-off between hook callbacks and background work."""

from __future__ import annotations

import threading
import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hermess_metrics.dispatch import _POLL_SECONDS, Dispatcher

FLUSH_TIMEOUT = 5.0


def test_items_reach_the_handler_in_order() -> None:
    seen: list[int] = []
    dispatcher: Dispatcher[int] = Dispatcher(capacity=16)
    dispatcher.start(seen.append)
    try:
        for value in range(10):
            assert dispatcher.submit(value)
        assert dispatcher.flush(FLUSH_TIMEOUT)
    finally:
        dispatcher.stop(FLUSH_TIMEOUT)
    assert seen == list(range(10))


def test_submitting_to_a_full_queue_returns_without_blocking() -> None:
    release = threading.Event()
    dispatcher: Dispatcher[int] = Dispatcher(capacity=2)
    dispatcher.start(lambda _: release.wait(FLUSH_TIMEOUT))
    try:
        while dispatcher.submit(0):
            pass
        assert dispatcher.submit(1) is False
        assert dispatcher.stats().dropped >= 1
    finally:
        release.set()
        dispatcher.stop(FLUSH_TIMEOUT)


def test_a_raising_handler_does_not_stop_the_worker() -> None:
    seen: list[int] = []

    def handler(item: int) -> None:
        if item == 0:
            raise RuntimeError("handler blew up")
        seen.append(item)

    dispatcher: Dispatcher[int] = Dispatcher(capacity=16)
    dispatcher.start(handler)
    try:
        dispatcher.submit(0)
        dispatcher.submit(1)
        dispatcher.submit(2)
        assert dispatcher.flush(FLUSH_TIMEOUT)
    finally:
        dispatcher.stop(FLUSH_TIMEOUT)
    assert seen == [1, 2]
    assert dispatcher.stats().failed == 1
    assert dispatcher.stats().handled == 2


def test_submitting_before_start_is_refused_rather_than_raised() -> None:
    dispatcher: Dispatcher[int] = Dispatcher(capacity=4)
    assert dispatcher.submit(1) is False
    assert dispatcher.stats().dropped == 1


def test_submitting_after_stop_is_refused_rather_than_raised() -> None:
    dispatcher: Dispatcher[int] = Dispatcher(capacity=4)
    dispatcher.start(lambda _: None)
    dispatcher.stop(FLUSH_TIMEOUT)
    assert dispatcher.submit(1) is False


def test_stop_is_idempotent() -> None:
    dispatcher: Dispatcher[int] = Dispatcher(capacity=4)
    dispatcher.start(lambda _: None)
    dispatcher.stop(FLUSH_TIMEOUT)
    dispatcher.stop(FLUSH_TIMEOUT)


def test_flush_reports_failure_when_the_worker_cannot_drain() -> None:
    release = threading.Event()
    dispatcher: Dispatcher[int] = Dispatcher(capacity=8)
    dispatcher.start(lambda _: release.wait(FLUSH_TIMEOUT))
    try:
        dispatcher.submit(1)
        dispatcher.submit(2)
        assert dispatcher.flush(0.05) is False
    finally:
        release.set()
        dispatcher.stop(FLUSH_TIMEOUT)


def test_the_worker_thread_never_holds_the_interpreter_open() -> None:
    dispatcher: Dispatcher[int] = Dispatcher(capacity=4)
    dispatcher.start(lambda _: None)
    try:
        assert dispatcher.worker_is_daemon
    finally:
        dispatcher.stop(FLUSH_TIMEOUT)


# Every submission is accounted for exactly once.
@given(st.lists(st.integers(), min_size=1, max_size=200), st.integers(min_value=1, max_value=8))
def test_submissions_are_fully_accounted(values: list[int], capacity: int) -> None:
    dispatcher: Dispatcher[int] = Dispatcher(capacity=capacity)
    dispatcher.start(lambda _: None)
    try:
        for value in values:
            dispatcher.submit(value)
        dispatcher.flush(FLUSH_TIMEOUT)
    finally:
        dispatcher.stop(FLUSH_TIMEOUT)
    stats = dispatcher.stats()
    assert stats.accepted + stats.dropped == len(values)
    assert stats.handled + stats.failed == stats.accepted


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Dispatcher(capacity=0)


def test_starting_twice_keeps_the_first_worker() -> None:
    seen: list[int] = []
    dispatcher: Dispatcher[int] = Dispatcher(capacity=4)
    dispatcher.start(seen.append)
    first = dispatcher._worker
    dispatcher.start(seen.append)
    try:
        assert dispatcher._worker is first
        assert dispatcher.submit(7)
        assert dispatcher.flush(FLUSH_TIMEOUT)
    finally:
        dispatcher.stop(FLUSH_TIMEOUT)
    assert seen == [7]


def test_flush_before_start_has_nothing_to_wait_for() -> None:
    dispatcher: Dispatcher[int] = Dispatcher(capacity=4)
    assert dispatcher.flush(FLUSH_TIMEOUT) is True


def test_the_worker_survives_an_idle_period() -> None:
    seen: list[int] = []
    dispatcher: Dispatcher[int] = Dispatcher(capacity=4)
    dispatcher.start(seen.append)
    try:
        time.sleep(_POLL_SECONDS * 3)
        assert dispatcher.submit(1)
        assert dispatcher.flush(FLUSH_TIMEOUT)
    finally:
        dispatcher.stop(FLUSH_TIMEOUT)
    assert seen == [1]
