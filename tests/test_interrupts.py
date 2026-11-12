"""Unit tests for cross-platform Ctrl+C fast termination & bounded watchdog guarantees."""

from __future__ import annotations

import concurrent.futures
import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.common.interrupts import (
    InterruptController,
    SHUTDOWN_EVENT,
    interruptible_input,
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


def test_run_interruptible_subprocess_drains_large_utf8_output_without_deadlock() -> None:
    """Concurrent pipe draining handles >64KB UTF-8 stdout/stderr without deadlock or cp1252 failure."""
    script = (
        "import os, sys\n"
        "chunk = ('\\u2714 \\U0001f680 ' + 'x' * 70 + '\\n').encode('utf-8')\n"
        "for _ in range(1500):\n"
        "    sys.stdout.buffer.write(chunk)\n"
        "    sys.stderr.buffer.write(chunk)\n"
        "sys.stdout.buffer.write((os.environ.get('REPO360_TEST_MARKER', '') + '\\n').encode('utf-8'))\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stderr.buffer.flush()\n"
    )
    env = dict(os.environ)
    env["REPO360_TEST_MARKER"] = "env-propagated-ok"
    proc = run_interruptible_subprocess(
        [sys.executable, "-c", script],
        timeout=15.0,
        env=env,
    )
    assert proc.returncode == 0
    assert "\u2714 \U0001f680" in proc.stdout
    assert "\u2714 \U0001f680" in proc.stderr
    assert "env-propagated-ok" in proc.stdout
    assert proc.stdout.count("\u2714") == 1500
    assert proc.stderr.count("\u2714") == 1500


def test_interruptible_input_returns_value_and_aborts_cleanly_on_shutdown() -> None:
    """interruptible_input converts Ctrl+C/EOFError during shutdown to KeyboardInterrupt without blocking."""
    with patch("builtins.input", return_value="https://ghe.example.com"):
        assert interruptible_input("Domain: ") == "https://ghe.example.com"

    # Simulated blocking console read aborted by SHUTDOWN_EVENT must raise KeyboardInterrupt quickly.
    release_read = threading.Event()

    def _blocked_input(_prompt: str = "") -> str:
        release_read.wait(timeout=5.0)
        raise EOFError

    timer = threading.Timer(0.05, SHUTDOWN_EVENT.set)
    timer.start()
    start = time.monotonic()
    try:
        with patch("builtins.input", side_effect=_blocked_input):
            with pytest.raises(KeyboardInterrupt):
                interruptible_input("Domain: ", eof_default="", poll_interval=0.01)
    finally:
        timer.cancel()
        release_read.set()
    assert (time.monotonic() - start) < 0.5

    # Genuine EOF without shutdown returns eof_default when configured, or raises EOFError otherwise.
    reset_shutdown()
    with patch("builtins.input", side_effect=EOFError):
        assert interruptible_input("Domain: ", eof_default="") == ""
    with patch("builtins.input", side_effect=EOFError):
        with pytest.raises(EOFError):
            interruptible_input("Domain: ")


def test_interrupt_controller_is_silent_and_suppresses_duplicate_teardown_signal(capsys) -> None:
    """Ctrl+C handling must produce no extra terminal noise and suppress stale queued SIGINT."""
    exit_calls = []
    ctrl = InterruptController(grace_seconds=1.0, exit_fn=lambda code: exit_calls.append(code))
    try:
        with patch("src.common.interrupts._thread.interrupt_main"):
            ctrl.trigger(signum=2, from_os_thread=True)

        # Manual shutdown conversion (e.g. interruptible_input) makes pending OS SIGINT a silent no-op.
        ctrl.note_manual_interrupt()
        ctrl.handle_python_signal(2, None)
        assert exit_calls == []

        # A genuine second Ctrl+C (>= 0.15s later) still triggers immediate force-exit silently.
        time.sleep(0.18)
        with pytest.raises(KeyboardInterrupt):
            ctrl.handle_python_signal(2, None)
        assert exit_calls == [130]

        # Watchdog timeout exit must also emit zero terminal text.
        ctrl.reset()
        exit_calls.clear()
        ctrl.grace_seconds = 0.08
        with patch("src.common.interrupts._thread.interrupt_main"):
            ctrl.trigger(signum=2, from_os_thread=False)
        time.sleep(0.2)
        assert exit_calls == [130]

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
    finally:
        ctrl.reset()


def test_silent_interrupt_excepthook_suppresses_keyboard_interrupt_traceback(capsys) -> None:
    """Uncaught KeyboardInterrupt exits via silent excepthook without traceback noise."""
    from src.common import interrupts as int_mod

    orig_calls = []
    with patch.object(int_mod, "_ORIG_EXCEPTHOOK", side_effect=lambda *args: orig_calls.append(args[0])):
        int_mod._silent_interrupt_excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
        assert orig_calls == []
        int_mod._silent_interrupt_excepthook(ValueError, ValueError("boom"), None)
        assert orig_calls == [ValueError]

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""

