from __future__ import annotations

import dataclasses
from typing import Callable, Generic, TypeVar, cast

import rich.repr
from rich.text import Text
from textual import containers, events, on, widgets
from textual.app import App, ComposeResult
from textual.message import Message
from typing_extensions import Self

_T = TypeVar("_T")
_U = TypeVar("_U")


def _not_implemented(use_instead: str | None = None) -> Callable:
    def wrapper(self, *args, **kwargs) -> None:
        msg = f"{type(self).__qualname__} doesn't implement this method"
        if use_instead is not None:
            msg = f"{msg}, use {use_instead} instead"
        raise NotImplementedError(msg)

    return wrapper


@dataclasses.dataclass
class UdbListViewCellType(Generic[_T]):
    rendered: Text
    primary: str
    secondary: str | None
    extra: _T | None = None

    def __rich__(self) -> Text:
        return self.rendered


class UdbListView(widgets.DataTable[UdbListViewCellType[_T]], Generic[_T]):
    COMPONENT_CLASSES = {
        "rich-list-view--text-primary",
        "rich-list-view--text-secondary",
    }

    DEFAULT_CSS = """
    UdbListView {
        /* Otherwise it uses as little vertical space as possible. */
        height: 1fr;
    }

    UdbListView > .datatable--cursor {
        background: $primary;
        color: $text;
    }

    UdbListView > .datatable--odd-row {
        background: $surface !important;
    }

    App.-dark-mode UdbListView > .datatable--even-row {
        background: $surface-lighten-2 !important;
    }
    App.-light-mode UdbListView > .datatable--even-row {
        background: $surface-darken-2 !important;
    }

    UdbListView > .datatable--hover {
        background: $primary 30%;
    }

    App.-dark-mode UdbListView > .rich-list-view--text-primary {
        color: #f0f0f0; /* $text */
    }
    App.-light-mode UdbListView > .rich-list-view--text-primary {
        color: #616161; /* $text */
    }

    App.-dark-mode UdbListView > .rich-list-view--text-secondary {
        color: #cccccc; /* $text-muted */
    }
    App.-light-mode UdbListView > .rich-list-view--text-secondary {
        color: #a3a3a3; /* $text-muted */
    }
    """

    add_column = _not_implemented()
    add_columns = _not_implemented()
    add_row = _not_implemented("append")
    add_rows = _not_implemented("append")
    remove_row = _not_implemented()

    class ItemSelected(Message, Generic[_U], bubble=True):
        def __init__(
            self,
            cell_selected_message: UdbListView.CellSelected,
        ) -> None:
            self.cell_selected_message = cell_selected_message

            super().__init__()

        def __rich_repr__(self) -> rich.repr.Result:
            yield from self.cell_selected_message.__rich_repr__()

        @property
        def list_view(self) -> UdbListView[_U]:
            return cast(UdbListView[_U], self.cell_selected_message.data_table)

        @property
        def control(self) -> UdbListView[_U]:  # type: ignore[override]
            return self.list_view

        @property
        def value(self) -> UdbListViewCellType[_U]:
            return cast(UdbListViewCellType[_U], self.cell_selected_message.value)

    def __init__(
        self,
        *,
        zebra_stripes: bool = True,
        name: str | None = None,
        id: str | None = None,  # pylint: disable=redefined-builtin
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        self._max_row_width = 0
        self._content: list[UdbListViewCellType] = []

        super().__init__(
            show_header=False,
            show_row_labels=False,
            zebra_stripes=zebra_stripes,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )

        super()._set_hover_cursor(False)
        super().add_column("Content", width=0)  # Title doesn't matter as it's hidden.

        self.watch(self.app, "dark", self._on_dark_change, init=False)

    def _set_hover_cursor(self, active: bool) -> None:
        pass

    def _update_content_width(self) -> None:
        next(iter(self.columns.values())).width = max(
            self.scrollable_content_region.width - 2,
            self._max_row_width,
        )

    def _watch_virtual_region(self) -> None:
        self._update_content_width()

    def _watch_show_vertical_scrollbar(self) -> None:
        self._update_content_width()

    def _on_resize(self, event: events.Resize) -> None:
        # The event is needed to avoid pylint warning that the base method is different.
        self._update_content_width()

    def _on_dark_change(self) -> None:
        orig_cursor = self.cursor_coordinate
        orig_content = self._content
        self.clear()
        for cell in orig_content:
            self.append(cell.primary, cell.secondary, cell.extra)
        self.move_cursor(row=orig_cursor.row, column=orig_cursor.column)

    def _on_data_table_cell_selected(self, event: widgets.DataTable.CellSelected) -> None:
        self.post_message(UdbListView.ItemSelected(event))

    def clear(self, columns: bool = False) -> Self:
        if columns:
            raise ValueError(f"{type(self).__qualname__} doesn't support removing columns")
        super().clear(columns=False)
        self._content = []
        self._max_row_width = 0
        self._update_content_width()
        return self

    def append(self, primary: str, secondary: str | None = None, extra: _T | None = None) -> Self:
        cell = self._append_real(primary, secondary, extra)
        self._content.append(cell)
        return self

    def _append_real(
        self,
        primary: str,
        secondary: str | None = None,
        extra: _T | None = None,
    ) -> UdbListViewCellType[_T]:
        text = Text(
            primary,
            self.get_component_rich_style("rich-list-view--text-primary", partial=True),
            no_wrap=True,
        )
        if secondary is not None:
            text.append("\n")
            text.append(
                secondary,
                self.get_component_rich_style("rich-list-view--text-secondary", partial=True),
            )

        value = UdbListViewCellType[_T](
            rendered=text,
            primary=primary,
            secondary=secondary,
            extra=extra,
        )
        super().add_row(
            value,
            height=len(text.split()),  # How many lines?
        )

        # cell_len on the whole Text counts the printable characters on all the lines.
        text_width = max(t.cell_len for t in text.split())
        self._max_row_width = max(self._max_row_width, text_width)
        self._update_content_width()

        return value


class MyApp(App):
    BINDINGS = [
        ("ctrl+w", "toggle_dark", "Toggle dark mode"),
    ]

    DEFAULT_CSS = """
    UdbListView {
        border: round $accent;
    }
    UdbListView:focus {
        border: round $secondary;
    }
    """

    def compose(self) -> ComposeResult:
        yield widgets.Header()
        with containers.Vertical():
            yield widgets.Button("Add item", id="add")
            yield UdbListView[int](id="first")
            yield UdbListView[int](id="second")
        yield widgets.Footer()

    def on_ready(self) -> None:
        for id_ in ("first", "second"):
            list_view = self.query_one(f"#{id_}", UdbListView)
            list_view.append("Hello world!", extra=100)
            list_view.append("Something blah blah blah", "Something else less important", 101)
            list_view.append("Something blah blah blah", "Something else less important", 102)
            list_view.append("Foo", "Bar")

    @on(UdbListView.ItemSelected, "#second")
    def on_selected(self, event: UdbListView.ItemSelected) -> None:
        if event.value.extra is not None:
            list_view = self.query_one(f"#first", UdbListView)
            list_view.append(f"extra: {event.value.extra}", extra=event.value.extra)

    @on(widgets.Button.Pressed, "#add")
    def on_clicked(self, event: widgets.Button.Pressed) -> None:
        list_view = self.query_one(f"#first", UdbListView)
        list_view.append("From a click")


if __name__ == "__main__":
    MyApp().run()
