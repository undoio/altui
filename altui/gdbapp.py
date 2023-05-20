from __future__ import annotations

import contextlib
import functools
import os
import threading
import traceback
from typing import Any, Callable, Concatenate, Iterator, ParamSpec, TypeVar

import gdb  # type: ignore
from textual.app import App
from typing_extensions import Self

from . import gdbsupport, ioutil

_T = TypeVar("_T")
_P = ParamSpec("_P")
_GdbCompatibleAppT = TypeVar("_GdbCompatibleAppT", bound="GdbCompatibleApp")

_app_instance: GdbCompatibleApp | None = None


@functools.lru_cache(52)
def _make_ctrl_from_char(char: str) -> str:
    char = char.upper()
    assert len(char) == 1 and ord("A") <= ord(char) <= ord("Z"), f"Invalid char: {char!r}"
    return chr(ord(char) - ord("A") + 1)


def log_exceptions(
    func: Callable[Concatenate[_GdbCompatibleAppT, _P], _T | None]
) -> Callable[Concatenate[_GdbCompatibleAppT, _P], _T | None]:
    # Avoid wrapping twice as that seems to break functions called by textual with:
    #     missing 1 required positional argument: 'self'
    if getattr(func, "_exceptions_handled_by_wrapper", False):
        return func

    @functools.wraps(func)
    def wrapper(self: _GdbCompatibleAppT, *args: _P.args, **kwargs: _P.kwargs) -> _T | None:
        try:
            return func(self, *args, **kwargs)
        except Exception:  # pylint: disable=broad-exception-caught
            # When productising, consider printing the full stack trace only in tests.
            tb = traceback.format_exc().rstrip("\n")
            self.on_gdb_thread(print, tb)
            return None

    # pylint: disable=protected-access
    wrapper._exceptions_handled_by_wrapper = True  # type: ignore[attr-defined]
    return wrapper


def fatal_exceptions(
    func: Callable[Concatenate[_GdbCompatibleAppT, _P], _T]
) -> Callable[Concatenate[_GdbCompatibleAppT, _P], _T]:
    # Avoid wrapping twice as that seems to break functions called by textual with:
    #     missing 1 required positional argument: 'self'
    if getattr(func, "_exceptions_handled_by_wrapper", False):
        return func

    @functools.wraps(func)
    def wrapper(self: _GdbCompatibleAppT, *args: _P.args, **kwargs: _P.kwargs) -> _T:
        try:
            return func(self, *args, **kwargs)
        except Exception:
            self.configuration.handle_fatal_error(msg="unhandled exception")
            raise AssertionError("Impossible control flow")  # For pylint's sake.

    # pylint: disable=protected-access
    wrapper._exceptions_handled_by_wrapper = True  # type: ignore[attr-defined]
    return wrapper


def ui_thread_only_without_handling_exceptions(
    func: Callable[Concatenate[_GdbCompatibleAppT, _P], _T]
) -> Callable[Concatenate[_GdbCompatibleAppT, _P], _T]:
    @functools.wraps(func)
    def wrapper(self: _GdbCompatibleAppT, *args: _P.args, **kwargs: _P.kwargs) -> _T:
        self._assert_in_ui_thread(func.__name__)  # pylint: disable=protected-access
        return func(self, *args, **kwargs)

    return wrapper


def ui_thread_only(
    func: Callable[Concatenate[_GdbCompatibleAppT, _P], _T]
) -> Callable[Concatenate[_GdbCompatibleAppT, _P], _T | None]:
    return ui_thread_only_without_handling_exceptions(log_exceptions(func))


def gdb_thread_only(
    func: Callable[Concatenate[_GdbCompatibleAppT, _P], _T]
) -> Callable[Concatenate[_GdbCompatibleAppT, _P], _T]:
    @functools.wraps(func)
    def wrapper(self: _GdbCompatibleAppT, *args: _P.args, **kwargs: _P.kwargs) -> _T:
        if threading.current_thread() is not threading.main_thread():
            self.configuration.handle_fatal_error(
                msg=f"{func.__name__} can only be executed on the main thread"
            )
        return func(self, *args, **kwargs)

    return wrapper


# Not an ABC as that doesn't work with App.
class GdbCompatibleApp(App):
    _life_cycle_mutex = threading.Lock()

    @classmethod
    def get_instance(cls) -> Self | None:
        assert _app_instance is None or isinstance(_app_instance, cls)
        return _app_instance

    @classmethod
    def _set_instance(cls, instance: Self | None) -> None:
        global _app_instance
        _app_instance = instance

    @classmethod
    @contextlib.contextmanager
    def locked_get_instance(cls) -> Iterator[Self | None]:
        with cls._life_cycle_mutex:
            yield cls.get_instance()

    @classmethod
    def running(cls) -> bool:
        return cls.get_instance() is not None

    @classmethod
    def start(
        cls, configuration: gdbsupport.Configuration | None, *args: Any, **kwargs: Any
    ) -> None:
        assert threading.current_thread() is threading.main_thread()

        if configuration is None:
            raise gdb.GdbError("Altui not supported if standard streams are not TTYs.")

        def run() -> None:
            # Mypy doesn't understand that this code would never be reached is configuration
            # were None.
            assert configuration is not None

            self = cls(configuration, thread, init_barrier, *args, **kwargs)
            self.run()

            ioutil.reset_tty(configuration.real_tty_streams.stdout_fd)
            configuration.io_thread_ipc_queue.send(gdbsupport.IOThreadMessage.APP_EXITED)

        with cls.locked_get_instance() as instance:
            if instance is not None:
                raise gdb.GdbError("Already enabled.")

            init_barrier = threading.Barrier(2)
            thread = configuration.make_thread(run, "ui", daemon=True)
            thread.start()
            init_barrier.wait()

    def _assert_in_ui_thread(self, func_name: str = "this function") -> None:
        if threading.current_thread() is not self._thread:
            self.configuration.handle_fatal_error(
                msg=f"{func_name} can only be executed on the UI thread"
            )

    @classmethod
    def stop(cls) -> None:
        assert (
            threading.current_thread() is threading.main_thread()
            or not threading.main_thread().is_alive()  # CHECK IS IO THREAD
        )

        with cls.locked_get_instance() as instance:
            if instance is None:
                raise gdb.GdbError("Not enabled.")
            instance.on_ui_thread_wait(instance.exit, _use_locked_get_instance=False)
            assert cls.get_instance() is None

    def __init__(
        self,
        configuration: gdbsupport.Configuration,
        thread: threading.Thread,
        init_barrier: threading.Barrier,
        *args,
        **kwargs,
    ) -> None:
        self.configuration = configuration
        self._thread = thread
        self._init_barrier = init_barrier

        self._is_ready = False

        self._init_exit_stack = contextlib.ExitStack()
        self._init_exit_stack.enter_context(self.configuration.real_tty_streams_as_sys_std())

        self._connected_gdb_events: list[tuple[gdb.EventRegistry, Callable[..., None]]] = []
        # We should connect to gdb.events.gdb_exiting but it doesn't exist in the current version
        # of bundled GDB.
        # self._connect_event_thread_safe(
        #     gdb.events.gdb_exiting,
        #     lambda event: self._disconnect_events_now,
        # )

        super().__init__(*args, **kwargs)

        self._set_instance(self)

        self.configuration.io_thread_ipc_queue.send(gdbsupport.IOThreadMessage.APP_STARTED)

    @ui_thread_only
    @fatal_exceptions
    def on_ready(self) -> None:
        self._init_exit_stack.close()

        # Need to set it again.
        ioutil.allow_ctrl_c_handling(self.configuration.real_tty_streams.stdin_fd)

        self._is_ready = True
        self._init_barrier.wait()

    @fatal_exceptions
    def exit(self, *args: Any, _use_locked_get_instance: bool = True, **kwargs: Any) -> None:
        exit_stack = contextlib.ExitStack()
        if _use_locked_get_instance:
            instance = exit_stack.enter_context(self.locked_get_instance())
            assert self is instance

        with exit_stack:
            self._assert_in_ui_thread()

            self._disconnect_events_thread_safe()

            self._init_exit_stack.close()
            self._set_instance(None)
            with self.configuration.real_tty_streams_as_sys_std():  # FIXME: Needed?
                super().exit(*args, **kwargs)

    def on_ui_thread(self, callback: Callable, *args: Any, **kwargs: Any) -> None:
        self.call_next(log_exceptions(callback), *args, **kwargs)

    def on_ui_thread_wait(
        self,
        callback: Callable[..., _T],
        *args: Any,
        **kwargs: Any,
    ) -> _T | None:
        if threading.current_thread() is self._thread:
            self.configuration.handle_fatal_error(
                msg=f"on_ui_thread_wait cannot be executed on the UI thread"
            )

        return self.call_from_thread(log_exceptions(callback), *args, **kwargs)

    def on_gdb_thread(self, callback: Callable, *args: Any, **kwargs: Any) -> None:
        if not threading.main_thread().is_alive():
            # This avoids crashes if the main thread already exited. We could avoid the same by
            # disconnecting on gdb_exiting but that's not supported by our current bundled GDB.
            return

        if args or kwargs:
            callback = functools.partial(callback, *args, **kwargs)

        # Exceptions raised by the callback are already printed by GDB.
        gdb.post_event(callback)

    def connect_event_thread_safe(
        self,
        registry: gdb.EventRegistry,
        callback: Callable[..., None],
        *predefined_args,
        **predefined_kwargs,
    ) -> None:
        def wrapper(*runtime_args, **runtime_kwargs) -> None:
            if self.get_instance() is self:
                callback(
                    *predefined_args,
                    *runtime_args,
                    **predefined_kwargs,
                    **runtime_kwargs,
                )

        def real_connect():
            registry.connect(wrapper)
            self._connected_gdb_events.append((registry, wrapper))

        self.on_gdb_thread(real_connect)

    @gdb_thread_only
    def _disconnect_events_now(self) -> None:
        assert threading.current_thread() is threading.main_thread()

        for registry, callback in self._connected_gdb_events:
            registry.disconnect(callback)
        self._connected_gdb_events.clear()

    def _disconnect_events_thread_safe(self) -> None:
        self.on_gdb_thread(self._disconnect_events_now)

    @classmethod
    def process_output(cls, buff: bytes) -> bool:
        raise NotImplementedError

    def terminal_execute(self, command: str) -> None:
        home = _make_ctrl_from_char("A")
        clear_after_cursor = _make_ctrl_from_char("K")
        os.write(
            self.configuration.gdb_io_fd,
            f"{home}{clear_after_cursor}{command}\n".encode("utf-8"),
        )
