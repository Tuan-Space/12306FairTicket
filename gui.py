"""Desktop entry point for 12306FairTicket.

The command-line application deliberately remains independent from Qt.  This
small launcher keeps that property and provides a useful error when the GUI
extra has not been installed yet.
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from ticket_app.gui.app import run_gui
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("PySide6"):
            print(
                "图形界面需要 PySide6。请先执行: pip install PySide6",
                file=sys.stderr,
            )
            return 2
        raise
    return run_gui(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
