from __future__ import annotations

import contextlib
import functools
from pathlib import Path

import rich.measure
import rich.segment
import rich.style
import rich.syntax
import rich.text
from rich.text import Text
from textual import widgets
from textual.app import App, ComposeResult
from textual.reactive import reactive
from textual.strip import Strip
from typing_extensions import Self


class _SourceOnlyStripsView:
    def __init__(self, source_view: SourceView) -> None:
        self._source_view = source_view

    def _source_to_real_index(self, line: int) -> int:
        return line - 1

    def __getitem__(self, line: int) -> Strip:
        return self._source_view.lines[self._source_to_real_index(line)]

    def __setitem__(self, line: int, value: Strip) -> None:
        self._source_view.lines[self._source_to_real_index(line)] = value

    def __len__(self) -> int:
        return self._source_to_real_index(len(self._source_view.lines))


class SourceView(widgets.TextLog):
    COMPONENT_CLASSES = {
        "source-view--current-line",
    }

    DEFAULT_CSS = """
    SourceView {
        /* Horizontal scrolling is broken. */
        overflow-x: auto;
    }

    SourceView .source-view--current-line {
        background: $secondary-background;
    }
    """

    path: reactive[Path | None] = reactive(None)
    current_line: reactive[int | None] = reactive(None)
    placeholder: reactive[str | None] = reactive(None)

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,  # pylint: disable=redefined-builtin
        classes: str | None = None,
    ) -> None:
        self._restorable_source_strips: dict[int, Strip] = {}

        super().__init__(name=name, id=id, classes=classes, auto_scroll=False)

    @functools.cached_property
    def _source_strips(self) -> _SourceOnlyStripsView:
        return _SourceOnlyStripsView(self)

    def clear(self) -> Self:
        self._restorable_source_strips.clear()
        return super().clear()

    def watch_path(self, old: Path | None, new: Path | None) -> None:
        old = old.resolve() if old is not None else None
        new = new.resolve() if new is not None else None

        if new == old:
            return

        self.placeholder = None
        self.clear()

        if new is None:
            return

        try:
            try:
                content = new.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = new.read_text(encoding="ascii", errors="backslashreplace")
        except FileNotFoundError:
            self.placeholder = (
                "File not found (in a future version we may show the assembly code instead)"
            )
            return
        except OSError as exc:
            self.placeholder = f"Cannot load file: {exc}"
            return

        lexer = rich.syntax.Syntax.guess_lexer(str(new), content)
        syntax = rich.syntax.Syntax(
            content,
            lexer,
            line_numbers=True,
            theme="ansi_dark",
        )
        self.write(syntax)

    def validate_current_line(self, line: int | None) -> int | None:
        if line is None or self.path is None or len(self._source_strips) == 0:
            return None
        else:
            return max(
                min(line, len(self._source_strips)),
                1,
            )

    def _restore_line_style(self, line: int) -> None:
        with contextlib.suppress(IndexError, KeyError):
            self._source_strips[line] = self._restorable_source_strips[line]

    def _style_line(self, line: int, modifier: rich.style.Style) -> None:
        self._restore_line_style(line)

        original_strip = self._source_strips[line]
        self._restorable_source_strips[line] = original_strip

        segments = [
            rich.segment.Segment(
                segment.text,
                rich.style.Style.chain(
                    segment.style or rich.style.Style(),
                    modifier,
                ),
                segment.control,
            )
            for segment in original_strip
        ]

        # FIXME: this is almost for sure the wrong way of doing it.
        segments.append(
            rich.segment.Segment(
                " " * (self.scrollable_content_region.width - original_strip.cell_length),
                modifier,
            )
        )

        self._source_strips[line] = Strip(segments)
        self._line_cache.clear()

    def watch_current_line(self, old: int | None, new: int | None) -> None:
        # Clear.
        if old is not None:
            self._restore_line_style(old)

        if new is None:
            return

        # Highlight the new line.
        self._style_line(new, self.get_component_rich_style("source-view--current-line"))

        # Scroll to make the new line visible (if necessary).
        first_visible_line = self.scroll_y
        last_visible_line = first_visible_line + self.scrollable_content_region.height

        center_area_margin = self.scrollable_content_region.height // 4
        center_first_line = first_visible_line + center_area_margin
        center_last_line = last_visible_line - center_area_margin

        if center_first_line <= new <= center_last_line:
            return

        if first_visible_line <= new <= last_visible_line:
            scroll_x = None
        else:
            scroll_x = 0

        scroll_y = new - center_area_margin
        self.scroll_to(scroll_x, scroll_y, animate=False)

        self.refresh()

    def watch_placeholder(self, new: str | None) -> None:
        self.path = None
        self.current_line = None

        self.clear()
        if new is not None:
            self.write(Text(f"\n\n\n  {new}", "bold"))


class MyApp(App):
    BINDINGS = [
        ("k", "up", "Move up"),
        ("j", "down", "Move down"),
        ("s", "switch", "Switch file"),
    ]

    FILES = [
        (__file__, 10),
        ("../demo.c", 100),
        ("/this/doesnt/exists.c", 12),
    ]

    def __init__(self, *args, **kwargs) -> None:
        self._current_file_index = -1

        super().__init__(*args, **kwargs)

    def compose(self) -> ComposeResult:
        yield widgets.Header()
        yield SourceView()
        yield widgets.Footer()

    def on_ready(self) -> None:
        self.action_switch()

    def move_current_line(self, offset: int) -> None:
        source_view = self.query_one(SourceView)
        if source_view.current_line is not None:
            source_view.current_line += offset

    def action_up(self) -> None:
        self.move_current_line(-1)

    def action_down(self) -> None:
        self.move_current_line(1)

    def action_switch(self) -> None:
        self._current_file_index = (self._current_file_index + 1) % len(self.FILES)
        file, line = self.FILES[self._current_file_index]

        source_view = self.query_one(SourceView)
        source_view.path = Path(file)
        source_view.current_line = line


if __name__ == "__main__":
    MyApp().run()
