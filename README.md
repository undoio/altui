Altui — an alternative text-based UI for UDB
============================================

Altui provides an alternative to plain UDB and to TUI mode. It's implemented using the
[textual](https://textual.textualize.io/) Python package.

_**Warning:** This is an experiment. It's buggy and has limited functionalities._


How to install
--------------

1. Check out this repository:

   ```
   $ git clone git@git.undoers.io:mbarisione/altui.git
   ```

2. Run the `install_deps.sh` script passing a path where UDB is installed/compiled:

   ```
   $ cd altui/
   $ ./install_deps.sh ~/src/undo/core/release-x64
   ```

3. Source the file from you `.udbinit`/`.gdbinit` (see the documentation on
   [initialisation files](https://docs.undo.io/InitializationFiles.html)) by adding something like
   this:

   ```
   $ echo 'source <PATH_TO_ALTUI>/source_this.py'
   ```

   Alternatively, you can source the file only when needed.


How to use
----------

In a UDB session use `altui enable` to enable altui mode. `altui disable` goes back to normal text
mode.


Bugs
----

Please do not report bugs now (as this is just a prototype!) unless it's a crash/complete failure to
work.

Please send reports to Marco directly.
