from pathlib import Path

from rich.console import RenderableType
from rich.text import Text

from textual.widget import Widget

from src.udbpy import engine  # type: ignore[import]


class StatusBar(Widget):
    COMPONENT_CLASSES = {
        "status-bar--execution-mode--not-running",
        "status-bar--execution-mode--deferred-recording",
        "status-bar--execution-mode--recording",
        "status-bar--execution-mode--replaying-live-process",
        "status-bar--execution-mode--replaying-recording",
        "status-bar--execution-mode--core-file",
    }

    DEFAULT_CSS = """
    StatusBar {
        background: $accent;
        color: $text;
        dock: bottom;
        height: 1;
    }

    StatusBar > .status-bar--execution-mode--not-running,
    StatusBar > .status-bar--execution-mode--deferred-recording,
    StatusBar > .status-bar--execution-mode--recording,
    StatusBar > .status-bar--execution-mode--replaying-live-process,
    StatusBar > .status-bar--execution-mode--replaying-recording,
    StatusBar > .status-bar--execution-mode--core-file {
        text-style: bold;
    }

    StatusBar > .status-bar--execution-mode--not-running {
        background: skyblue;
        color: black;
    }
        
    StatusBar > .status-bar--execution-mode--deferred-recording,
    StatusBar > .status-bar--execution-mode--core-file {
        background: steelblue;
        color: black;
    }

    StatusBar > .status-bar--execution-mode--recording {
        background: maroon;
        color: white;
    }

    StatusBar > .status-bar--execution-mode--replaying-live-process,
    StatusBar > .status-bar--execution-mode--replaying-recording {
        background: limegreen;
        color: black;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="status-bar")

        self._execution_mode = engine.ExecutionMode.NOT_RUNNING
        self._target_name: str | None = None
        self._time: engine.Time | None = None
        self._time_extent:engine.LogExtent|None=None
        self._source_path: Path | None = None

        self._content: Text | None = None

    def update(
        self,
        execution_mode: engine.ExecutionMode,
        target_name: str | None,
        time: engine.Time | None,
        time_extent: engine.LogExtent | None,
        source_path: Path | None,
    ) -> None:
        assert (time is None) == (time_extent is None)

        self._execution_mode = execution_mode
        self._target_name = target_name
        self._time = time
        self._time_extent = time_extent
        self._source_path = source_path

        self._content = None
        self.refresh()

    def notify_style_update(self) -> None:
        self._content = None

    def render(self) -> RenderableType:
        if self._content is None:
            self._content = self._generate_content()
        return self._content

    def _generate_content(self) -> Text:
        text = Text(
            style=self.rich_style,
            no_wrap=True,
            justify="left",
            end="",
            overflow="ellipsis",
        )

        msg = self._execution_mode.value.status_prompt_message
        if self._target_name:
            msg = f"{msg}: {self._target_name}"
        execution_mode_component_name = self._execution_mode.name.lower().replace("_", "-")
        text.append(
            f" {msg} ",
            self.get_component_rich_style(
                f"status-bar--execution-mode--{execution_mode_component_name}"
            ),
        )

        # Time and extent are both None or not None (see update), but mypy cannot know.
        if self._time is not None and self._time_extent is not None:
            # Logic coped from Prompt._get_history_progress in udb_prompt.py.
            bbs_total = self._time_extent.end - self._time_extent.start
            bbs_from_start = self._time.bbcount - self._time_extent.start

            # Whether we are at the end is checked before whether we are at the start because, if
            # there is a single BB and we are recording it would be confusing to show "start".
            if bbs_from_start == bbs_total:
                msg = "end of history"
            elif bbs_from_start == 0:
                msg = "start of history"
            else:
                # Rounding down means that that we never show 100% if not at the very last BB, which
                # seems like a good UX.
                percentage = bbs_from_start * 100 // bbs_total
                msg = f"{percentage}% through history"
            text.append(f" at time {self._time} ({msg})")

        if self._source_path is not None:
            text.append(f" in {self._source_path.name}")

        return text
