import contextlib
import dataclasses
import itertools
import operator
import os
import threading
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

import gdb  # type: ignore[import]
import rich.markup
from rich.markdown import Markdown
from rich.text import Text
from src.udbpy import comms, engine, textutil  # type: ignore[import]
from src.udbpy.gdb_extensions import gdbutils, udb_base  # type: ignore[import]
from textual import containers, on, widgets
from textual.app import ComposeResult

from . import mi, status_bar, terminal, udbwidgets
from .gdbapp import (
    GdbCompatibleApp,
    fatal_exceptions,
    gdb_thread_only,
    ui_thread_only,
    ui_thread_only_without_handling_exceptions,
)

_T = TypeVar("_T")


@dataclasses.dataclass(frozen=True, order=True)
class _BookmarksCellNameAndCommand:
    sort_weight: int
    name: str
    goto_command: str | None

    def __rich__(self) -> Text:
        return Text.from_markup(self.name)


def _to_type_or_none(type_: Callable[..., _T], value: Any) -> _T | None:
    if value is None:
        return None

    try:
        return type_(value)
    except (TypeError, ValueError):
        return None


class UdbApp(GdbCompatibleApp):
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
        border: round $secondary;
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

    _CURRENT_ITEM_MARKER = "\N{BLACK RIGHT-POINTING TRIANGLE}"

    _BOOKMARKS_NAME_COLUMN = "Name"
    _BOOKMARKS_TIME_COLUMN = "Time"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._udb: udb_base.Udb = gdb._udb

        super().__init__(*args, **kwargs)

    @ui_thread_only_without_handling_exceptions
    @fatal_exceptions
    def compose(self) -> ComposeResult:
        @contextlib.contextmanager
        def tabs() -> Iterator[None]:
            with (
                containers.Vertical(),
                widgets.TabbedContent(),
            ):
                yield

        @contextlib.contextmanager
        def tab_pane(title: str, base_id: str) -> Iterator[None]:
            with (
                widgets.TabPane(title, id=f"{base_id}-tab-pane"),
                containers.Vertical(
                    id=f"{base_id}-tab-pane-vertical",
                    classes="disable-on-execution",
                ),
            ):
                yield

        with containers.Horizontal(id="columns"):
            with containers.Vertical(id="column-central"):
                yield udbwidgets.SourceView(id="code", classes="main-window-panel")
                yield terminal.Terminal(id="terminal", classes="main-window-panel")

            with containers.Vertical(id="column-right"):
                with tabs():
                    with tab_pane("Backtrace", "backtrace"):
                        yield udbwidgets.UdbListView(
                            id="backtrace", classes="main-window-panel disable-on-execution"
                        )
                    with tab_pane("Threads", "threads"):
                        yield udbwidgets.UdbListView(
                            id="threads", classes="main-window-panel disable-on-execution"
                        )

                with tabs():
                    with tab_pane("Variables", "variables"):
                        yield udbwidgets.UdbListView(
                            id="variables", classes="main-window-panel disable-on-execution"
                        )
                    with tab_pane("Bookmarks", "bookmarks"):
                        t: udbwidgets.UdbTable = udbwidgets.UdbTable(
                            id="bookmarks", classes="main-window-panel disable-on-execution"
                        )
                        t.add_column("")
                        t.add_column(self._BOOKMARKS_NAME_COLUMN, key=self._BOOKMARKS_NAME_COLUMN)
                        t.add_column(self._BOOKMARKS_TIME_COLUMN, key=self._BOOKMARKS_TIME_COLUMN)
                        yield t

        yield status_bar.StatusBar()

        with containers.Horizontal(id="progress_panel"):
            yield widgets.ProgressBar(id="progress_indicator", total=100, show_eta=False)

    @ui_thread_only
    @fatal_exceptions
    def on_ready(self) -> None:
        term = self.query_one("#terminal", terminal.Terminal)
        term.focus()
        term.attach_to_tty(
            fd=self.configuration.gdb_io_fd,
            pid=os.getpid(),
            read_from_tty=False,
        )

        code = self.query_one("#code", udbwidgets.SourceView)
        code.write(Markdown((Path(__file__).parent / "welcome.md").read_text(encoding="utf-8")))

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

    @ui_thread_only
    # We cannot rely on logging exception (done by `ui_thread_only` via `log_exceptions`) as, to
    # log them, we need to go through this function.
    @fatal_exceptions
    def _process_output_internal(self, buff: bytes) -> None:
        if self._is_ready:
            self.query_one(terminal.Terminal).process_output(
                buff.decode("utf-8", errors="backslashreplace")
            )

    @ui_thread_only
    def _change_widgets_enablement(self, enabled: bool) -> None:
        for widget in self.query(".disable-on-execution"):  # pylint: disable=not-an-iterable
            widget.disabled = not enabled

    def _update_ui(self) -> None:
        # Doing MI commands from this event leads to a GDB crash.
        # Investigate whether this happens in newer versions of GDB>
        self.on_gdb_thread(self._update_ui_callback)

    @gdb_thread_only
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

        thread_info = mi.execute("-thread-info")
        local_vars = mi.execute("-stack-list-locals 1").get("locals", [])

        target_name = None
        current_time = None
        time_extent = None
        bookmarks = []
        time_next_undo = None
        time_next_redo = None
        with contextlib.suppress(comms.WrongExecutionModeError):
            current_time = self._udb.time.get()
            time_extent = self._udb.get_event_log_extent()
            bookmarks = list(self._udb.bookmarks.iter_bookmarks())

            selected_inferior = self._udb.inferiors.selected
            if selected_inferior.recording is not None:
                target_name = selected_inferior.recording.name
            else:
                filename = selected_inferior.gdb_inferior.progspace.filename
                if filename is not None:
                    target_name = Path(filename).name

            with contextlib.suppress(StopIteration):
                time_next_undo = next(self._udb.time.undo_items)
            with contextlib.suppress(StopIteration):
                time_next_redo = next(self._udb.time.redo_items)

        self.on_ui_thread(
            self._set_ui_to_values,
            stack=stack,
            stack_arguments=stack_arguments,
            stack_selected_frame_index=stack_selected_frame_index,
            thread_info=thread_info,
            local_vars=local_vars,
            execution_mode=self._udb.get_execution_mode(),
            current_time=current_time,
            time_extent=time_extent,
            target_name=target_name,
            bookmarks=bookmarks,
            time_next_undo=time_next_undo,
            time_next_redo=time_next_redo,
        )

    @ui_thread_only
    def _set_ui_to_values(
        self,
        stack: list[dict[str, Any]],
        stack_arguments: list[dict[str, Any]],
        stack_selected_frame_index: int | None,
        thread_info: dict[str, Any],
        local_vars: list[dict[str, Any]],
        execution_mode: engine.ExecutionMode,
        current_time: engine.Time | None,
        time_extent: engine.LogExtent | None,
        target_name: str,
        bookmarks: list[tuple[str, engine.Time]],
        time_next_undo: engine.Time | None,
        time_next_redo: engine.Time | None,
    ) -> None:
        # pylint: disable=too-many-locals

        def format_var(d: dict[str, Any]) -> str:
            name = d.get("name", "???")
            value = d.get("value", "...")
            return f"{name} = {value}"

        vars_lv = self.query_one("#variables", udbwidgets.UdbListView)
        vars_lv.clear()

        source_path = None
        source_line = None
        source_short_path = None
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
                source_short_path = frame.get("file")
                source_line = _to_type_or_none(int, frame.get("line"))
                for arg in formatted_args:
                    vars_lv.append(arg)

        bt_lv.move_cursor(row=stack_selected_frame_index)

        code = self.query_one("#code", udbwidgets.SourceView)
        code.path = source_path
        code.current_line = source_line
        code.border_title = source_short_path

        threads_lv: udbwidgets.UdbListView[int | None] = self.query_one(
            "#threads", udbwidgets.UdbListView
        )
        threads_lv.clear()
        selected_thread_id = _to_type_or_none(int, thread_info.get("current-thread-id", -1))
        for i, thread in enumerate(thread_info.get("threads", [])):
            thread_id = _to_type_or_none(int, thread.get("id", None))
            frame = thread.get("frame", {})
            file = frame.get("file", "???")
            line = frame.get("line", "???")
            func_name = frame.get("func", "???")
            arg_list = ", ".join(format_var(arg) for arg in frame.get("args", []))
            thread_id_formatted = f"[{thread_id}] "
            indent = " " * len(thread_id_formatted)
            threads_lv.append(
                f"{thread_id_formatted}{thread.get('target-id', 'Unknown thread details')}",
                f"{indent}{func_name}({arg_list})\n{indent}{file}, line {line}",
                extra=thread_id,
            )
            if selected_thread_id == thread_id:
                threads_lv.move_cursor(row=i)

        for var in local_vars:
            if var.get("name") != "__PRETTY_FUNCTION__":
                vars_lv.append(format_var(var))

        bookmarks_table = self.query_one("#bookmarks", udbwidgets.UdbTable)
        bookmarks_table.clear()
        row_with_current_time = None
        aligned_times = textutil.align_recording_times(
            itertools.chain(
                [current_time, time_next_undo, time_next_redo],
                map(operator.itemgetter(1), bookmarks),
            )
        )
        current_time_aligned = next(aligned_times)
        time_next_undo_aligned = next(aligned_times)
        time_next_redo_aligned = next(aligned_times)
        for (name, time), time_aligned in zip(bookmarks, aligned_times):
            row = bookmarks_table.add_row(
                self._CURRENT_ITEM_MARKER if time == current_time else "",
                _BookmarksCellNameAndCommand(
                    0,
                    rich.markup.escape(name),
                    f"ugo bookmark {textutil.gdb_command_arg_escape(name)}",
                ),
                time_aligned,
            )
            if time == current_time and row_with_current_time is None:
                row_with_current_time = row
        if current_time is not None and row_with_current_time is None:
            row_with_current_time = bookmarks_table.add_row(
                self._CURRENT_ITEM_MARKER,
                _BookmarksCellNameAndCommand(
                    0,
                    "[italic][dim](current time)[/dim][/italic]",
                    None,
                ),
                current_time_aligned,
            )
        if time_next_undo is not None:
            bookmarks_table.add_row(
                self._CURRENT_ITEM_MARKER if time_next_undo == current_time else "",
                _BookmarksCellNameAndCommand(
                    1,
                    "[italic][dim](undo target)[/dim][/italic]",
                    "ugo undo",
                ),
                time_next_undo_aligned,
            )
        if time_next_redo is not None:
            bookmarks_table.add_row(
                self._CURRENT_ITEM_MARKER if time_next_redo == current_time else "",
                _BookmarksCellNameAndCommand(
                    2,
                    "[italic][dim](redo target)[/dim][/italic]",
                    "ugo redo",
                ),
                time_next_redo_aligned,
            )
        bookmarks_table.sort(self._BOOKMARKS_TIME_COLUMN, self._BOOKMARKS_NAME_COLUMN)
        if row_with_current_time is not None:
            # https://github.com/Textualize/textual/issues/2587.
            row_index = bookmarks_table._row_locations.get(  # pylint: disable=protected-access
                row_with_current_time
            )
            bookmarks_table.move_cursor(row=row_index)

        status = self.query_one(status_bar.StatusBar)
        status.update(
            execution_mode=execution_mode,
            target_name=target_name,
            time=current_time,
            time_extent=time_extent,
            source_path=source_path,
        )

    @on(udbwidgets.UdbListView.ItemSelected, "#backtrace")
    @ui_thread_only
    def _backtrace_selected(self, event: udbwidgets.UdbListView.ItemSelected[int]) -> None:
        frame_num = event.value.extra

        def set_frame() -> None:
            gdbutils.execute_to_string(f"frame {frame_num}")
            self._update_ui()

        self.on_gdb_thread(set_frame)

    @on(udbwidgets.UdbListView.ItemSelected, "#threads")
    @ui_thread_only
    def _thread_selected(self, event: udbwidgets.UdbListView.ItemSelected[int | None]) -> None:
        thread_num = event.value.extra
        # Cannot use execute_to_string because it still prints something to the terminal.
        self.terminal_execute(f"thread {thread_num}")

    @on(udbwidgets.UdbTable.RowSelected, "#bookmarks")
    @ui_thread_only
    def _bookmark_selected(self, event: udbwidgets.UdbTable.RowSelected) -> None:
        bookmarks_table = self.query_one("#bookmarks", udbwidgets.UdbTable)
        cell: _BookmarksCellNameAndCommand = bookmarks_table.get_cell(
            row_key=event.row_key,
            # https://github.com/Textualize/textual/issues/2586.
            column_key=self._BOOKMARKS_NAME_COLUMN,  # type: ignore[arg-type]
        )
        if cell.goto_command is not None:
            self.terminal_execute(cell.goto_command)

    @ui_thread_only
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

    @ui_thread_only
    def progress_hide(self) -> None:
        progress_panel = self.query_one("#progress_panel", containers.Horizontal)
        progress_panel.styles.display = "none"

    @ui_thread_only
    def progress_update(self, total: int) -> None:
        progress_indicator = self.query_one("#progress_indicator", widgets.ProgressBar)
        progress_indicator.update(progress=total)

    @ui_thread_only
    def _action_expand(self) -> None:
        term = self.query_one("#terminal", terminal.Terminal)
        code = self.query_one("#code", udbwidgets.SourceView)

        if term.styles.min_height is None:
            term.styles.min_height = term.outer_size.height + code.outer_size.height - 5
        else:
            term.styles.min_height = None
