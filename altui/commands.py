from typing import Callable

import gdb  # type: ignore[import]

from . import app, gdbsupport


class _AltuiPrefixCommand(gdb.Command):
    def __init__(self) -> None:
        super().__init__("altui", gdb.COMMAND_USER, prefix=True)


class _AltuiCommand(gdb.Command):
    def __init__(
        self,
        name: str,
        configuration: gdbsupport.Configuration | None,
        err_msg: str | None,
        callback: Callable[[gdbsupport.Configuration], None],
    ) -> None:
        super().__init__(name, gdb.COMMAND_USER)

        self._configuration = configuration
        self._err_msg = err_msg
        self._callback = callback

    def invoke(self, args: str, from_tty: bool) -> None:
        self.dont_repeat()

        if self._configuration is None:
            raise gdb.GdbError(self._err_msg)

        self._callback(self._configuration)


def register(configuration: gdbsupport.Configuration | None, err_msg: str | None) -> None:
    assert bool(configuration) != bool(err_msg), f"{configuration=}; {err_msg=}"

    _AltuiPrefixCommand()
    _AltuiCommand(
        "altui enable",
        configuration,
        err_msg,
        app.UdbApp.start,
    )
    _AltuiCommand(
        "altui disable",
        configuration,
        err_msg,
        lambda c: app.UdbApp.stop(),
    )
