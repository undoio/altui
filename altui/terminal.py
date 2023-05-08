from __future__ import annotations

import asyncio
import dataclasses
import fcntl
import functools
import itertools
import os
import pty
import re
import shlex
import signal
import struct
import sys
import termios
from typing import Any, Callable, Iterator, Mapping

import pyte  # type: ignore
import pyte.modes  # type: ignore
import pyte.screens  # type: ignore
from rich.color import Color
from rich.style import Style
from rich.text import Text
from textual import events, widgets
from textual.app import App, ComposeResult
from textual.geometry import Offset, Region, Size
from textual.scroll_view import ScrollView
from textual.strip import Strip

# See https://invisible-island.net/xterm/ctlseqs/ctlseqs.html.
_ESC = "\N{ESCAPE}"
_CSI = f"{_ESC}["
_SS3 = f"{_ESC}O"
_KEY_TO_ANSI = {
    # "PC-Style Function Keys" section.
    "up": f"{_SS3}A",
    "down": f"{_SS3}B",
    "right": f"{_SS3}C",
    "left": f"{_SS3}D",
    "home": f"{_SS3}H",
    "end": f"{_SS3}F",
    "F1": f"{_SS3}P",
    "F2": f"{_SS3}Q",
    "F3": f"{_SS3}R",
    "F4": f"{_SS3}S",
    "F5": f"{_CSI}15~",
    "F6": f"{_CSI}17~",
    "F7": f"{_CSI}18~",
    "F8": f"{_CSI}19~",
    "F9": f"{_CSI}20~",
    "F10": f"{_CSI}21~",
    "F11": f"{_CSI}23~",
    "F12": f"{_CSI}24~",
    # "VT220-Style Function Keys" section.
    "delete": f"{_CSI}3~",
    "pageup": f"{_CSI}5~",
    "pagedown": f"{_CSI}6~",
}


@dataclasses.dataclass(frozen=True)
class _CharStyle:
    fg_pyte_color: str
    bg_pyte_color: str
    bold: bool
    italics: bool
    underscore: bool
    strikethrough: bool
    reverse: bool

    @classmethod
    @functools.cache
    def unstyled(cls) -> _CharStyle:
        return _Char.from_pyte_char(pyte.screens.Char(" ")).style

    @functools.lru_cache(maxsize=256)
    def rich_style(self, default_rich_style: Style) -> Style:
        return Style(
            color=self._convert_color(self.fg_pyte_color, default_rich_style.color),
            bgcolor=self._convert_color(self.bg_pyte_color, default_rich_style.bgcolor),
            italic=self.italics,
            underline=self.underscore,
            strike=self.strikethrough,
            reverse=self.reverse,
        )

    @staticmethod
    def _convert_color(pyte_color: str, default_color: Color | None) -> Color | str | None:
        if pyte_color == "default":
            return default_color
        if pyte_color == "brown":
            # Pyte uses "brown" to mean "yellow", see `graphics.py` in the Pyte repository.
            return "yellow"
        if re.fullmatch("[0-9a-f]{6}", pyte_color, re.IGNORECASE):
            return "#" + pyte_color
        return pyte_color


@dataclasses.dataclass(frozen=True)
class _Char:
    data: str
    style: _CharStyle

    @classmethod
    def from_pyte_char(cls, pyte_char: pyte.screens.Char) -> _Char:
        assert pyte_char.data is not None
        return cls(
            data=pyte_char.data,
            style=_CharStyle(
                fg_pyte_color=pyte_char.fg,
                bg_pyte_color=pyte_char.bg,
                bold=pyte_char.bold,
                italics=pyte_char.italics,
                underscore=pyte_char.underscore,
                strikethrough=pyte_char.strikethrough,
                reverse=pyte_char.reverse,
            ),
        )


_ScreenBufferLine = Mapping[int, pyte.screens.Char]


class _Screen(pyte.HistoryScreen):
    buffer: Mapping[int, _ScreenBufferLine]

    def __init__(self, columns: int = 80, lines: int = 24, history: int = 1_000) -> None:
        super().__init__(columns=columns, lines=lines, history=history)

        for event_name in pyte.Stream.events:
            original_event_func: Callable[..., Any] = getattr(self, event_name)

            def wrapped_event_func(
                *args: Any,
                _event_name: str = event_name,
                _original_event_func: Callable[..., Any] = original_event_func,
                **kwargs: Any,
            ) -> Any:
                result = _original_event_func(*args, **kwargs)
                return result

            setattr(self, event_name, wrapped_event_func)

    @property
    def virtual_lines(self) -> int:
        return len(self.history.top) + self.lines + len(self.history.bottom)

    def content_at_virtual_line(self, n: int) -> _ScreenBufferLine:
        if n < 0:
            raise IndexError(f"Negative indices not supported: {n}")

        for d in self.history.top, self.buffer, self.history.bottom:
            if n < len(d):
                return d[n]
            n -= len(d)

        return {}

    @property
    def cursor_virtual_position(self) -> Offset:
        return Offset(
            x=self.cursor.x,
            y=self.cursor.y + len(self.history.top),
        )

    @property
    def virtual_dirty(self) -> Iterator[int]:
        for dirty in self.dirty:
            yield len(self.history.top) + dirty

    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        dropped_lines = self.lines - lines
        if dropped_lines > 0:
            # FIXME: deal with too many lines in history
            self.history.top.extend(self.buffer[i] for i in range(dropped_lines))

        super().resize(lines, columns)

    def reverse_index(self) -> None:
        assert False, "reverse_index not supported yet"

    def prev_page(self) -> None:
        raise NotImplementedError()

    def next_page(self):
        raise NotImplementedError()


class Terminal(ScrollView, can_focus=True):
    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,  # pylint: disable=redefined-builtin
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)

        self._fd: int | None = None
        self._pid: int | None = None
        # self._cached_rendered_lines = LRUCache(1024)
        self._screen = _Screen(80, 24)  # FIXME: Update size
        self._stream = pyte.Stream(self._screen)

    def _on_styles_updated(self) -> None:
        # Who calls this?
        # self._cached_rendered_lines.clear()
        pass

    def on_resize(self, event: events.Resize) -> None:
        self._update_sizes()

    def _update_sizes(self, force: bool = False) -> None:
        width = self.scrollable_content_region.width
        height = self.scrollable_content_region.height

        if (width, height) == (0, 0):
            # This seems to happen multiple times during shutdown, but it's then rejected by
            # the screen so it happend again and again.
            return

        if force or (width, height) != (self._screen.columns, self._screen.lines):
            # The widget's internal size changed.
            self._screen.resize(columns=width, lines=height)
            self.refresh()

            if self._fd is not None:
                fcntl.ioctl(
                    self._fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", height, width, 0, 0),
                )
                if self._pid is not None:
                    os.kill(self._pid, signal.SIGWINCH)

        self.virtual_size = Size(self._screen.columns, self._screen.virtual_lines)

    def attach_to_tty(self, fd: int, pid: int | None = None, *, read_from_tty: bool = True) -> None:
        if not os.isatty(fd):
            raise ValueError(f"File descriptor {fd} is not a TTY")

        if self._fd is not None:
            raise ValueError("Cannot attach to TTY {fd} as already attached to {self._fd}")

        self._fd = fd
        self._pid = pid

        self._screen.reset()
        self._update_sizes(force=True)

        if read_from_tty:
            loop = asyncio.get_running_loop()
            loop.add_reader(self._fd, self._on_output)

    def _on_output(self) -> None:
        if self._fd is None:
            return

        buff = os.read(self._fd, 4096).decode("utf-8", errors="backslashreplace")
        self.process_output(buff)

    def process_output(self, buff: str) -> None:
        self._stream.feed(buff)

        self._update_sizes()
        self.scroll_end(animate=False)
        for line in self._screen.virtual_dirty:
            self.refresh(
                Region(
                    x=0,
                    y=line - self.scroll_offset.y,
                    width=self.size.width,
                    height=1,
                )
            )

    def render_line(self, y: int) -> Strip:
        # FIXME: do some caching?
        # FIXME: consider scroll_x
        _scroll_x, scroll_y = self.scroll_offset
        y += scroll_y

        rich_text = Text.assemble(*self._styled_chunks(y), no_wrap=False)
        if not rich_text.cell_len:
            return Strip.blank(self.size.width, self.rich_style)
        assert "\n" not in rich_text.plain

        # Pad so the right amount of horizontal space is used. We could just pass the width to the
        # Strip, but doing it here makes it easier to handle the cursor (as, when adding text, it's
        # behind the last element in rich_text).
        rich_text.append(" " * max(self.size.width - rich_text.cell_len - 1, 0))

        cursor_pos = self._screen.cursor_virtual_position
        if cursor_pos.y == y:
            rich_text.stylize("reverse", cursor_pos.x, cursor_pos.x + 1)

        segments = self.app.console.render(rich_text)
        strip = Strip(segments)
        return strip

    def _styled_chunks(self, y: int) -> Iterator[tuple[str, Style]]:
        last_style = _CharStyle.unstyled()
        pending_text: list[str] = []
        for char in itertools.chain(self._chars_for_line(y), [None]):
            if char is None or char.style != last_style:
                text = "".join(pending_text)
                if text:  # Avoid yielding an initial empty string for every line.
                    yield text, last_style.rich_style(self.rich_style)
                if char is not None:
                    pending_text = [char.data]
                    last_style = char.style
            else:
                pending_text.append(char.data)

    def _chars_for_line(self, y: int) -> Iterator[_Char]:
        pyte_chars = self._screen.content_at_virtual_line(y)
        if not pyte_chars:
            return
        # Explain it's defaultdict
        for i in range(max(pyte_chars.keys()) + 1):
            yield _Char.from_pyte_char(pyte_chars[i])

    async def on_key(self, event: events.Key) -> None:
        if self._fd is None:
            return

        event.stop()
        char = _KEY_TO_ANSI.get(event.key) or event.character
        if char is not None:
            os.write(self._fd, char.encode("utf-8"))


class TerminalTestApp(App):
    def __init__(self, cmd: list[str] | None = None) -> None:
        super().__init__()
        self.cmd = cmd or shlex.split(os.environ.get("APP", "./print_colors.py"))

    def compose(self) -> ComposeResult:
        yield widgets.Header()
        yield Terminal()
        yield widgets.Footer()

    def on_ready(self) -> None:
        self.query_one(Terminal).focus()

        pid, fd = pty.fork()
        if pid == 0:
            os.execvp(self.cmd[0], self.cmd)

        self.query_one(Terminal).attach_to_tty(fd, pid)


def main() -> None:
    app = TerminalTestApp(sys.argv[1:] or None)
    app.run()


if __name__ == "__main__":
    main()
