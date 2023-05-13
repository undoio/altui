from __future__ import annotations

import contextlib
import functools
import threading
from typing import Any, Callable, Iterator, TypeVar

import gdb  # type: ignore
from textual.app import App
from typing_extensions import Self

from . import gdbsupport, ioutil

_T = TypeVar("_T")

_app_instance: GdbCompatibleApp | None = None


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

            configuration.io_thread_ipc_queue.send(gdbsupport.IOThreadMessage.APP_EXITED)

        with cls.locked_get_instance() as instance:
            if instance is not None:
                raise gdb.GdbError("Already enabled.")

            init_barrier = threading.Barrier(2)
            thread = configuration.make_thread(run, "ui", daemon=True)
            thread.start()
            init_barrier.wait()

    @classmethod
    def _assert_in_ui_thread(cls) -> None:
        # FIXME: add a place argument.
        instance = cls.get_instance()
        # Pylint doesn't know that instance is an instance of this class.
        # pylint: disable=protected-access
        if instance is None or instance._thread is not threading.current_thread():
            raise gdb.GdbError("Must be called in the UI thread")

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

    def on_ready(self) -> None:
        self._init_exit_stack.close()

        # Need to set it again.
        ioutil.allow_ctrl_c_handling(self.configuration.real_tty_streams.stdin_fd)

        self._is_ready = True
        self._init_barrier.wait()

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
        self.call_next(callback, *args, **kwargs)

    def on_ui_thread_wait(self, callback: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        return self.call_from_thread(callback, *args, **kwargs)

    def on_gdb_thread(self, callback: Callable, *args: Any, **kwargs: Any) -> None:
        if not threading.main_thread().is_alive():
            # This avoids crashes if the main thread already exited. We could avoid the same by
            # disconnecting on gdb_exiting but that's not supported by our current bundled GDB.
            return

        if args or kwargs:
            callback = functools.partial(callback, *args, **kwargs)

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
