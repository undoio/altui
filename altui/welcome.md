Welcome to altui, an alternative text-based UI for UDB
======================================================

Altui provides a modern and user-friendly alternative to plain UDB and to TUI mode.

_**Warning: This is an experiment. It's buggy and has limited features. It may also stop working
at any point.**_


How to use
----------

* The panel underneath this one is a normal UDB terminal where you can type UDB commands.

* Once you start debugging, this panel will show the source code for the debugged program.

* On the right there are a few panels with useful information: the backtrace, variables, bookmarks,
  etc. You can select these using your mouse. Keyboard-based navigation is not implemented yet.

* Type `altui disable` to go back to normal UDB.


Limitations
-----------

* The UI doesn't notice if the terminal it's running in is resized.

* Probably very slow with programs with a lot of threads, deep backtraces, etc.

* No support for configuration (like re-arranging panels).

* Many more not listed here! This is just a prototype.

You can report issues on [GitHub issues](https://github.com/undoio/altui/issues). Alternatively,
feel free to email [Marco Barisione &lt;mbarisione@undo.io&gt;](mailto:mbarisione@undo.io) with
questions, issues or suggestions.
