"""Centralized cross-platform Ctrl+C (SIGINT/SIGTERM) responsiveness & shutdown guard.

Provides:
1. Cooperative cancellation via SHUTDOWN_EVENT so retry loops, sleeps, HTTP pools,
   and Future wait loops exit immediately on interrupt.
2. Native Windows SetConsoleCtrlHandler + Python signal handlers so Ctrl+C registers
   instantly even when the main thread is blocked in C/socket calls.
3. Bounded hard-exit watchdog (default 2.0s) ensuring CLI termination within 3-4s even
   if third-party C extensions or worker threads remain blocked.
"""

from __future__ import annotations

import concurrent.futures
import os
import signal
import subprocess
import sys
import threading
import time
import weakref
from typing import Any, Callable, Iterable, Iterator, List, Optional, Sequence, Tuple, TypeVar

try:
    import _thread
except ImportError:  # pragma: no cover
    _thread = None  # type: ignore[assignment]

F = TypeVar("F", bound=concurrent.futures.Future)

DEFAULT_INTERRUPT_GRACE_SECONDS = 2.0
_DOUBLE_SIGNAL_DEBOUNCE_SECONDS = 0.15

SHUTDOWN_EVENT = threading.Event()
_REGISTRY_LOCK = threading.Lock()
_ACTIVE_PROCESSES: "weakref.WeakSet[subprocess.Popen[Any]]" = weakref.WeakSet()
_ACTIVE_CLOSABLES: "weakref.WeakSet[Any]" = weakref.WeakSet()
_ORIG_SUBPROCESS_RUN = subprocess.run
_ORIG_TIME_SLEEP = time.sleep
_ORIG_EXCEPTHOOK = sys.excepthook


def is_shutting_down() -> bool:
    """Return True if an OS termination or Ctrl+C interrupt has been requested."""
    return SHUTDOWN_EVENT.is_set()


def register_active_process(proc: subprocess.Popen[Any]) -> None:
    """Track an active subprocess so Ctrl+C can terminate it immediately."""
    with _REGISTRY_LOCK:
        _ACTIVE_PROCESSES.add(proc)


def unregister_active_process(proc: subprocess.Popen[Any]) -> None:
    """Stop tracking a finished subprocess."""
    with _REGISTRY_LOCK:
        _ACTIVE_PROCESSES.discard(proc)


def register_closable(resource: Any) -> None:
    """Track a network session, server, or socket with .close()/.server_close() for shutdown cleanup."""
    with _REGISTRY_LOCK:
        _ACTIVE_CLOSABLES.add(resource)


def unregister_closable(resource: Any) -> None:
    """Stop tracking a resource after normal completion."""
    with _REGISTRY_LOCK:
        _ACTIVE_CLOSABLES.discard(resource)


def terminate_active_processes() -> None:
    """Best-effort immediate termination of all tracked child processes."""
    with _REGISTRY_LOCK:
        procs = list(_ACTIVE_PROCESSES)
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.wait(timeout=0.3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def close_active_closables() -> None:
    """Best-effort closure of active HTTP sessions/sockets/servers to unblock socket waits."""
    with _REGISTRY_LOCK:
        resources = list(_ACTIVE_CLOSABLES)
    for res in resources:
        try:
            if hasattr(res, "server_close") and callable(res.server_close):
                res.server_close()
            elif hasattr(res, "close") and callable(res.close):
                res.close()
        except Exception:
            pass


def interruptible_sleep(seconds: float, sleep_fn: Optional[Callable[[float], Any]] = None) -> bool:
    """Sleep up to ``seconds`` while waking instantaneously on Ctrl+C.

    Respects patched ``time.sleep`` in tests cleanly and raises ``KeyboardInterrupt``
    if ``SHUTDOWN_EVENT`` is set before or during the wait.
    """
    if SHUTDOWN_EVENT.is_set():
        note_manual_interrupt()
        raise KeyboardInterrupt
    if seconds <= 0:
        return False

    if sleep_fn is not None and sleep_fn is not _ORIG_TIME_SLEEP:
        sleep_fn(seconds)
        if SHUTDOWN_EVENT.is_set():
            note_manual_interrupt()
            raise KeyboardInterrupt
        return False

    if time.sleep is not _ORIG_TIME_SLEEP:
        time.sleep(seconds)
        if SHUTDOWN_EVENT.is_set():
            note_manual_interrupt()
            raise KeyboardInterrupt
        return False

    if SHUTDOWN_EVENT.wait(timeout=float(seconds)):
        note_manual_interrupt()
        raise KeyboardInterrupt
    return False


def wait_future(future: concurrent.futures.Future[Any], poll_interval: float = 0.2) -> Any:
    """Await a single Future while polling SHUTDOWN_EVENT for prompt Ctrl+C cancellation."""
    while not future.done():
        if SHUTDOWN_EVENT.is_set():
            future.cancel()
            note_manual_interrupt()
            raise KeyboardInterrupt
        concurrent.futures.wait((future,), timeout=poll_interval)
    if SHUTDOWN_EVENT.is_set():
        note_manual_interrupt()
        raise KeyboardInterrupt
    return future.result()


def iter_completed(
    futures: Iterable[concurrent.futures.Future[Any]],
    poll_interval: float = 0.2,
) -> Iterator[concurrent.futures.Future[Any]]:
    """Yield completed futures cleanly without blocking indefinitely in C Condition.wait on Windows."""
    pending = set(futures)
    while pending:
        if SHUTDOWN_EVENT.is_set():
            for fut in pending:
                fut.cancel()
            note_manual_interrupt()
            raise KeyboardInterrupt
        done, pending = concurrent.futures.wait(
            pending,
            timeout=poll_interval,
            return_when=concurrent.futures.FIRST_COMPLETED,
        )
        for fut in done:
            if SHUTDOWN_EVENT.is_set():
                for rem in pending:
                    rem.cancel()
                note_manual_interrupt()
                raise KeyboardInterrupt
            yield fut


def interruptible_input(
    prompt: str = "",
    *,
    eof_default: Optional[str] = None,
    poll_interval: float = 0.02,
) -> str:
    """Read a line from stdin via ``input()`` while aborting immediately on Ctrl+C.

    On Windows, native console reads (``ReadConsoleW``) can block or convert Ctrl+C
    into ``EOFError`` ahead of Python's signal delivery. Running ``input()`` on a
    daemon reader thread ensures Ctrl+C always wakes immediately as
    ``KeyboardInterrupt`` rather than leaking ``EOFError``.
    """
    if SHUTDOWN_EVENT.is_set():
        note_manual_interrupt()
        raise KeyboardInterrupt

    result: List[str] = []
    exc_holder: List[BaseException] = []
    done_event = threading.Event()

    def _read() -> None:
        try:
            result.append(input(prompt))
        except EOFError as err:
            if SHUTDOWN_EVENT.is_set():
                return
            if eof_default is not None:
                result.append(eof_default)
            else:
                exc_holder.append(err)
        except BaseException as err:  # pragma: no cover
            if not SHUTDOWN_EVENT.is_set():
                exc_holder.append(err)
        finally:
            done_event.set()

    reader = threading.Thread(target=_read, name="repo360-stdin-reader", daemon=True)
    reader.start()

    while not done_event.is_set():
        if SHUTDOWN_EVENT.is_set():
            note_manual_interrupt()
            raise KeyboardInterrupt
        done_event.wait(timeout=poll_interval)

    if SHUTDOWN_EVENT.is_set():
        note_manual_interrupt()
        raise KeyboardInterrupt
    if exc_holder:
        raise exc_holder[0]
    return result[0] if result else ""


def run_interruptible_subprocess(
    cmd: Sequence[str],
    *,
    timeout: Optional[float] = None,
    capture_output: bool = True,
    text: bool = True,
    check: bool = False,
    poll_interval: float = 0.2,
    env: Optional[dict[str, str]] = None,
    cwd: Optional[str] = None,
) -> subprocess.CompletedProcess[Any]:
    """Execute a subprocess cleanly while draining pipes and aborting promptly on Ctrl+C."""
    if SHUTDOWN_EVENT.is_set():
        note_manual_interrupt()
        raise KeyboardInterrupt

    # Keep subprocess.run patchable by tests
    if subprocess.run is not _ORIG_SUBPROCESS_RUN:
        kwargs: dict[str, Any] = {
            "capture_output": capture_output,
            "text": text,
            "check": check,
            "timeout": timeout,
        }
        if env is not None:
            kwargs["env"] = env
        if cwd is not None:
            kwargs["cwd"] = cwd
        return subprocess.run(cmd, **kwargs)

    stdout_dest = subprocess.PIPE if capture_output else None
    stderr_dest = subprocess.PIPE if capture_output else None
    popen_kwargs: dict[str, Any] = {
        "stdout": stdout_dest,
        "stderr": stderr_dest,
        "text": text,
    }
    if text:
        # Decode child output as UTF-8 so Windows cp1252 defaults do not crash
        popen_kwargs["encoding"] = "utf-8"
        popen_kwargs["errors"] = "replace"
    if env is not None:
        popen_kwargs["env"] = env
    if cwd is not None:
        popen_kwargs["cwd"] = cwd

    proc = subprocess.Popen(cmd, **popen_kwargs)
    register_active_process(proc)
    deadline = (time.monotonic() + timeout) if timeout is not None else None

    out_chunks: List[Any] = []
    err_chunks: List[Any] = []
    empty_sentinel: Any = "" if text else b""

    def _drain_stream(stream: Any, sink: List[Any]) -> None:
        if stream is None:
            return
        try:
            for chunk in iter(lambda: stream.read(8192), empty_sentinel):
                sink.append(chunk)
        except Exception:
            pass

    out_reader: Optional[threading.Thread] = None
    err_reader: Optional[threading.Thread] = None
    if capture_output:
        if proc.stdout is not None:
            out_reader = threading.Thread(target=_drain_stream, args=(proc.stdout, out_chunks), daemon=True)
            out_reader.start()
        if proc.stderr is not None:
            err_reader = threading.Thread(target=_drain_stream, args=(proc.stderr, err_chunks), daemon=True)
            err_reader.start()

    def _collect_captured() -> Tuple[Optional[Any], Optional[Any]]:
        if not capture_output:
            return None, None
        for reader in (out_reader, err_reader):
            if reader is not None:
                reader.join(timeout=1.5)
        joined_out = empty_sentinel.join(out_chunks)
        joined_err = empty_sentinel.join(err_chunks)
        return joined_out, joined_err

    try:
        while proc.poll() is None:
            if SHUTDOWN_EVENT.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=0.4)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=0.5)
                for stream in (proc.stdout, proc.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
                note_manual_interrupt()
                raise KeyboardInterrupt

            wait_slice = poll_interval
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    proc.kill()
                    proc.wait(timeout=1.0)
                    for stream in (proc.stdout, proc.stderr):
                        if stream is not None:
                            try:
                                stream.close()
                            except Exception:
                                pass
                    stdout_val, stderr_val = _collect_captured()
                    raise subprocess.TimeoutExpired(cmd, timeout or 0, output=stdout_val, stderr=stderr_val)
                wait_slice = min(poll_interval, remaining)

            try:
                proc.wait(timeout=wait_slice)
            except subprocess.TimeoutExpired:
                continue

        stdout_val, stderr_val = _collect_captured()
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, cmd, output=stdout_val, stderr=stderr_val
            )
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout_val, stderr_val)
    finally:
        unregister_active_process(proc)


class InterruptController:
    """Manages signal handling, cleanup dispatch, and hard-exit watchdog deadlines."""

    def __init__(
        self,
        grace_seconds: float = DEFAULT_INTERRUPT_GRACE_SECONDS,
        exit_fn: Callable[[int], Any] = os._exit,
    ) -> None:
        self.grace_seconds = max(0.05, float(grace_seconds))
        self.exit_fn = exit_fn
        self._state_lock = threading.Lock()
        self._triggered = False
        self._expect_python_signal = False
        self._manual_interrupt_raised = False
        self._finalizing = False
        self._first_trigger_ts = 0.0
        self._watchdog_timer: Optional[threading.Timer] = None
        self._installed = False
        self._win_handler_ref: Any = None

    def _on_watchdog_timeout(self, exit_code: int) -> None:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        self.exit_fn(exit_code)

    @staticmethod
    def _cleanup_async() -> None:
        terminate_active_processes()
        close_active_closables()

    def note_manual_interrupt(self) -> None:
        """Record that a helper already raised KeyboardInterrupt via SHUTDOWN_EVENT."""
        with self._state_lock:
            self._manual_interrupt_raised = True

    def trigger(self, signum: int = 2, from_os_thread: bool = False) -> None:
        """Initiate immediate silent shutdown and start the bounded exit watchdog."""
        exit_code = 130 if signum in (getattr(signal, "SIGINT", 2), 0, 1) else 128 + signum
        now = time.monotonic()

        with self._state_lock:
            already_triggered = self._triggered
            elapsed_since_first = now - self._first_trigger_ts if already_triggered else 0.0
            if not already_triggered:
                self._triggered = True
                self._first_trigger_ts = now
                self._expect_python_signal = bool(from_os_thread)

        if already_triggered:
            # Ignore duplicate OS/Python signal for one keystroke; a second Ctrl+C exits immediately
            if elapsed_since_first >= _DOUBLE_SIGNAL_DEBOUNCE_SECONDS:
                self.exit_fn(exit_code)
            return

        SHUTDOWN_EVENT.set()
        timer = threading.Timer(self.grace_seconds, self._on_watchdog_timeout, args=(exit_code,))
        timer.daemon = True
        with self._state_lock:
            self._watchdog_timer = timer
        timer.start()

        cleaner = threading.Thread(target=self._cleanup_async, name="repo360-interrupt-cleanup", daemon=True)
        cleaner.start()

        if from_os_thread and _thread is not None:
            try:
                _thread.interrupt_main()
            except Exception:
                pass

    def handle_python_signal(self, signum: int, _frame: Any) -> None:
        """Python signal handler callback raising KeyboardInterrupt after starting watchdog."""
        with self._state_lock:
            if self._finalizing:
                self._expect_python_signal = False
                self._manual_interrupt_raised = False
                return
            if self._expect_python_signal:
                self._expect_python_signal = False
                if self._manual_interrupt_raised:
                    self._manual_interrupt_raised = False
                    return
                raise KeyboardInterrupt
        self.trigger(signum=signum, from_os_thread=False)
        raise KeyboardInterrupt

    def _install_windows_console_handler(self) -> None:
        """Register native SetConsoleCtrlHandler so Ctrl+C bypasses C/GIL main-thread waits."""
        if sys.platform != "win32" or self._win_handler_ref is not None:
            return
        try:
            import ctypes

            # Win32 PHANDLER_ROUTINE returns BOOL (32-bit c_int), not 8-bit c_bool.
            handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

            def _ctrl_handler(ctrl_type: int) -> int:
                try:
                    # CTRL_C_EVENT=0, CTRL_BREAK_EVENT=1, CTRL_CLOSE_EVENT=2
                    if ctrl_type in (0, 1, 2):
                        self.trigger(signum=int(ctrl_type), from_os_thread=True)
                        return 1
                except BaseException:
                    return 1
                return 0

            ref = handler_type(_ctrl_handler)
            if ctypes.windll.kernel32.SetConsoleCtrlHandler(ref, True):
                self._win_handler_ref = ref
        except Exception:
            pass

    def install(self, force_in_tests: bool = False) -> None:
        """Install Python signal handlers, silent excepthook, and native Windows console handler once."""
        if os.environ.get("PYTEST_CURRENT_TEST") and not force_in_tests:
            return
        if self._installed:
            return
        self._installed = True

        sys.excepthook = _silent_interrupt_excepthook

        try:
            signal.signal(signal.SIGINT, self.handle_python_signal)
            for sig_name in ("SIGTERM", "SIGBREAK"):
                sig = getattr(signal, sig_name, None)
                if sig is not None:
                    signal.signal(sig, self.handle_python_signal)
        except (ValueError, OSError):
            pass

        self._install_windows_console_handler()

    def finalize_exit(self, code: int = 130) -> int:
        """Complete interrupt teardown, bypassing atexit ThreadPoolExecutor joins on real Ctrl+C."""
        with self._state_lock:
            self._finalizing = True
            self._expect_python_signal = False
            self._manual_interrupt_raised = False
            timer = self._watchdog_timer
            self._watchdog_timer = None
            triggered = self._triggered
        if timer is not None:
            timer.cancel()

        if os.environ.get("PYTEST_CURRENT_TEST") or (not triggered and not SHUTDOWN_EVENT.is_set()):
            return code

        terminate_active_processes()
        close_active_closables()
        try:
            if sys.stdout and not sys.stdout.closed:
                sys.stdout.flush()
            if sys.stderr and not sys.stderr.closed:
                sys.stderr.flush()
        except Exception:
            pass
        self.exit_fn(code)

    def reset(self) -> None:
        """Reset state cleanly (intended for unit tests)."""
        with self._state_lock:
            if self._watchdog_timer is not None:
                self._watchdog_timer.cancel()
                self._watchdog_timer = None
            self._triggered = False
            self._expect_python_signal = False
            self._manual_interrupt_raised = False
            self._finalizing = False
            self._first_trigger_ts = 0.0
        if sys.excepthook is _silent_interrupt_excepthook:
            sys.excepthook = _ORIG_EXCEPTHOOK
        SHUTDOWN_EVENT.clear()


_DEFAULT_CONTROLLER = InterruptController()


def _silent_interrupt_excepthook(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_tb: Any,
) -> None:
    """Exit silently with code 130 when KeyboardInterrupt escapes without printing a traceback."""
    if isinstance(exc_type, type) and issubclass(exc_type, KeyboardInterrupt):
        _DEFAULT_CONTROLLER.finalize_exit(130)
        if not os.environ.get("PYTEST_CURRENT_TEST"):
            _DEFAULT_CONTROLLER.exit_fn(130)
        return
    _ORIG_EXCEPTHOOK(exc_type, exc_value, exc_tb)


def note_manual_interrupt() -> None:
    """Record that Shutdown detection already converted an interrupt into KeyboardInterrupt."""
    _DEFAULT_CONTROLLER.note_manual_interrupt()


def install_signal_handlers(grace_seconds: float = DEFAULT_INTERRUPT_GRACE_SECONDS) -> None:
    """Install Repo360 fast-interrupt handlers with bounded exit timeout."""
    _DEFAULT_CONTROLLER.grace_seconds = max(0.05, float(grace_seconds))
    _DEFAULT_CONTROLLER.install()


def finalize_interrupt_exit(code: int = 130) -> int:
    """Return exit code cleanly or terminate immediately without joining stuck worker threads."""
    return _DEFAULT_CONTROLLER.finalize_exit(code)


def reset_shutdown() -> None:
    """Reset shutdown flags and registries (for tests)."""
    _DEFAULT_CONTROLLER.reset()
    with _REGISTRY_LOCK:
        _ACTIVE_PROCESSES.clear()
        _ACTIVE_CLOSABLES.clear()
