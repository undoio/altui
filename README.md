Altui — an alternative text-based UI for UDB
============================================

Altui provides a modern and user-friendly alternative to plain UDB and to TUI mode. It's implemented
using the [textual](https://textual.textualize.io/) Python package.

_**Warning: This is an experiment. It's buggy and has limited functionalities.**_


Installation
------------

1. Check out this repository:

   ```
   $ git clone https://github.com/undoio/altui.git
   ```

2. Run the `install_deps.sh` script passing a path where UDB is installed. For instance:

   ```
   $ cd altui/
   $ ./install_deps.sh /usr/local/undo
   ```

3. Source the file from your `.udbinit`/`.gdbinit` (see the documentation on
   [initialisation files](https://docs.undo.io/InitializationFiles.html)) by adding a line like
   this:

   ```
   source <PATH_TO_ALTUI>/source_this.py
   ```

   Alternatively, you can source the file only when needed running the command above in a UDB
   session.


How to use
----------

In a UDB session use `altui enable` to enable altui mode. `altui disable` goes back to normal text
mode.

Once altui is running you should see a large area in the bottom half of the screen showing a normal
UDB terminal where you can type commands. The panel on the top will show the source code for the
program you are debugging.

On the right there are a few panels with useful information: the backtrace, variables, bookmarks,
etc. You can select these using your mouse. Keyboard-based navigation is not implemented yet.


Limitations
-----------

* The UI doesn't notice if the terminal it's running in is resized.

* Probably very slow with programs with a lot of threads, deep backtraces, etc.

* No support for configuration (like re-arranging panels).

* Many more not listed here! This is just a prototype.

You can report issues on [GitHub issues](https://github.com/undoio/altui/issues). Alternatively,
feel free to email [Marco Barisione &lt;mbarisione@undo.io&gt;](mailto:mbarisione@undo.io) with
questions, issues or suggestions.
