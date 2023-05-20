from textual import containers, widgets
from textual.app import App, ComposeResult
from textual.widget import Widget


class UdbToolbarSeparator(Widget):
    DEFAULT_CSS = """
    UdbToolbarSeparator {
        width: 1;
        height: 0;
    }
    """


class UdbToolbar(containers.Horizontal):
    DEFAULT_CSS = """
    UdbToolbar {
        height: auto;
        padding: 0 1;
    }

    UdbToolbar Button {
        border: none;
        padding: 0 1 0 1;
        min-width: 5;
        height: 3;
    }
    App.-dark-mode UdbToolbar Button {
        background: $panel-lighten-1;
    }
    App.-light-mode UdbToolbar Button {
        background: $panel;
    }

    UdbToolbar Button:focus {
        text-style: bold;  /* Overwite "bold reverse" */
        color: $secondary;
    }

    UdbToolbar Button:hover {
        border: none;
    }
    App.-dark-mode UdbToolbar Button:hover {
        background: $panel-lighten-2;
    }
    App.-light-mode UdbToolbar Button:hover {
        background: $panel-darken-1;
    }

    UdbToolbar Button.-active {
        border: none;
        color: $text;
        background: $secondary !important;
    }
    App.-dark-mode UdbToolbar Button.-active {

    }
    App.-light-mode UdbToolbar Button.active {
    }

    UdbToolbar Button.followed-by-button {
        /* The border on the right adds 1 char, so we remove one by reducing the padding.
           Just setting padding-right should do the same but doesn't work correctly. It looks like
           it applies to all sides. */
        padding: 0 0 0 1;
    }
    UdbToolbar .followed-by-button {
        border-right: wide transparent !important;
    }
    """

    def on_mount(self) -> None:
        # The same could be done with the `+` or `~` selectors but textual doesn't support them.
        for curr, nxt in zip(self.children, self.children[1:]):
            match nxt:
                case UdbToolbarSeparator():
                    curr.add_class("followed-by-toolbar-separator")
                case widgets.Button():
                    curr.add_class("followed-by-button")


class MyApp(App):
    BINDINGS = [
        ("ctrl+w", "toggle_dark", "Toggle dark mode"),
    ]

    def compose(self) -> ComposeResult:
        yield widgets.Header()

        with containers.Middle(), containers.Center():
            with UdbToolbar():
                yield widgets.Button("\N{SLICE OF PIZZA} Pizza")
                yield widgets.Button("\N{HAMBURGER} Burger")
                yield widgets.Button("\N{STEAMING BOWL} Noodles")
                yield UdbToolbarSeparator()
                yield widgets.Button("Something")
                yield widgets.Button("Something else")

        yield widgets.Footer()


if __name__ == "__main__":
    MyApp().run()
