from __future__ import annotations

from typing import Generic, Literal, TypeVar

from textual import containers, on, widgets
from textual.app import App, ComposeResult

_T = TypeVar("_T")

CursorType = Literal["cell", "row", "column", "none"]


class UdbTable(widgets.DataTable[_T], Generic[_T]):
    DEFAULT_CSS = """
    UdbTable {
        /* Otherwise it uses as little vertical space as possible. */
        height: 1fr;
    }

    UdbTable > .datatable--odd-row {
        background: $surface !important;
    }

    App.-dark-mode UdbTable > .datatable--even-row {
        background: $surface-lighten-2 !important;
    }
    App.-light-mode UdbTable > .datatable--even-row {
        background: $surface-darken-1 !important;
    }
    """

    def __init__(
        self,
        *,
        cursor_type: CursorType = "row",
        show_header: bool = True,
        show_row_labels: bool = True,
        zebra_stripes: bool = True,
        name: str | None = None,
        id: str | None = None,  # pylint: disable=redefined-builtin
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            show_header=show_header,
            show_row_labels=show_row_labels,
            zebra_stripes=zebra_stripes,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )

        self.cursor_type = cursor_type
        super()._set_hover_cursor(False)

    def _set_hover_cursor(self, active: bool) -> None:
        # Prevent hover from highlighting cells/rows.
        pass



class MyApp(App):
    BINDINGS = [
        ("ctrl+w", "toggle_dark", "Toggle dark mode"),
    ]

    DEFAULT_CSS = """
    UdbTable {
        border: round $accent;
    }
    UdbTable:focus {
        border: round $secondary;
    }
    """

    def compose(self) -> ComposeResult:
        yield widgets.Header()
        with containers.Vertical():
            yield UdbTable[int](id="first")
            yield UdbTable[int](id="second")
        yield widgets.Footer()

    def on_ready(self) -> None:
        for id_ in ("first", "second"):
            table = self.query_one(f"#{id_}", UdbTable)
            table.add_columns("", "Name", "Time")
            table.add_rows(
                (
                    ("", "foo", "    1,234:0xABC"),
                    ("\N{BLACK RIGHT-POINTING TRIANGLE}", "bar", "  100,234:0xABC"),
                    ("", "baz", "1,234,678:0xABC"),
                )
            )

    @on(UdbTable.RowSelected, "#second")
    def on_selected(self, event: UdbTable.RowSelected) -> None:
        table = self.query_one("#first", UdbTable)
        table.add_row("", "X", "0")


if __name__ == "__main__":
    MyApp().run()
