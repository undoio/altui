import functools
import os
import threading
from pathlib import Path
from typing import Any, Callable, TypeVar

import gdb  # type: ignore[import]
from src.udbpy.gdb_extensions import gdbutils, udb_base  # type: ignore[import]
from textual import containers, on, widgets
from textual.app import ComposeResult

from . import gdbapp, mi, source_view, terminal


_T = TypeVar("_T")


def _to_type_or_none(type_: Callable[..., _T], value: Any) -> _T | None:
    if value is None:
        return None

    try:
        return type_(value)
    except (TypeError, ValueError):
        return None


class UdbApp(gdbapp.GdbCompatibleApp):
    BINDINGS = [("ctrl+w", "toggle_dark", "Toggle Dark Mode")]

    DEFAULT_CSS = """
    # https://github.com/Textualize/textual/issues/2411
    # See https://discord.com/channels/1026214085173461072/1033754296224841768/1103292048666275902
    ContentSwitcher {
        height: 1fr !important;
    }
    Horizontal > * {
        width: 1fr;
    }

    # Avoid padding around widgets in tabs.
    TabPane {
        padding: 0 !important;
    }

    #column-central {
        width: 70%
    }

    .main-window-panel {
        border: round $primary;
    }

    .main-window-panel:focus {
        border: round $accent-lighten-2;
    }

    Terminal {
        overflow-x: auto;
        overflow-y: scroll;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._udb: udb_base.Udb = gdb._udb

        super().__init__(*args, **kwargs)

    def compose(self) -> ComposeResult:
        with containers.Horizontal(id="columns"):
            with containers.Vertical(id="column-central"):
                yield source_view.SourceView(id="code", classes="main-window-panel")
                yield terminal.Terminal(id="terminal", classes="main-window-panel")

            with containers.Vertical(id="column-right"):
                with containers.Vertical(), widgets.TabbedContent():
                    with widgets.TabPane("Backtrace", id="backtrace-tab-pane"):
                        yield widgets.ListView(
                            id="backtrace", classes="main-window-panel disable-on-execution"
                        )
                    with widgets.TabPane("Threads", id="threads-tab-pane"):
                        yield widgets.ListView(
                            id="threads", classes="main-window-panel disable-on-execution"
                        )

                with containers.Vertical(), widgets.TabbedContent():
                    with widgets.TabPane("Variables", id="variables-tab-pane"):
                        yield widgets.ListView(
                            id="variables", classes="main-window-panel disable-on-execution"
                        )
                    with widgets.TabPane("Registers", id="registers-tab-pane"):
                        yield widgets.ListView(
                            id="registers", classes="main-window-panel disable-on-execution"
                        )

        yield widgets.Footer()

    def on_ready(self) -> None:
        term = self.query_one("#terminal", terminal.Terminal)
        term.focus()
        term.attach_to_tty(
            fd=self.configuration.gdb_io_fd,
            pid=os.getpid(),
            read_from_tty=False,
        )

        def change_widgets_enablement_gdb_thread(enabled: bool, event: gdb.ThreadEvent) -> None:
            self.app.call_from_thread(self._change_widgets_enablement, enabled)

        def connect_events():
            gdb.events.before_prompt.connect(self._before_prompt)

            gdb.events.cont.connect(functools.partial(change_widgets_enablement_gdb_thread, False))
            gdb.events.stop.connect(functools.partial(change_widgets_enablement_gdb_thread, True))

        gdb.post_event(connect_events)

    @classmethod
    def process_output(cls, buff: bytes) -> bool:
        with cls.locked_get_instance() as instance:
            if instance is None:
                # We cannot deal with the output as there's no app running.
                return False

            # Pylint doesn't know that instance is an instance of this class.
            # pylint: disable=protected-access
            instance.call_from_thread(instance._process_output_internal, buff)
            return True

    def _process_output_internal(self, buff: bytes) -> None:
        self._assert_in_ui_thread()

        if self._is_ready:
            self.query_one(terminal.Terminal).process_output(
                buff.decode("utf-8", errors="backslashreplace")
            )

    def _change_widgets_enablement(self, enabled: bool) -> None:
        for widget in self.query(".disable-on-execution"):  # pylint: disable=not-an-iterable
            widget.disabled = not enabled

    def _before_prompt(self) -> None:
        # Doing MI commands from this event leads to a GDB crash.
        # Investigate whether this happens in newer versions of GDB>
        gdb.post_event(self._before_prompt_real)

    def _before_prompt_real(self) -> None:
        assert threading.current_thread() is threading.main_thread()

        # FIXME: only requests a few frames initially as this could take a while.
        # Also consider if it's possible not updating everything at each prompt.
        stack = mi.execute("-stack-list-frames").get("stack", [])
        stack_arguments = mi.execute("-stack-list-arguments 1").get("stack-args", [])
        stack_selected_frame_index = _to_type_or_none(
            int,
            mi.execute("-stack-info-frame").get("frame", {}).get("level", None),
        )

        local_vars = mi.execute("-stack-list-locals 1").get("locals", [])

        self.app.call_from_thread(
            self._update_ui,
            stack=stack,
            stack_arguments=stack_arguments,
            stack_selected_frame_index=stack_selected_frame_index,
            local_vars=local_vars,
        )

    def _update_ui(
        self,
        stack: list[dict[str, Any]],
        stack_arguments: list[dict[str, Any]],
        stack_selected_frame_index: int | None,
        local_vars: list[dict[str, Any]],
    ) -> None:
        def format_var(d: dict[str, Any]) -> str:
            name = d.get("name", "???")
            value = d.get("value", "...")
            return f"{name} = {value}"

        vars_lv = self.query_one("#variables", widgets.ListView)
        vars_lv.clear()

        source_path = None
        source_line = None
        bt_lv = self.query_one("#backtrace", widgets.ListView)
        bt_lv.clear()
        for i, (frame, frame_args) in enumerate(zip(stack, stack_arguments)):
            formatted_args = [format_var(arg) for arg in frame_args.get("args", [])]
            arg_list = ", ".join(formatted_args)
            func_name = frame.get("func", "???")
            bt_lv.append(widgets.ListItem(widgets.Label(f"{func_name}({arg_list})")))
            if i == stack_selected_frame_index:
                source_path = _to_type_or_none(Path, frame.get("fullname"))
                source_line = _to_type_or_none(int, frame.get("line"))
                for arg in formatted_args:
                    vars_lv.append(widgets.ListItem(widgets.Label(arg)))

        bt_lv.index = stack_selected_frame_index

        code = self.query_one("#code", source_view.SourceView)
        code.path = Path(source_path) if source_path is not None else None
        code.current_line = source_line

        for var in local_vars:
            vars_lv.append(widgets.ListItem(widgets.Label(format_var(var))))
        vars_lv.index = None

    @on(widgets.ListView.Selected)  # FIXME: "#backtrace"
    def _backtrace_selected(self, event: widgets.ListView.Selected) -> None:
        list_view = event.list_view
        if list_view.id != "backtrace":  # FIXME
            return

        index = list_view.index

        def set_frame():
            gdbutils.execute_to_string(f"frame {index}")
            self._before_prompt()

        gdb.post_event(set_frame)
