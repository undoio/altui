import random
import time
from typing import Callable

import gdb  # type: ignore[import]

from . import app, gdbsupport, telemetry_support


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


class _FakeProgress(gdb.Command):
    def __init__(self) -> None:
        super().__init__("fake-progress", gdb.COMMAND_USER)

    def invoke(self, args: str, from_tty: bool) -> None:
        instance = app.UdbApp.get_instance()
        if instance is None:
            raise gdb.GdbError(f"This command only works under altui.")

        # pylint: disable=protected-access
        try:
            instance.on_ui_thread_wait(instance._change_widgets_enablement, False)
            instance.on_ui_thread(instance.progress_show)

            progress = 0
            while True:
                time.sleep(0.6)
                progress = min(
                    progress + random.choice([1, 3, 7, 11, 15]),
                    100,
                )
                instance.on_ui_thread(instance.progress_update, progress)
                if progress == 100:
                    break

            time.sleep(0.3)
        finally:
            instance.on_ui_thread(instance.progress_hide)
            instance.on_ui_thread(instance._change_widgets_enablement, True)


def register(configuration: gdbsupport.Configuration | None, err_msg: str | None) -> None:
    assert bool(configuration) != bool(err_msg), f"{configuration=}; {err_msg=}"

    _AltuiPrefixCommand()

    def enable(c: gdbsupport.Configuration) -> None:
        telemetry_support.get(gdb._udb).enabled = True  # pylint: disable=protected-access

        assert c is configuration
        app.UdbApp.start(c)

    def disable(c: gdbsupport.Configuration) -> None:
        telemetry_support.get(gdb._udb).disabled = True  # pylint: disable=protected-access

        assert c is configuration
        app.UdbApp.stop()

    _AltuiCommand("altui enable", configuration, err_msg, enable)
    _AltuiCommand("altui disable", configuration, err_msg, disable)

    _FakeProgress()
