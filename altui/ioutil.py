from __future__ import annotations

import dataclasses
import functools
import os
import termios
import tty
from typing import IO, Iterator


@dataclasses.dataclass(frozen=True)
class Streams:
    stdin_fd: int
    stdout_fd: int
    stderr_fd: int

    def __iter__(self) -> Iterator[int]:
        return iter((self.stdin_fd, self.stdout_fd, self.stderr_fd))

    @classmethod
    @functools.cache
    def standard(cls) -> Streams:
        return cls(*range(3))

    def dup(self) -> Streams:
        return Streams(
            *(os.dup(fd) for fd in iter(self)),
        )

    @functools.cached_property
    def all_same_tty(self) -> bool:
        paths = set()

        # Are all the file descriptors TTYs?
        for fd in iter(self):
            if not os.isatty(fd):
                return False
            paths.add(os.ttyname(fd))

        # Were all TTYs pointing to the same device?
        return len(paths) == 1

    def file_objs(self) -> Iterator[IO[str]]:
        for fd, mode in zip(iter(self), "rww"):
            yield os.fdopen(fd, mode, closefd=False)


def allow_ctrl_c_handling(fd: int) -> None:
    mode = termios.tcgetattr(fd)
    mode[tty.LFLAG] |= termios.ISIG
    termios.tcsetattr(fd, termios.TCSAFLUSH, mode)


def reset_tty(fd: int) -> None:
    if not os.isatty(fd):
        return

    # Return the terminal to the normal mode.
    tty.setcbreak(fd, termios.TCSANOW)
    mode = termios.tcgetattr(fd)
    # Repostore canonical mode and echoing of input.
    mode[tty.LFLAG] |= termios.ECHO | termios.ICANON
    # Make \n imply a \r so printing works as normal in Python.
    mode[tty.OFLAG] |= termios.OPOST | termios.ONLCR
    termios.tcsetattr(fd, termios.TCSANOW, mode)

    codes = [
        f"\N{ESCAPE}{c}"
        for c in (
            # Reset to initial state.
            "c",
            # Reset color and style.
            "[0m",
            # Select the default character set (in case the alternate one was selected producing
            # weird drawings instead of normal characters).
            "(B",
            # Enable the cursor.
            "[?25h",
            # Erase the whole display (without affecting the cursor).
            "[J",
        )
    ]
    os.write(fd, "".join(codes).encode("ascii"))
