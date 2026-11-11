"""Unit tests for cross-platform Ctrl+C fast termination & bounded watchdog guarantees."""

from __future__ import annotations

import concurrent.futures
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.common.interrupts import (
    InterruptController,
    SHUTDOWN_EVENT,
    interruptible_sleep,
    iter_completed,
    register_closable,
    reset_shutdown,
    run_interruptible_subprocess,
    wait_future,
)


@pytest.fixture(autouse=True)
def _clean_interrupt_state():
    reset_shutdown()
    yield
    reset_shutdown()


def test_interruptible_sleep_wakes_immediately() -> None:
    """Long sleeps (e.g. 30s rate-limit backoffs) must abort instantaneously when shutdown fires."""
    starter = threading.Timer(0.05, SHUTDOWN_EVENT.set)
    starter.start()
    start = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            interruptible_sleep(30.0)
    finally:
        starter.cancel()
    assert (time.monotonic() - start) < 0.5


def test_iter_completed_and_wait_future_abort_quickly() -> None:
    """Pending worker futures must stop blocking the main thread immediately upon Ctrl+C."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    stop_worker = threading.Event()
    try:
        fut = pool.submit(lambda: stop_worker.wait(timeout=10.0))
        timer = threading.Timer(0.08, SHUTDOWN_EVENT.set)
        timer.start()
        start = time.monotonic()
        try:
            with pytest.raises(KeyboardInterrupt):
                for _ in iter_completed([fut], poll_interval=0.05):
                    pass
        finally:
            timer.cancel()
        assert (time.monotonic() - start) < 0.6

        reset_shutdown()
        fut2 = pool.submit(lambda: stop_worker.wait(timeout=10.0))
        timer2 = threading.Timer(0.08, SHUTDOWN_EVENT.set)
        timer2.start()
        start2 = time.monotonic()
        try:
            with pytest.raises(KeyboardInterrupt):
                wait_future(fut2, poll_interval=0.05)
        finally:
            timer2.cancel()
        assert (time.monotonic() - start2) < 0.6
    finally:
        stop_worker.set()
        pool.shutdown(wait=False, cancel_futures=True)


def test_run_interruptible_subprocess_terminates_child_promptly() -> None:
    """Active child subprocesses (git/pipx) must terminate well below 3s on Ctrl+C."""
    timer = threading.Timer(0.15, SHUTDOWN_EVENT.set)
    timer.start()
    start = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            run_interruptible_subprocess(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=30,
                poll_interval=0.05,
            )
    finally:
        timer.cancel()
    assert (time.monotonic() - start) < 1.5


def test_interrupt_controller_watchdog_and_double_ctrl_c() -> None:
    """Verify 2s-style bounded watchdog exit and immediate exit on genuine second Ctrl+C."""
    exit_calls = []
    ctrl = InterruptController(grace_seconds=0.2, exit_fn=lambda code: exit_calls.append(code))
    closable_session = MagicMock(spec=["close"])
    closable_server = MagicMock(spec=["server_close"])
    register_closable(closable_session)
    register_closable(closable_server)

    # First Ctrl+C sets shutdown, closes tracked resources, and triggers watchdog within ~0.2s
    ctrl.trigger(signum=2, from_os_thread=False)
    assert SHUTDOWN_EVENT.is_set()
    time.sleep(0.08)
    assert closable_session.close.called
    assert closable_server.server_close.called

    # Duplicate trigger within debounce (<150ms) does not double-exit
    ctrl.trigger(signum=2, from_os_thread=False)
    assert exit_calls == []

    # Watchdog expires cleanly after 0.2s
    time.sleep(0.3)
    assert exit_calls == [130]

    # Genuine second Ctrl+C (>=150ms after first) force-exits synchronously without waiting
    ctrl.reset()
    exit_calls.clear()
    ctrl.trigger(signum=2, from_os_thread=False)
    time.sleep(0.18)
    ctrl.trigger(signum=2, from_os_thread=False)
    assert exit_calls == [130]


def test_handle_python_signal_consumes_expected_os_interrupt() -> None:
    """_thread.interrupt_main() signal delivery (>150ms later) must not trigger a false double-Ctrl+C exit."""
    exit_calls = []
    ctrl = InterruptController(grace_seconds=1.0, exit_fn=lambda code: exit_calls.append(code))
    try:
        with patch("src.common.interrupts._thread.interrupt_main") as mock_interrupt_main:
            ctrl.trigger(signum=2, from_os_thread=True)
            mock_interrupt_main.assert_called_once()

        # Simulate CPython delivering the queued SIGINT past the 0.15s debounce window.
        time.sleep(0.18)
        with pytest.raises(KeyboardInterrupt):
            ctrl.handle_python_signal(2, None)
        assert exit_calls == []

        # A genuine second Ctrl+C signal after consuming the expected interrupt force-exits.
        with pytest.raises(KeyboardInterrupt):
            ctrl.handle_python_signal(2, None)
        assert exit_calls == [130]
    finally:
        ctrl.reset()
