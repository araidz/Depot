"""Depot entry point + run loop.

Single-threaded: poll stdin with a frame-interval timeout, dispatch keys,
drain aria2 every 500ms, full-redraw. Data loads in a background thread on
startup. aria2 does the downloading in its own process.
"""

from __future__ import annotations

import signal
import sys
import threading
import time

from . import __version__
from .download import Aria2, Aria2Error
from .tui import App, Terminal, render

HELP = ("depot — macOS installer & firmware downloader.\n"
        "  depot               start the TUI browser\n"
        "  depot --version     show version\n"
        "  depot --help        show this help")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    for a in argv:
        if a in ("-h", "--help"):
            print(HELP)
            return 0
        if a in ("-V", "--version"):
            print(f"depot {__version__}")
            return 0

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("depot needs an interactive terminal.")
        return 1

    eng = Aria2()
    try:
        eng.start()
    except Aria2Error as e:
        print(f"aria2 failed to start: {e}\nIs aria2 installed? (brew install aria2)")
        return 1

    app = App(eng)
    if eng.download_dir():
        app.download_dir = eng.download_dir()

    # load data in background
    app.load_data()

    term = Terminal()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    term.enter()
    last_poll = 0.0
    try:
        while app.running:
            for k in term.read_keys(0.04 if app.animating() else 0.2):
                app.on_key(k)
            if not app.running:
                break
            now = time.monotonic()
            if now - last_poll > 0.5:
                try:
                    app.update_downloads(eng.poll())
                except Aria2Error:
                    pass
                last_poll = now
            cols, rows = term.size()
            term.write(render(app, cols, rows))
    except KeyboardInterrupt:
        pass
    finally:
        term.leave()
        eng.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
