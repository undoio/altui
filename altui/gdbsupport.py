from __future__ import annotations

import contextlib
import dataclasses
import enum
import fcntl
import os
import select
import signal
import struct
import sys
import termios
import threading
import traceback
import tty
import warnings
from typing import Any, Callable, Iterator, NoReturn
from unittest import mock

from . import ioutil


class IOThreadMessage(bytes, enum.Enum):
    APP_STARTED = b"A"
    APP_EXITED = b"a"
    MAIN_THREAD_TERMINATED = b"q"


@dataclasses.dataclass(frozen=True)
class IOThreadIPCQueue:
    waitable_fd: int
    _write_fd: int

    @classmethod
    def make(cls) -> IOThreadIPCQueue:
        return cls(*os.pipe())

    def receive(self) -> IOThreadMessage:
        return IOThreadMessage(os.read(self.waitable_fd, 1))

    def send(self, msg: IOThreadMessage) -> None:
        os.write(self._write_fd, msg.value)


class UnsupportedError(Exception):
    pass


class Configuration:
    def __init__(self) -> None:
        from src.udbpy import report  # type: ignore[import]

        assert (
            "rich" not in sys.modules
        ), "The rich package was imported before setting redirection up"
        assert (
            "textual" not in sys.modules
        ), "The textual package was imported before setting redirection up"

        if not ioutil.Streams.standard().all_same_tty:
            raise UnsupportedError("Altui can only run on terminals.")
        if report.mi_mode:
            raise UnsupportedError("Altui cannot run in MI mode.")
        term = os.getenv("TERM")
        if term and not term.startswith(("xterm", "linux", "screen")):
            raise UnsupportedError(f"Altui doesn't support your current terminal: {term!r}")

        self.main_watcher_thread = self.make_thread(self._main_watcher_thread_func, "main_watcher")
        self.io_thread = self.make_thread(self._io_thread_func, "io")

        self.io_thread_ipc_queue = IOThreadIPCQueue.make()
        self.real_tty_streams = ioutil.Streams.standard().dup()
        self.gdb_io_fd, new_std_stream_fd = os.openpty()

        # Copy the attributes from the real standard output so they behave the same.
        # FIXME: Needed?
        termios.tcsetattr(
            new_std_stream_fd,
            termios.TCSAFLUSH,
            termios.tcgetattr(self.real_tty_streams.stdout_fd),
        )
        # Set the stream to raw but then restore ISIG which we need for CTRL-C to work.
        tty.setraw(self.real_tty_streams.stdin_fd)
        ioutil.allow_ctrl_c_handling(self.real_tty_streams.stdin_fd)

        # Replace the standard streams with the new PTY. This is going to be used both by GDB and
        # the debuggee.
        for fd in ioutil.Streams.standard():
            os.dup2(new_std_stream_fd, fd, inheritable=True)
        os.close(new_std_stream_fd)

        self._allow_sigwinch_from_threads()

        # Set the window size for the new TTY based on the size of the real stdout
        self.update_window_size()

        warnings.filterwarnings(
            "ignore",
            category=RuntimeWarning,
            message=r"coroutine 'MessagePump\._post_message' was never awaited",
        )

        with self.real_tty_streams_as_sys_std():
            import rich.console  # pylint: disable=unused-import,import-outside-toplevel
            import textual  # pylint: disable=unused-import,import-outside-toplevel

        for thread in self.main_watcher_thread, self.io_thread:
            thread.start()

    @contextlib.contextmanager
    def real_tty_streams_as_sys_std(self) -> Iterator[None]:
        with contextlib.ExitStack() as exit_stack:
            for std_base_name, file in zip(
                ("stdin", "stdout", "stderr"),
                self.real_tty_streams.file_objs(),
            ):
                for std_name in (std_base_name, f"__{std_base_name}__"):
                    exit_stack.enter_context(
                        mock.patch(f"sys.{std_name}", file),
                    )

            yield

    def handle_fatal_error(
        self,
        *,
        msg: str = "",
        exc: BaseException | None = None,
    ) -> NoReturn:
        if exc is None:
            exc = sys.exc_info()[1]

        if exc is None:
            stack = traceback.format_stack()
            exc_msg = ""
        else:
            stack = traceback.format_exception(exc)
            exc_msg = f": {exc}"
        stack_msg = "".join(stack).rstrip() + "\n\n"

        if msg:
            msg = f": {msg}"

        full_msg = f"\n{stack_msg}Fatal error{msg}{exc_msg}\n\n"

        ioutil.reset_tty(self.real_tty_streams.stdout_fd)
        os.write(
            self.real_tty_streams.stderr_fd,
            full_msg.encode("utf-8", errors="backslashreplace"),
        )
        os.abort()

    def _allow_sigwinch_from_threads(self):
        real_signal = signal.signal

        def fake_signal(signalnum: int, *args, **kwargs) -> Any:
            if signalnum == signal.SIGWINCH:
                return signal.SIG_IGN
            return real_signal(signalnum, *args, **kwargs)

        signal.signal = fake_signal

    def make_thread(
        self,
        target: Callable[[], None],
        name: str,
        *,
        daemon: bool = False,
    ) -> threading.Thread:
        def wrapper():
            try:
                target()
            except BaseException:
                # KeyboardInterrupt cannot happen in threads, so we are not accidentally catching
                # one here.
                self.handle_fatal_error(msg=f"thread {name!r} unexpectedly terminated")

        return threading.Thread(target=wrapper, name=name)

    def update_window_size(self) -> None:
        winsize = fcntl.ioctl(
            self.real_tty_streams.stdout_fd,
            termios.TIOCGWINSZ,
            struct.pack("HHHH", 0, 0, 0, 0),
        )
        fcntl.ioctl(self.gdb_io_fd, termios.TIOCSWINSZ, winsize)

    def _main_watcher_thread_func(self) -> None:
        threading.main_thread().join()
        self.io_thread_ipc_queue.send(IOThreadMessage.MAIN_THREAD_TERMINATED)

    def _io_thread_func(self) -> None:
        from . import app

        # It looks like programs never use more than 1KB even if here we allow a bigger buffer.
        max_buff_size = 1024

        while True:
            read_fds = [self.gdb_io_fd, self.io_thread_ipc_queue.waitable_fd]
            if not app.UdbApp.running():
                read_fds.append(self.real_tty_streams.stdin_fd)
            readable, _, _ = select.select(read_fds, [], [])

            if self.io_thread_ipc_queue.waitable_fd in readable:
                match self.io_thread_ipc_queue.receive():
                    case IOThreadMessage.APP_STARTED:
                        # This is used to break from the select so that different file descriptors
                        # can be listened to.
                        continue
                    case IOThreadMessage.APP_EXITED:
                        # App exited. If the main thread terminated as well, then we can quit this
                        # thread and let the process terminate.
                        # Otherwise, nothing should be done (maybe the user just switched back to
                        # terminal UI).
                        if not threading.main_thread().is_alive():
                            return
                    case IOThreadMessage.MAIN_THREAD_TERMINATED:
                        # Use an exception instead.
                        if app.UdbApp.running():
                            app.UdbApp.stop()
                        else:
                            return
                    case _ as unhandled:
                        raise ValueError(f"Invalid message: {unhandled!r}")

            if self.real_tty_streams.stdin_fd in readable:
                buff = os.read(self.real_tty_streams.stdin_fd, max_buff_size)
                os.write(self.gdb_io_fd, buff)
                continue

            if self.gdb_io_fd in readable:
                buff = os.read(self.gdb_io_fd, max_buff_size)
                if not app.UdbApp.process_output(buff):
                    os.write(self.real_tty_streams.stdout_fd, buff)
