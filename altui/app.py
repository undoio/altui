import contextlib
import os
import threading
from pathlib import Path
from typing import Any, Callable, TypeVar

import gdb  # type: ignore[import]
from src.udbpy import comms, engine  # type: ignore[import]
from src.udbpy.gdb_extensions import gdbutils, udb_base  # type: ignore[import]
from textual import containers, on, widgets
from textual.app import ComposeResult

from . import gdbapp, mi, status_bar, terminal, udbwidgets

_T = TypeVar("_T")


def _to_type_or_none(type_: Callable[..., _T], value: Any) -> _T | None:
    if value is None:
        return None

    try:
        return type_(value)
    except (TypeError, ValueError):
        return None


class UdbApp(gdbapp.GdbCompatibleApp):
    BINDINGS = [
        ("ctrl+w", "toggle_dark", "Toggle dark mode"),
        ("ctrl+x", "expand", "Test terminal expansion"),
    ]

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

    Horizontal#progress_panel {
        display: none;
        layer: progress_indicator;
        dock: top;
        padding: 0 1;
        border: tall $secondary;
        background: $panel;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._udb: udb_base.Udb = gdb._udb

        super().__init__(*args, **kwargs)

    def compose(self) -> ComposeResult:
        with containers.Horizontal(id="columns"):
            with containers.Vertical(id="column-central"):
                yield udbwidgets.SourceView(id="code", classes="main-window-panel")
                yield terminal.Terminal(id="terminal", classes="main-window-panel")

            with containers.Vertical(id="column-right"):
                with containers.Vertical(), widgets.TabbedContent():
                    with widgets.TabPane("Backtrace", id="backtrace-tab-pane"):
                        yield udbwidgets.UdbListView(
                            id="backtrace", classes="main-window-panel disable-on-execution"
                        )
                    with widgets.TabPane("Threads", id="threads-tab-pane"):
                        yield udbwidgets.UdbListView(
                            id="threads", classes="main-window-panel disable-on-execution"
                        )

                with containers.Vertical(), widgets.TabbedContent():
                    with widgets.TabPane("Variables", id="variables-tab-pane"):
                        yield udbwidgets.UdbListView(
                            id="variables", classes="main-window-panel disable-on-execution"
                        )
                    with widgets.TabPane("Registers", id="registers-tab-pane"):
                        yield udbwidgets.UdbListView(
                            id="registers", classes="main-window-panel disable-on-execution"
                        )

        yield status_bar.StatusBar()

        with containers.Horizontal(id="progress_panel"):
            yield widgets.ProgressBar(id="progress_indicator", total=100, show_eta=False)

    def on_ready(self) -> None:
        term = self.query_one("#terminal", terminal.Terminal)
        term.focus()
        term.attach_to_tty(
            fd=self.configuration.gdb_io_fd,
            pid=os.getpid(),
            read_from_tty=False,
        )

        def change_widgets_enablement_gdb_thread(enabled: bool, event: gdb.ThreadEvent) -> None:
            self.on_ui_thread(self._change_widgets_enablement, enabled)

        self.connect_event_thread_safe(gdb.events.before_prompt, self._update_ui)
        self.connect_event_thread_safe(gdb.events.cont, change_widgets_enablement_gdb_thread, False)
        self.connect_event_thread_safe(gdb.events.stop, change_widgets_enablement_gdb_thread, True)

        # Note that this can cause GDB to crash, for instance if you pass `-ex "altui enable" -ex
        # start` to UDB.
        self._update_ui()

    @classmethod
    def process_output(cls, buff: bytes) -> bool:
        with cls.locked_get_instance() as instance:
            if instance is None:
                # We cannot deal with the output as there's no app running.
                return False

            # Pylint doesn't know that instance is an instance of this class.
            # pylint: disable=protected-access
            instance.on_ui_thread(instance._process_output_internal, buff)
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

    def _update_ui(self) -> None:
        # Doing MI commands from this event leads to a GDB crash.
        # Investigate whether this happens in newer versions of GDB>
        self.on_gdb_thread(self._update_ui_callback)

    def _update_ui_callback(self) -> None:
        if self.get_instance() is not self:
            return

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

        target_name = None
        time = None
        time_extent = None
        with contextlib.suppress(comms.WrongExecutionModeError):
            time = self._udb.time.get()
            time_extent = self._udb.get_event_log_extent()

            selected_inferior = self._udb.inferiors.selected
            if selected_inferior.recording is not None:
                target_name = selected_inferior.recording.name
            else:
                filename = selected_inferior.gdb_inferior.progspace.filename
                if filename is not None:
                    target_name = Path(filename).name

        self.on_ui_thread(
            self._set_ui_to_values,
            stack=stack,
            stack_arguments=stack_arguments,
            stack_selected_frame_index=stack_selected_frame_index,
            local_vars=local_vars,
            execution_mode=self._udb.get_execution_mode(),
            time=time,
            time_extent=time_extent,
            target_name=target_name,
        )

    def _set_ui_to_values(
        self,
        stack: list[dict[str, Any]],
        stack_arguments: list[dict[str, Any]],
        stack_selected_frame_index: int | None,
        local_vars: list[dict[str, Any]],
        execution_mode: engine.ExecutionMode,
        time: engine.Time,
        time_extent: engine.LogExtent,
        target_name: str,
    ) -> None:
        def format_var(d: dict[str, Any]) -> str:
            name = d.get("name", "???")
            value = d.get("value", "...")
            return f"{name} = {value}"

        vars_lv = self.query_one("#variables", udbwidgets.UdbListView)
        vars_lv.clear()

        source_path = None
        source_line = None
        bt_lv: udbwidgets.UdbListView[int] = self.query_one("#backtrace", udbwidgets.UdbListView)
        bt_lv.clear()
        for i, (frame, frame_args) in enumerate(zip(stack, stack_arguments)):
            formatted_args = [format_var(arg) for arg in frame_args.get("args", [])]
            arg_list = ", ".join(formatted_args)
            func_name = frame.get("func", "???")
            bt_lv.append(
                f"{func_name}({arg_list})",
                f'{frame.get("file", "???")}, line {frame.get("line", "???")}',
                extra=i,
            )
            if i == stack_selected_frame_index:
                source_path = _to_type_or_none(Path, frame.get("fullname"))
                source_line = _to_type_or_none(int, frame.get("line"))
                for arg in formatted_args:
                    vars_lv.append(arg)

        bt_lv.move_cursor(row=stack_selected_frame_index)

        code = self.query_one("#code", udbwidgets.SourceView)
        code.path = Path(source_path) if source_path is not None else None
        code.current_line = source_line

        for var in local_vars:
            vars_lv.append(format_var(var))

        status = self.query_one(status_bar.StatusBar)
        status.update(
            execution_mode=execution_mode,
            target_name=target_name,
            time=time,
            time_extent=time_extent,
            source_path=source_path,
        )

    @on(udbwidgets.UdbListView.ItemSelected, "#backtrace")
    def _backtrace_selected(self, event: udbwidgets.UdbListView.ItemSelected[int]) -> None:
        frame_num = event.value.extra

        def set_frame():
            gdbutils.execute_to_string(f"frame {frame_num}")
            self._update_ui()

        self.on_gdb_thread(set_frame)

    def progress_show(self) -> None:
        term = self.query_one("#terminal", terminal.Terminal)
        progress_panel = self.query_one("#progress_panel", containers.Horizontal)
        progress_indicator = self.query_one("#progress_indicator", widgets.ProgressBar)

        progress_panel.styles.display = "block"
        progress_panel.styles.width = w = progress_indicator.virtual_region_with_margin.width + 4
        progress_panel.styles.height = progress_indicator.virtual_region_with_margin.height + 2

        progress_panel.styles.margin = (
            term.content_region.y,
            0,
            0,
            term.content_region.x + term.outer_size.width - w - 4,
        )
        self.progress_update(0)

    def progress_hide(self) -> None:
        progress_panel = self.query_one("#progress_panel", containers.Horizontal)
        progress_panel.styles.display = "none"

    def progress_update(self, total: int) -> None:
        progress_indicator = self.query_one("#progress_indicator", widgets.ProgressBar)
        progress_indicator.update(progress=total)

    def _action_expand(self) -> None:
        term = self.query_one("#terminal", terminal.Terminal)
        code = self.query_one("#code", udbwidgets.SourceView)

        if term.styles.min_height is None:
            term.styles.min_height = term.outer_size.height + code.outer_size.height - 5
        else:
            term.styles.min_height = None
