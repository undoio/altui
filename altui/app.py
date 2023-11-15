import contextlib
import dataclasses
import functools
import itertools
import operator
import os
from pathlib import Path
from typing import Any, Iterator, TypeVar

import gdb  # type: ignore[import]
import rich.markup
from rich.markdown import Markdown
from rich.text import Text
from src.udbpy import comms, engine, textutil  # type: ignore[import]
from src.udbpy.gdb_extensions import gdbutils, udb_base  # type: ignore[import]
from textual import containers, on, widgets
from textual.app import ComposeResult

from . import status_bar, terminal, udbwidgets
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


@dataclasses.dataclass(frozen=True, order=True)
class Variable:
    name: str
    value: str

    # TODO: add more and consider using enumerations.
    is_argument: bool

    def __str__(self) -> str:
        return self.to_string(compact=False)

    def to_string(self, *, compact: bool) -> str:
        space_around_assignement = "" if compact else " "
        return f"{self.name}{space_around_assignement}={space_around_assignement}{self.value}"


@dataclasses.dataclass(frozen=True)
class SourceLocation:
    path: Path
    short_path: Path
    line: int

    def __str__(self) -> str:
        return f"{self.path}, line {self.line}"


@dataclasses.dataclass(frozen=True)
class CalledFunction:
    # TODO: is it always a function? or could this represent fake frames?

    level: int
    name: str
    arguments: list[Variable]
    is_selected: bool
    source: SourceLocation | None

    def __str__(self) -> str:
        formatted_args = ", ".join(arg.to_string(compact=True) for arg in self.arguments)
        return f"{self.name} ({formatted_args})"

    def source_to_string(self) -> str:
        if self.source is None:
            return "No source information"
        return str(self.source)


@dataclasses.dataclass(frozen=True)
class Thread:
    num: int
    thread_name: str | None
    pid: int
    tid: int

    is_selected: bool

    function: CalledFunction

    @functools.cached_property
    def name(self) -> str:
        # Matches what `info thread` shows in the `Target Id` column, minus the "Thread " prefix
        # which is not useful.
        name = f"{self.pid}.{self.tid}"
        if self.thread_name is not None:
            name = f'{name} "{self.thread_name}"'
        return name

    # TODO: Consider adding more state, for instance stopped/running/exited.


def iter_function_blocks(frame: gdb.Frame) -> Iterator[gdb.Block]:
    try:
        block = frame.block()
    except RuntimeError:
        # Instead of returning `None`, GDB raises a "Cannot locate block for frame." `RuntimeError`.
        block = None

    while block is not None:
        yield block
        if block.function is not None:
            # block is the top-level function block so we don't need to go further.
            break
        block = block.superblock


def function_variables(frame: gdb.Frame) -> Iterator[Variable]:
    for block in iter_function_blocks(frame):
        for gdb_symbol in block:
            assert gdb_symbol.print_name is not None  # TODO: superfluous?
            if gdb_symbol.print_name != "__PRETTY_FUNCTION__":
                value = gdb_symbol.value(frame)
                assert (
                    value is not None
                ), f"None value for {gdb_symbol.print_name!r}"  # TODO: superfluous?
                yield Variable(
                    name=gdb_symbol.print_name,
                    value=str(value),
                    is_argument=gdb_symbol.is_argument,
                )


def function(frame: gdb.Frame) -> CalledFunction:
    sal = frame.find_sal()
    if sal.symtab is not None:
        source = SourceLocation(
            path=Path(sal.symtab.fullname()),
            short_path=Path(sal.symtab.filename),
            line=sal.line,
        )
    else:
        source = None

    return CalledFunction(
        level=frame.level(),
        name=frame.name() or "???",
        arguments=[s for s in function_variables(frame) if s.is_argument],
        is_selected=(frame == gdb.selected_frame()),
        source=source,
    )


def stack() -> Iterator[CalledFunction]:
    try:
        gdbutils.ensure_running()
    except comms.WrongExecutionModeError:
        return

    frame = gdb.newest_frame()
    while frame is not None:
        yield function(frame)
        frame = frame.older()


def threads() -> Iterator[Thread]:
    selected_gdb_thread = gdb.selected_thread()
    inf = gdb.selected_inferior()
    try:
        for gdb_thread in inf.threads():
            gdb_thread.switch()
            pid, tid, _ = gdb_thread.ptid
            yield Thread(
                num=gdb_thread.num,
                thread_name=gdb_thread.name,
                pid=pid,
                tid=tid,
                is_selected=(gdb_thread == selected_gdb_thread),
                function=function(gdb.selected_frame()),
            )
    finally:
        if selected_gdb_thread is not None:
            selected_gdb_thread.switch()


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

    .dock-right {
        dock: right;
    }

    #continue-last {
        display: none;
        height: auto;
        padding: 0 1;
    }

    #continue-last UdbToolbar {
        padding: 1 0 0 0;
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
                        with udbwidgets.UdbToolbar(id="variables-toolbar"):
                            yield widgets.Button(
                                "\N{BLACK LEFT-POINTING TRIANGLE} last", id="last-backward"
                            )
                            yield widgets.Button(
                                "\N{BLACK RIGHT-POINTING TRIANGLE}", id="last-forward"
                            )

                        with containers.Vertical(id="continue-last", classes="main-window-panel"):
                            yield widgets.Static(id="continue-last-text")
                            yield widgets.Static(id="continue-last-expression")
                            with udbwidgets.UdbToolbar():
                                yield widgets.Button(
                                    "\N{BLACK LEFT-POINTING TRIANGLE} Backward",
                                    id="continue-last-backward",
                                )
                                # FIXME: Add a Forward label when we have a way of dealing with
                                # not enough horizontal space.
                                yield widgets.Button(
                                    "\N{BLACK RIGHT-POINTING TRIANGLE}",
                                    id="continue-last-forward",
                                )
                                yield widgets.Button(
                                    "\N{MULTIPLICATION X} Cancel",
                                    id="continue-last-cancel",
                                    classes="dock-right",
                                )

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
        for widget in self.query(".disable-on-execution"):
            widget.disabled = not enabled

    def _update_ui(self) -> None:
        self.on_gdb_thread(self._update_ui_callback)

    @gdb_thread_only
    def _update_ui_callback(self) -> None:
        if self.get_instance() is not self:
            return

        if gdbutils.is_tui_enabled():
            gdb.execute("tui disable")

        target_name = None
        selected_frame = None
        time_extent = None
        current_time = None
        bookmarks = []
        time_next_undo = None
        time_next_redo = None
        with contextlib.suppress(comms.WrongExecutionModeError):
            selected_inferior = self._udb.inferiors.selected
            if selected_inferior.recording is not None:
                target_name = selected_inferior.recording.name
            else:
                filename = selected_inferior.gdb_inferior.progspace.filename
                if filename is not None:
                    target_name = Path(filename).name

            selected_frame = gdbutils.selected_frame()

            current_time = self._udb.time.get()
            time_extent = self._udb.get_event_log_extent()
            bookmarks = list(self._udb.bookmarks.iter_bookmarks())

            with contextlib.suppress(StopIteration):
                time_next_undo = next(self._udb.time.undo_items)
            with contextlib.suppress(StopIteration):
                time_next_redo = next(self._udb.time.redo_items)

        self.on_ui_thread(
            self._set_ui_to_values,
            stack=list(stack()),
            threads=list(threads()),
            variables=(
                sorted(function_variables(selected_frame)) if selected_frame is not None else []
            ),
            execution_mode=self._udb.get_execution_mode(),
            current_time=current_time,
            time_extent=time_extent,
            target_name=target_name,
            bookmarks=bookmarks,
            time_next_undo=time_next_undo,
            time_next_redo=time_next_redo,
            last_search=self._udb.last._latest_search,  # pylint: disable=protected-access
        )


    @ui_thread_only
    def _set_ui_to_values(
        self,
        stack: list[CalledFunction],
        threads: list[Thread],
        variables: list[Variable],
        execution_mode: engine.ExecutionMode,
        current_time: engine.Time | None,
        time_extent: engine.LogExtent | None,
        target_name: str | None,
        bookmarks: list[tuple[str, engine.Time]],
        time_next_undo: engine.Time | None,
        time_next_redo: engine.Time | None,
        last_search: Any,
    ) -> None:
        bt_lv: udbwidgets.UdbListView[CalledFunction] = self.query_one(
            "#backtrace", udbwidgets.UdbListView
        )
        bt_lv.clear()
        curr_function: CalledFunction | None = None
        for i, f in enumerate(stack):
            bt_lv.append(str(f), f.source_to_string(), extra=f)
            if f.is_selected:
                assert curr_function is None, (
                    f"Two functions appear to be the current function: "
                    f"{curr_function} ({curr_function.source_to_string()}) and "
                    f"{f} ({f.source_to_string()})"
                )
                curr_function = f
                bt_lv.move_cursor(row=i)

        code = self.query_one("#code", udbwidgets.SourceView)
        source_path = None
        if curr_function is not None and curr_function.source is not None:
            code.path = source_path = curr_function.source.path
            code.current_line = curr_function.source.line
            code.border_title = str(curr_function.source.short_path)
        else:
            code.path = None
            code.current_line = None
            code.border_title = None

        threads_lv: udbwidgets.UdbListView[Thread] = self.query_one(
            "#threads", udbwidgets.UdbListView
        )
        threads_lv.clear()
        for i, thread in enumerate(threads):
            thread_label = f"[{thread.num}] "
            indent = " " * len(thread_label)
            # TODO: is the name correct/useful? If not, consider making one from the PID/TID like
            # GDB does (`Thread 3088776.3088776`).
            threads_lv.append(
                f"{thread_label}{thread.name or ''}".rstrip(),
                (f"{indent}{thread.function}\n" f"{indent}{thread.function.source}"),
                extra=thread,
            )
            if thread.is_selected:
                threads_lv.move_cursor(row=i)

        vars_lv: udbwidgets.UdbListView[Variable] = self.query_one(
            "#variables", udbwidgets.UdbListView
        )
        vars_lv.clear()
        for v in variables:
            vars_lv.append(str(v), extra=v)

        # If there is any variable then one must be selected.
        self.query_one("#variables-toolbar", udbwidgets.UdbToolbar).disabled = (
            vars_lv.row_count == 0
        )

        if last_search is not None:
            self.query_one("#continue-last-text", widgets.Static).update(
                "Continue search for value changes "
                + ("without re-evaluating:" if last_search.addr_range is not None else "to:")
            )
            self.query_one("#continue-last-expression", widgets.Static).update(
                Text(f"  {last_search.expression}", no_wrap=True, overflow="ellipsis")
            )
            for btn_id in "last-backward", "last-forward":
                if self.query_one(f"#{btn_id}", widgets.Button).has_focus:
                    self.query_one(f"#continue-{btn_id}", widgets.Button).focus()
                    break

        self.query_one("#continue-last", containers.Vertical).styles.display = (
            "block" if last_search is not None else "none"
        )

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
    def _backtrace_selected(
        self, event: udbwidgets.UdbListView.ItemSelected[CalledFunction]
    ) -> None:
        func = event.value.extra

        def set_frame() -> None:
            assert func is not None  # Guaranteed by the check below, but mypy doesn't know.

            gdbutils.execute_to_string(f"frame {func.level}")
            self._update_ui()

        if func is not None:
            self.on_gdb_thread(set_frame)

    @on(udbwidgets.UdbListView.ItemSelected, "#threads")
    @ui_thread_only
    def _thread_selected(self, event: udbwidgets.UdbListView.ItemSelected[Thread]) -> None:
        thread = event.value.extra
        if thread is not None:
            # Cannot use execute_to_string because it still prints something to the terminal.
            self.terminal_execute(f"thread {thread.num}")

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

    @on(widgets.Button.Pressed, "#last-backward")
    @on(widgets.Button.Pressed, "#last-forward")
    @on(widgets.Button.Pressed, "#continue-last-backward")
    @on(widgets.Button.Pressed, "#continue-last-forward")
    @ui_thread_only
    def _last_pressed(self, event: widgets.Button.Pressed):
        cmd = ["last"]
        if event.button.id is not None and "forward" in event.button.id:
            cmd.append("-f")

        if event.button.id is not None and "continue" not in event.button.id:
            table: udbwidgets.UdbListView[Variable] = self.query_one(
                "#variables",
                udbwidgets.UdbListView,
            )
            cell = table.get_cell_at(table.cursor_coordinate)
            var = cell.extra
            assert (
                var is not None
            ), f"Button {event.button.id!r} is not disabled even if there's no selected variable"
            cmd.append(var.name)

        self.terminal_execute(" ".join(cmd))

    @on(widgets.Button.Pressed, "#continue-last-cancel")
    @ui_thread_only
    def _last_cancel_pressed(self, event: widgets.Button.Pressed):
        self._udb.last._latest_search = None  # pylint: disable=protected-access
        self.query_one("#continue-last", containers.Vertical).styles.display = "none"

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
