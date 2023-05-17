Altui — an alternative text-based UI for UDB
============================================

Altui provides a modern and user-friendly alternative to plain UDB and to TUI mode. It's implemented
using the [textual](https://textual.textualize.io/) Python package.

_**Warning: This is an experiment. It's buggy and has limited functionalities.**_


Installation
------------

###  How to install (customers)

1. Unpack the content of the `altui.zip` file somewhere on your computer.

2. Run the `install_deps.sh` script passing a path where UDB is installed:

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

   Alternatively, you can source the file only when needed with the same command.



### How to install (Undo devs)

1. Check out this repository:

   ```
   $ git clone git@git.undoers.io:mbarisione/altui.git
   ```

2. Run the `install_deps.sh` script passing a path where UDB is installed/compiled:

   ```
   $ cd altui/
   $ ./install_deps.sh ~/src/undo/core/release-x64
   ```

3. Source the file from your `.udbinit`/`.gdbinit` (see the documentation on
   [initialisation files](https://docs.undo.io/InitializationFiles.html)) by adding a line like
   this:

   ```
   source <PATH_TO_ALTUI>/source_this.py
   ```

   Alternatively, you can source the file only when needed with the same command.


### How to share with customers

1. Run the `make_dist.sh` script.

2. Share the generated `altui.zip` file and tell them to read the `README.md` file.

   **Warning:** The ZIP file includes all files in the git repository, including this one!


How to use
----------

In a UDB session use `altui enable` to enable altui mode. `altui disable` goes back to normal text
mode.

Once altui is running you should see a large area on the bottom showing a normal UDB terminal where
you can type commands. The panel on the top will show the source code for the program you are
debugging.

On the right there are a few panels with useful information: the backtrace, variables, bookmarks,
etc. You can select these using your mouse. Keyboard-based navigation is not implemented yet.


Bugs
----

This is a prototype so it's buggy and lots of things are not implemented yet. For comments, bugs and
questions email [Marco Barisione &lt;mbarisione@undo.io&gt;](mailto:mbarisione@undo.io).
