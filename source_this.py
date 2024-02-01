def _configure_altui() -> None:
    import os
    import sys

    import gdb  # type: ignore[import]

    try:
        # pylint: disable=unused-import
        from src.udbpy.gdb_extensions import udb_base  # type: ignore[import]
    except ImportError:
        print("Only recent versions of UDB are supported, not plain GDB.")
        return

    from src.udbpy import cfg  # type: ignore[import]

    if int(cfg.get().build_id_version.split(".")[0]) < 7:
        print("Altui requires UDB 7.0 or later.")
        return

    try:
        # _udb is injected in the global scope (where this file must be sourced from) by UDB.
        gdb._udb = _udb  # type: ignore[name-defined]  # pylint: disable=protected-access
    except NameError:
        print('You must source this file via the "source" command or the "-x" command-line option.')
        return

    deps_dir = os.path.join(
        os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
        "undo/altui_packages",
    )
    missing = [
        package
        for package in (
            "pyte",
            "rich",
            "textual",
        )
        if not os.path.exists(os.path.join(deps_dir, package))
    ]
    if missing:
        print(
            f"Missing dependencies: {', '.join(missing)}\n"
            "Please run ./install_deps.sh before trying to source this file."
        )
        return
    sys.path.append(deps_dir)

    # Set up altui.
    sys.path.append(os.path.dirname(__file__))

    from altui import gdbsupport, ioutil, telemetry_support

    telemetry_support.get(gdb._udb).sourced = True  # pylint: disable=protected-access

    original_stderr = os.dup(sys.__stderr__.fileno())
    err_msg = None
    configuration = None
    try:
        configuration = gdbsupport.Configuration()
    except gdbsupport.UnsupportedError as exc:
        err_msg = str(exc)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        ioutil.reset_tty(original_stderr)
        os.write(
            original_stderr,
            (
                f"\nFailed to initialise altui: {exc}\n"
                "This program may have been left in an unreliable state and it will abort.\n"
            ).encode("utf-8"),
        )
        os.abort()
    finally:
        os.close(original_stderr)

    # Here it's safe to import other modules as Configuration took care of dealing with the initial
    # rich/textual configuration.
    from altui import commands

    commands.register(configuration, err_msg)

    gdb.execute("set prompt -reset")


_configure_altui()
del _configure_altui
