"""Raw-ANSI terminal UI for Slipway: state, render, input. No third-party deps.

Full-redraw renderer (truecolor) with a warm amber theme. Left-rail navigation
between Firmwares / Installers / Downloads, table views, sheen progress bars.
The run loop lives in __main__.py.
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import threading
import time
import tty
import unicodedata

from . import theme as T
from .sources import (Firmware, Installer, firmwares, installers,
                      CATALOG_NAMES)
from .download import Aria2, Aria2Error, Download
from .flash import (FlashError, USBDisk, list_usb_disks,
                    _find_installer_app, format_and_flash)

# ---------------------------------------------------------------------------
# ANSI + width primitives
# ---------------------------------------------------------------------------

RESET = "\x1b[0m"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _fg(hexc: str) -> str:
    n = int(hexc[1:], 16)
    return f"\x1b[38;2;{(n >> 16) & 255};{(n >> 8) & 255};{n & 255}m"


def style(text: str, color: str | None = None, bold: bool = False, dim: bool = False) -> str:
    pre = ("\x1b[1m" if bold else "") + ("\x1b[2m" if dim else "") + (_fg(color) if color else "")
    return f"{pre}{text}{RESET}" if pre else text


def strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


def _cw(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def dwidth(s: str) -> int:
    return sum(_cw(c) for c in s)


def dtrunc(s: str, maxw: int) -> str:
    if maxw <= 0:
        return ""
    if dwidth(s) <= maxw:
        return s
    out, w = "", 0
    for ch in s:
        cw = _cw(ch)
        if w + cw > maxw - 1:
            break
        out += ch
        w += cw
    return out + "…"


def pad(s: str, w: int, align: str = "left") -> str:
    gap = w - dwidth(s)
    if gap <= 0:
        return s
    if align == "right":
        return " " * gap + s
    if align == "center":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


def cell(text: str, w: int, align: str = "left", color: str | None = None,
         bold: bool = False, dim: bool = False) -> str:
    if w <= 0:
        return ""
    return style(pad(dtrunc(text, w), w, align), color, bold, dim)


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def fmt_bytes(n: float | None) -> str:
    if not n or n <= 0:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.0f} {units[i]}" if i == 0 else f"{n:.1f} {units[i]}"


def fmt_speed(n: float | None) -> str:
    if not n or n <= 0:
        return "0 B/s"
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.1f} {units[i]}" if (n < 10 and i > 0) else f"{n:.0f} {units[i]}"


def fmt_eta(sec: float | None) -> str:
    if not sec or sec <= 0 or sec == float("inf"):
        return ""
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean(s: str) -> str:
    s = "".join(c if c.isprintable() or c == " " else " " for c in s)
    return re.sub(r"\s+", " ", s).strip()


def copy_clipboard(text: str) -> bool:
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def reveal(path: str) -> bool:
    args = ["open", "-R", path] if os.path.isfile(path) else ["open", path]
    try:
        subprocess.run(args, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def open_url(url: str) -> bool:
    try:
        subprocess.run(["open", url], check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def notify(title: str, message: str) -> None:
    script = f'display notification {json.dumps(clean(message)[:200])} with title {json.dumps(title)}'
    try:
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Progress bar
# ---------------------------------------------------------------------------

def render_bar(progress: float, width: int, tick: float, animate: bool,
               base: str = T.ACCENT) -> str:
    if width <= 0:
        return ""
    filled = round(max(0.0, min(1.0, progress)) * width)
    empty = width - filled
    denom = max(1, width - 1)
    period = T.sheen_period(width)
    center = T.sheen_center(tick, period)
    cells = []
    for i in range(filled):
        c = T.progress_ramp(i / denom, T.DEEP, base, T.BRIGHT)
        if animate:
            inten = T.sheen_intensity(i, center)
            if inten > 0:
                c = T.lerp_hex(c, T.SHEEN_PEAK, inten)
        cells.append(c)
    out = []
    j = 0
    while j < len(cells):
        k = j
        while k < len(cells) and cells[k] == cells[j]:
            k += 1
        out.append(style(T.BLOCK * (k - j), cells[j]))
        j = k
    if empty:
        out.append(style(T.TRACK * empty, T.RULE))
    return "".join(out)


# ---------------------------------------------------------------------------
# Key parsing
# ---------------------------------------------------------------------------

_ARROWS = {b"A": "up", b"B": "down", b"C": "right", b"D": "left"}


def parse_keys(data: bytes) -> list[str]:
    keys: list[str] = []
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b == 0x1b:
            if data[i:i + 3] == b"\x1b[<":  # SGR mouse
                j = i + 3
                while j < n and data[j] not in (ord("M"), ord("m")):
                    j += 1
                parts = data[i + 3:j].split(b";")
                if parts and parts[0].isdigit():
                    btn = int(parts[0])
                    if btn == 64:
                        keys.append("up")
                    elif btn == 65:
                        keys.append("down")
                i = j + 1
            elif data[i:i + 3] == b"\x1b[Z":  # back-tab (Shift-Tab)
                keys.append("shift-tab")
                i += 3
            elif i + 2 < n and data[i + 1] in (ord("["), ord("O")) and bytes([data[i + 2]]) in _ARROWS:
                keys.append(_ARROWS[bytes([data[i + 2]])])
                i += 3
            else:
                keys.append("esc")
                i += 1
        elif b in (0x0d, 0x0a):
            keys.append("enter")
            i += 1
        elif b in (0x7f, 0x08):
            keys.append("backspace")
            i += 1
        elif b == 0x09:
            keys.append("tab")
            i += 1
        elif b == 0x03:
            keys.append("ctrl-c")
            i += 1
        elif b < 0x20:
            i += 1
        else:
            j = i
            while j < n and data[j] >= 0x20 and data[j] != 0x1b:
                j += 1
            keys.extend(data[i:j].decode("utf-8", "ignore"))
            i = j
    return keys


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------

class Terminal:
    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.saved = None

    def enter(self) -> None:
        self.saved = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        sys.stdout.write("\x1b[?1049h\x1b[3J\x1b[2J\x1b[H\x1b[?25l\x1b[?1000h\x1b[?1006h")
        sys.stdout.flush()

    def leave(self) -> None:
        sys.stdout.write("\x1b[?1000l\x1b[?1006l\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if self.saved:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def size(self) -> tuple[int, int]:
        s = shutil.get_terminal_size((100, 30))
        return s.columns, s.lines

    def read_keys(self, timeout: float) -> list[str]:
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        try:
            data = os.read(self.fd, 4096)
        except OSError:
            return []
        return parse_keys(data)

    def write(self, lines: list[str]) -> None:
        buf = ["\x1b[H"]
        for i, ln in enumerate(lines):
            buf.append(ln + "\x1b[K")
            if i < len(lines) - 1:
                buf.append("\r\n")
        buf.append("\x1b[J")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_csv(items: list, path: str) -> bool:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    if items and isinstance(items[0], Firmware):
        w.writerow(["Name", "Version", "Build", "Size", "URL", "SHA1"])
        for f in items:
            w.writerow([f.name, f.version, f.build, f.size, f.url, f.sha1])
    elif items and isinstance(items[0], Installer):
        w.writerow(["Name", "Version", "Build", "Date", "Size", "URL", "ProductID"])
        for i in items:
            w.writerow([i.name, i.version, i.build, i.date, i.size, i.url, i.product_id])
    try:
        with open(path, "w", newline="") as f:
            f.write(buf.getvalue())
        return True
    except OSError:
        return False


def export_json(items: list, path: str) -> bool:
    def _enc(o):
        if isinstance(o, (Firmware, Installer)):
            return {k: v for k, v in o.__dict__.items()}
        return str(o)
    try:
        with open(path, "w") as f:
            json.dump(items, f, indent=2, default=_enc)
        return True
    except OSError:
        return False


def export_plist(items: list, path: str) -> bool:
    import plistlib
    def _enc(o):
        if isinstance(o, (Firmware, Installer)):
            return {k: v for k, v in o.__dict__.items()}
        return str(o)
    try:
        data = json.loads(json.dumps(items, default=_enc))
        with open(path, "wb") as f:
            plistlib.dump(data, f)
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------

PANE_FIRMWARES = 0
PANE_INSTALLERS = 1
PANE_DOWNLOADS = 2
PANES = [
    ("Firmwares", PANE_FIRMWARES),
    ("Installers", PANE_INSTALLERS),
    ("Downloads", PANE_DOWNLOADS),
]

SORT_VERSION = "version"
SORT_DATE = "date"
SORT_SIZE = "size"
SORT_ORDER = [SORT_VERSION, SORT_DATE, SORT_SIZE]


class App:
    def __init__(self, eng: Aria2 | None = None):
        self.eng = eng
        self.running = True
        self.start = time.monotonic()

        # pane: 0=firmwares, 1=installers, 2=downloads
        self.pane = PANE_FIRMWARES
        self.sel = 0  # selection index in current list

        # data
        self.firmwares: list[Firmware] = []
        self.installers: list[Installer] = []
        self.downloads: list[Download] = []
        self.data_loaded = False
        self.loading = True
        self.loading_msg = "Loading data…"

        # filters
        self.sort = SORT_VERSION
        self.filter_text = ""

        # catalog selection
        self.catalog_idx = 0  # index into CATALOG_NAMES
        self.catalog = CATALOG_NAMES[0]

        # export prompt
        self.export_prompt = False

        # flash to USB flow
        self.flash_prompt = False
        self.flash_step = ""  # "" | "select_disk" | "confirm" | "flashing" | "done"
        self.flash_disks: list[USBDisk] = []
        self.flash_disk_sel = 0
        self.flash_status = ""
        self.flash_thread: threading.Thread | None = None

        # detail view
        self.detail: Firmware | Installer | None = None

        # status bar
        self.status = ""
        self.help = False

        # download dir
        self.download_dir: str | None = None

    @property
    def tick(self) -> float:
        return (time.monotonic() - self.start) * 1000 / T.SHEEN_TICK_MS

    def animating(self) -> bool:
        return any(d.status in ("active", "waiting") for d in self.downloads)

    # -- data loading (runs in background) --

    def load_data(self) -> None:
        """Fetch firmwares + installers on a background thread."""
        def _load():
            try:
                self.firmwares = firmwares()
            except Exception as e:
                self.firmwares = []
                self.status = f"firmware load error: {e}"
            try:
                self.installers = installers(catalog=self.catalog)
            except Exception as e:
                if not self.status:
                    self.status = f"installer load error: {e}"
            self.data_loaded = True
            self.loading = False
        threading.Thread(target=_load, daemon=True).start()

    def reload_installers(self) -> None:
        """Reload installers with the current catalog (runs in background)."""
        self.loading = True
        self.loading_msg = f"Loading {self.catalog}…"
        self.sel = 0
        def _reload():
            try:
                self.installers = installers(catalog=self.catalog)
            except Exception as e:
                self.status = f"installer load error: {e}"
            self.loading = False
        threading.Thread(target=_reload, daemon=True).start()

    # -- filtered/sorted views --

    def visible_firmwares(self) -> list[Firmware]:
        items = self.firmwares
        if self.filter_text:
            q = self.filter_text.lower()
            items = [f for f in items if q in f.name.lower() or q in f.version or q in f.build.lower()]
        return _cluster_by_name(self._sort_items(items))

    def visible_installers(self) -> list[Installer]:
        items = self.installers
        if self.filter_text:
            q = self.filter_text.lower()
            items = [i for i in items if q in i.name.lower() or q in i.version or q in i.build.lower()]
        return _cluster_by_name(self._sort_items(items))

    def _sort_items(self, items: list) -> list:
        if self.sort == SORT_SIZE:
            return sorted(items, key=lambda x: x.size, reverse=True)
        if self.sort == SORT_DATE:
            def _date_key(x):
                d = getattr(x, "date", "") or ""
                v = getattr(x, "version", "") or ""
                return d or v
            return sorted(items, key=_date_key, reverse=True)
        # version
        return sorted(items, key=lambda x: [int(p) for p in x.version.split(".") if p.isdigit()], reverse=True)

    def cur_list(self) -> list:
        if self.pane == PANE_FIRMWARES:
            return self.visible_firmwares()
        if self.pane == PANE_INSTALLERS:
            return self.visible_installers()
        return self.downloads

    def cur_item(self):
        items = self.cur_list()
        return items[self.sel] if 0 <= self.sel < len(items) else None

    # -- actions --

    def download_selected(self) -> None:
        if not self.eng:
            self.status = "no download engine"
            return
        item = self.cur_item()
        if item is None:
            return
        dest = self.download_dir or os.path.expanduser("~/Downloads")
        try:
            sha1 = item.sha1 if isinstance(item, Firmware) else ""
            self.eng.add(item.url, dest=dest, sha1=sha1)
            self.status = f"downloading: {item.name} {item.version}"
        except Aria2Error as e:
            self.status = f"error: {e}"

    def cycle_sort(self) -> None:
        i = SORT_ORDER.index(self.sort) if self.sort in SORT_ORDER else 0
        self.sort = SORT_ORDER[(i + 1) % len(SORT_ORDER)]
        self.sel = 0
        self.status = f"sorted by {self.sort}"

    def move(self, d: int) -> None:
        n = len(self.cur_list())
        self.sel = max(0, min(self.sel + d, n - 1)) if n else 0

    def cycle_pane(self, d: int) -> None:
        self.pane = (self.pane + d) % 3
        self.sel = 0

    def do_export(self, fmt: str) -> None:
        items = self.cur_list()
        if not items:
            self.status = "nothing to export"
            return
        ext = {"csv": ".csv", "json": ".json", "plist": ".plist"}.get(fmt, ".txt")
        path = os.path.expanduser(f"~/Desktop/{self.pane_name()}{ext}")
        ok = False
        if fmt == "csv":
            ok = export_csv(items, path)
        elif fmt == "json":
            ok = export_json(items, path)
        elif fmt == "plist":
            ok = export_plist(items, path)
        self.status = f"exported to {path}" if ok else "export failed"

    def pane_name(self) -> str:
        return ["Firmwares", "Installers", "Downloads"][self.pane]

    def cycle_catalog(self) -> None:
        self.catalog_idx = (self.catalog_idx + 1) % len(CATALOG_NAMES)
        self.catalog = CATALOG_NAMES[self.catalog_idx]
        self.reload_installers()
        self.status = f"catalog: {self.catalog}"

    def start_flash(self) -> None:
        """Begin the flash-to-USB flow."""
        self.flash_prompt = True
        self.flash_step = "select_disk"
        self.flash_status = "Scanning for USB drives…"
        self.flash_disk_sel = 0
        def _scan():
            try:
                self.flash_disks = list_usb_disks()
                if not self.flash_disks:
                    self.flash_status = "No USB drives detected. Plug one in and try again."
                    self.flash_step = ""
                    self.flash_prompt = False
                else:
                    self.flash_status = f"Found {len(self.flash_disks)} drive(s). Select one:"
            except FlashError as e:
                self.flash_status = str(e)
                self.flash_step = ""
                self.flash_prompt = False
        threading.Thread(target=_scan, daemon=True).start()

    def flash_confirm(self) -> None:
        """Start the actual flashing process."""
        self.flash_step = "flashing"
        disk = self.flash_disks[self.flash_disk_sel]
        self.flash_status = f"Flashing {disk.name}…"
        def _do_flash():
            try:
                app_path = _find_installer_app()
                def _cb(msg):
                    self.flash_status = msg
                format_and_flash(disk, app_path, status_cb=_cb)
                self.flash_step = "done"
            except FlashError as e:
                self.flash_status = f"Error: {e}"
                self.flash_step = ""
        self.flash_thread = threading.Thread(target=_do_flash, daemon=True)
        self.flash_thread.start()

    # -- key handling --

    def on_key(self, k: str) -> None:
        if self.help:
            self.help = False
            return
        if k == "ctrl-c":
            self.running = False
            return
        if self.export_prompt:
            self.export_prompt = False
            if k in ("c", "1"):
                self.do_export("csv")
            elif k in ("j", "2"):
                self.do_export("json")
            elif k in ("p", "3"):
                self.do_export("plist")
            return
        if self.flash_prompt:
            if k == "esc":
                self.flash_prompt = False
                self.flash_step = ""
            elif self.flash_step == "select_disk" and self.flash_disks:
                if k in ("up", "k"):
                    self.flash_disk_sel = max(0, self.flash_disk_sel - 1)
                elif k in ("down", "j"):
                    self.flash_disk_sel = min(len(self.flash_disks) - 1, self.flash_disk_sel + 1)
                elif k == "enter":
                    self.flash_step = "confirm"
                    self.flash_status = f"Flash {self.flash_disks[self.flash_disk_sel].name}? This will ERASE ALL DATA. (y/n)"
            elif self.flash_step == "confirm":
                if k == "y":
                    self.flash_confirm()
                else:
                    self.flash_prompt = False
                    self.flash_step = ""
            elif self.flash_step == "done":
                self.flash_prompt = False
                self.flash_step = ""
            return
        if self.detail is not None:
            if k in ("esc", "enter", "q"):
                self.detail = None
            elif k == "d":
                self.download_selected()
                self.detail = None
            elif k == "y":
                url = getattr(self.detail, "url", "")
                self.status = "URL copied" if url and copy_clipboard(url) else "copy failed"
            return

        # global keys
        if k == "q":
            self.running = False
        elif k == "?":
            self.help = True
        elif k == "tab":
            self.cycle_pane(1)
        elif k == "shift-tab" or k == "backtab":
            self.cycle_pane(-1)
        elif k in ("up", "k"):
            self.move(-1)
        elif k in ("down", "j"):
            self.move(1)
        elif k == "g":
            self.sel = 0
        elif k == "G":
            self.sel = max(0, len(self.cur_list()) - 1)
        elif k == "S":
            self.cycle_sort()
        elif k == "d":
            self.download_selected()
        elif k == "enter":
            if (item := self.cur_item()):
                self.detail = item
        elif k == "y":
            if (item := self.cur_item()):
                url = getattr(item, "url", "")
                self.status = "URL copied" if url and copy_clipboard(url) else "copy failed"
        elif k == "o":
            if (item := self.cur_item()) and hasattr(item, "path") and item.path:
                self.status = "revealed" if reveal(item.path) else "couldn't open"
        elif k == "e":
            self.export_prompt = True
        elif k == "/":
            self.filter_text = ""
            self.sel = 0
            self.status = "filter cleared"
        elif k == "c":
            if self.pane == PANE_INSTALLERS:
                self.cycle_catalog()
        elif k == "f":
            if self.pane == PANE_INSTALLERS:
                self.start_flash()
            else:
                self.status = "flash to USB: switch to Installers pane first"
        elif k == "x":
            # cancel download
            if self.pane == PANE_DOWNLOADS and self.downloads:
                d = self.downloads[self.sel] if 0 <= self.sel < len(self.downloads) else None
                if d and self.eng:
                    self.eng.remove(d.gid)
                    self.status = f"cancelled: {d.name}"
        elif k == "p":
            # pause/resume download
            if self.pane == PANE_DOWNLOADS and self.downloads:
                d = self.downloads[self.sel] if 0 <= self.sel < len(self.downloads) else None
                if d and self.eng:
                    if d.status == "paused":
                        self.eng.resume(d.gid)
                        self.status = f"resumed: {d.name}"
                    else:
                        self.eng.pause(d.gid)
                        self.status = f"paused: {d.name}"
        elif k == "r":
            # retry failed download
            if self.pane == PANE_DOWNLOADS and self.downloads:
                d = self.downloads[self.sel] if 0 <= self.sel < len(self.downloads) else None
                if d and d.status == "error" and self.eng:
                    self.eng.retry(d.gid)
                    self.status = f"retrying: {d.name}"
        elif len(k) == 1 and k >= " ":
            # filter typing (only on list panes)
            if self.pane in (PANE_FIRMWARES, PANE_INSTALLERS):
                self.filter_text += k
                self.sel = 0
                self.status = f"filter: {self.filter_text}"
        elif k == "backspace":
            if self.filter_text:
                self.filter_text = self.filter_text[:-1]
                self.sel = 0
                self.status = f"filter: {self.filter_text}" if self.filter_text else "filter cleared"

    def update_downloads(self, downloads: list[Download]) -> None:
        prev = {d.gid: d.status for d in self.downloads}
        for d in downloads:
            was = prev.get(d.gid)
            if d.status == "complete" and was is not None and was != "complete":
                notify("Slipway", f"download complete: {d.name}")
        self.downloads = downloads


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

RAIL_W = 16
MARGIN = 1


def _window(sel: int, total: int, h: int) -> int:
    if total <= h:
        return 0
    return max(0, min(sel - h // 2, total - h))


def _cluster_by_name(items: list) -> list:
    """Cluster items so all of one macOS release sit together, groups ordered
    by first appearance in the already-sorted list. Keeps selection indexing
    aligned with what the grouped view draws."""
    groups: dict[str, list] = {}
    order: list[str] = []
    for it in items:
        if it.name not in groups:
            groups[it.name] = []
            order.append(it.name)
        groups[it.name].append(it)
    out: list = []
    for name in order:
        out.extend(groups[name])
    return out


def _grouped_rows(items: list, sel: int, avail: int) -> list:
    """Turn a name-clustered item list into a windowed list of display rows.

    Rows are ("header", name, count) or ("item", item_index, obj). The window
    is centred on the selection; if it starts mid-group, the group's header is
    kept sticky at the top so you always know which release you're in.
    """
    counts: dict[str, int] = {}
    for it in items:
        counts[it.name] = counts.get(it.name, 0) + 1

    display: list = []
    sel_di = 0
    prev = None
    for idx, obj in enumerate(items):
        if obj.name != prev:
            display.append(("header", obj.name, counts[obj.name]))
            prev = obj.name
        if idx == sel:
            sel_di = len(display)
        display.append(("item", idx, obj))

    if len(display) <= avail:
        return display
    start = max(0, min(sel_di - avail // 2, len(display) - avail))
    win = display[start:start + avail]
    if win and win[0][0] == "item" and start > 0:
        obj = win[0][2]
        win = [("header", obj.name, counts[obj.name])] + win[1:]
    return win


def _group_header_line(name: str, count: int, width: int) -> str:
    head = f"▸ {name}"
    meta = f"{count} version{'s' if count != 1 else ''}"
    lead = pad(dtrunc(head, max(0, width - dwidth(meta) - 1)), width - dwidth(meta) - 1)
    return style(lead, T.ACCENT, bold=True) + " " + style(meta, T.ALT, dim=True)


def _logo_lines() -> list[str]:
    """SLIPWAY wordmark with a diagonal amber gradient + crate motif to its right."""
    out = []
    rows = len(T.LOGO_LINES)
    for row, line in enumerate(T.LOGO_LINES):
        chars = list(line)
        last = max(1, len(chars) - 1)
        ty = row / max(1, rows - 1)
        seg = ""
        for i, ch in enumerate(chars):
            if ch == " ":
                seg += " "
            else:
                seg += style(ch, T.logo_color(((i / last) + ty) / 2), bold=True)
        motif = T.LOGO_MOTIF[row] if row < len(T.LOGO_MOTIF) else ""
        out.append(seg + "  " + style(motif, T.CRATE_COLOR, bold=True))
    return out


def render_header(cols: int) -> list[str]:
    tagline = "macOS installer & firmware downloader"
    logo = _logo_lines()
    lines = [logo[0], logo[1] + "  " + style(tagline, T.ALT, dim=True)]
    lines.append(style("─" * cols, T.RULE))
    return lines


def render_rail(app: App, height: int) -> list[str]:
    """Left rail: pane list + counts."""
    lines: list[str] = []
    counts = [len(app.firmwares), len(app.installers), len(app.downloads)]
    for i, (name, _) in enumerate(PANES):
        selected = i == app.pane
        prefix = f"{T.PTR} " if selected else "  "
        cnt = f" ({counts[i]})" if counts[i] or i == PANE_DOWNLOADS else ""
        color = T.ACCENT if selected else T.ALT
        line = prefix + name + cnt
        lines.append(cell(line, RAIL_W, color=color, bold=selected))

    # fill remaining rail height
    while len(lines) < height:
        lines.append("")

    # filters info at bottom of rail
    if app.filter_text:
        lines[-3] = cell(f"filter: {app.filter_text}", RAIL_W, color=T.WARN, dim=True)
    if app.pane == PANE_INSTALLERS:
        cat_short = app.catalog.split("(")[0].strip()[:12]
        beta = app.catalog != "Release"
        lines[-2] = cell("catalog (c):", RAIL_W, color=T.ALT, dim=True)
        lines[-1] = cell(f" {cat_short}", RAIL_W,
                         color=T.WARN if beta else T.ACCENT, bold=beta)
    else:
        lines[-1] = cell(f"sort: {app.sort}", RAIL_W, color=T.ALT, dim=True)

    return lines


def render_firmware_table(app: App, width: int, height: int) -> list[str]:
    items = app.visible_firmwares()
    if not items and not app.loading:
        return [cell("No firmwares found.", width, color=T.ALT)]

    # column widths
    w_name = min(20, max(10, width * 30 // 100))
    w_ver = 10
    w_build = 10
    w_size = 10
    w_sha1 = 8
    # adjust name to fill remaining
    w_name = max(w_name, width - w_ver - w_build - w_size - w_sha1 - 4)

    # header
    hdr = ("   " +
           cell("Name", w_name - 3, bold=True, color=T.ALT) + " " +
           cell("Version", w_ver, bold=True, color=T.ALT, align="right") + " " +
           cell("Build", w_build, bold=True, color=T.ALT) + " " +
           cell("Size", w_size, bold=True, color=T.ALT, align="right") + " " +
           cell("SHA1", w_sha1, bold=True, color=T.ALT))

    lines = [style("─" * width, T.RULE), hdr, style("─" * width, T.RULE)]

    # grouped, windowed rows
    avail = height - len(lines) - 1  # leave room for footer hint
    for kind, a, b in _grouped_rows(items, app.sel, avail):
        if kind == "header":
            lines.append(_group_header_line(a, b, width))
            continue
        idx, fw = a, b
        selected = idx == app.sel
        color = T.TEXT if selected else T.ALT
        prefix = f"{T.PTR} " if selected else "   "
        row = (prefix +
               cell(fw.name, w_name - 3, color=color, bold=selected) + " " +
               cell(fw.version, w_ver, color=color, align="right") + " " +
               cell(fw.build, w_build, color=color) + " " +
               cell(fw.size_str, w_size, color=color, align="right") + " " +
               cell(fw.sha1[:7], w_sha1, color=T.ALT, dim=True))
        lines.append(row)

    # pad to height
    while len(lines) < height:
        lines.append("")

    return lines


def render_installer_table(app: App, width: int, height: int) -> list[str]:
    items = app.visible_installers()
    if not items and not app.loading:
        return [cell("No installers found.", width, color=T.ALT)]

    w_name = min(18, max(10, width * 25 // 100))
    w_ver = 10
    w_build = 10
    w_date = 12
    w_size = 10
    w_name = max(w_name, width - w_ver - w_build - w_date - w_size - 5)

    hdr = ("   " +
           cell("Name", w_name - 3, bold=True, color=T.ALT) + " " +
           cell("Version", w_ver, bold=True, color=T.ALT, align="right") + " " +
           cell("Build", w_build, bold=True, color=T.ALT) + " " +
           cell("Date", w_date, bold=True, color=T.ALT) + " " +
           cell("Size", w_size, bold=True, color=T.ALT, align="right"))

    lines = [style("─" * width, T.RULE), hdr, style("─" * width, T.RULE)]

    avail = height - len(lines) - 1
    for kind, a, b in _grouped_rows(items, app.sel, avail):
        if kind == "header":
            lines.append(_group_header_line(a, b, width))
            continue
        idx, inst = a, b
        selected = idx == app.sel
        color = T.TEXT if selected else T.ALT
        prefix = f"{T.PTR} " if selected else "   "
        row = (prefix +
               cell(inst.name, w_name - 3, color=color, bold=selected) + " " +
               cell(inst.version, w_ver, color=color, align="right") + " " +
               cell(inst.build, w_build, color=color) + " " +
               cell(inst.date, w_date, color=T.ALT) + " " +
               cell(inst.size_str, w_size, color=color, align="right"))
        lines.append(row)

    while len(lines) < height:
        lines.append("")

    return lines


def render_downloads(app: App, width: int, height: int, tick: float) -> list[str]:
    if not app.downloads:
        return [cell("No active downloads.", width, color=T.ALT)]

    lines: list[str] = []
    for i, d in enumerate(app.downloads):
        selected = i == app.sel
        color = T.TEXT if selected else T.ALT
        prefix = f"{T.PTR} " if selected else "   "

        # status icon
        if d.status == "complete":
            icon = style(T.DONE, T.GOOD, bold=True)
        elif d.status == "error":
            icon = style(T.ERR, T.BAD, bold=True)
        elif d.status == "paused":
            icon = style("⏸", T.PAUSED)
        else:
            icon = style(T.DOWN, T.ACCENT)

        # name line
        name_line = prefix + icon + " " + cell(d.name, width - 20, color=color, bold=selected)

        # progress line with bar
        bar_w = max(10, width - 30)
        bar = render_bar(d.progress, bar_w, tick, d.status in ("active", "waiting"))
        pct = f"{d.progress * 100:.1f}%"
        info_parts = [pct]
        if d.speed > 0:
            info_parts.append(fmt_speed(d.speed))
        if d.eta is not None:
            info_parts.append(fmt_eta(d.eta))
        if d.total > 0:
            info_parts.append(f"{fmt_bytes(d.completed)}/{fmt_bytes(d.total)}")
        info = " · ".join(info_parts)
        info_line = "   " + bar + " " + style(info, T.ALT, dim=True)

        lines.append(name_line)
        lines.append(info_line)
        if d.error:
            lines.append("   " + style(d.error[:width - 5], T.BAD, dim=True))
        lines.append("")  # gap between downloads

    while len(lines) < height:
        lines.append("")

    return lines


def render_detail(app: App, width: int, height: int) -> list[str]:
    """Full-width detail overlay for the selected item."""
    item = app.detail
    if item is None:
        return []

    inner_w = width - 6
    lines: list[str] = []

    lines.append("")
    lines.append(style("  Detail", T.ACCENT, bold=True))
    lines.append(style("  " + "─" * (width - 4), T.RULE))
    lines.append("")

    if isinstance(item, Firmware):
        fields = [
            ("Name", item.name),
            ("Version", item.version),
            ("Build", item.build),
            ("Size", item.size_str),
            ("SHA-1", item.sha1),
            ("URL", item.url),
        ]
    else:
        fields = [
            ("Name", item.name),
            ("Version", item.version),
            ("Build", item.build),
            ("Date", item.date),
            ("Size", item.size_str),
            ("Product ID", item.product_id),
            ("URL", item.url),
        ]

    for label, value in fields:
        lbl = style(f"  {label}:", T.ACCENT, bold=True)
        # wrap long values
        val_w = inner_w - len(label) - 3
        if dwidth(str(value)) > val_w:
            val = dtrunc(str(value), val_w)
        else:
            val = str(value)
        lines.append(lbl + " " + style(val, T.TEXT))

    lines.append("")
    lines.append(style("  d download  y copy URL  Enter/Esc back", T.ALT, dim=True))
    lines.append("")

    while len(lines) < height:
        lines.append("")

    return lines


def render_help(width: int, height: int) -> list[str]:
    help_text = [
        ("Key", "Action"),
        ("─" * 20, "─" * 40),
        ("j / ↓", "move down"),
        ("k / ↑", "move up"),
        ("g", "jump to top"),
        ("G", "jump to bottom"),
        ("Tab", "switch pane →"),
        ("Shift-Tab", "← switch pane"),
        ("Enter", "view details"),
        ("d", "download selected"),
        ("y", "copy URL to clipboard"),
        ("e", "export list (csv/json/plist)"),
        ("S", "cycle sort (version/date/size)"),
        ("c", "cycle catalog (installers)"),
        ("f", "flash bootable USB (installers)"),
        ("type", "filter list (backspace to delete)"),
        ("/", "clear filter"),
        ("o", "reveal in Finder (downloads)"),
        ("p", "pause / resume (downloads)"),
        ("x", "cancel download"),
        ("r", "retry failed download"),
        ("?", "toggle this help"),
        ("q", "quit"),
    ]

    lines = [style("  Slipway — Keyboard Shortcuts", T.ACCENT, bold=True), ""]
    for key, action in help_text:
        lines.append(f"  {cell(key, 20, color=T.ACCENT)} {style(action, T.TEXT)}")
    lines.append("")
    lines.append(style("  Press any key to close", T.ALT, dim=True))

    while len(lines) < height:
        lines.append("")

    return lines


def render_export_prompt(width: int, height: int) -> list[str]:
    lines = [
        "",
        style("  Export as:", T.ACCENT, bold=True),
        "",
        f"  {style('[c]', T.ACCENT, bold=True)} CSV",
        f"  {style('[j]', T.ACCENT, bold=True)} JSON",
        f"  {style('[p]', T.ACCENT, bold=True)} plist",
        "",
        style("  Esc to cancel", T.ALT, dim=True),
    ]
    while len(lines) < height:
        lines.append("")
    return lines


def render_flash_prompt(app: App, width: int, height: int) -> list[str]:
    lines = [
        "",
        style("  Flash Bootable Installer", T.ACCENT, bold=True),
        style("  " + "─" * (width - 4), T.RULE),
        "",
    ]

    if app.flash_step == "select_disk" and app.flash_disks:
        lines.append(style("  Select a USB drive:", T.TEXT))
        lines.append("")
        for i, disk in enumerate(app.flash_disks):
            selected = i == app.flash_disk_sel
            prefix = f"  {T.PTR} " if selected else "     "
            color = T.TEXT if selected else T.ALT
            lines.append(prefix + style(f"{disk.identifier}  {disk.name}  {disk.size_str}", color, bold=selected))
        lines.append("")
        lines.append(style("  ↑↓ select · Enter confirm · Esc cancel", T.ALT, dim=True))
    elif app.flash_step == "confirm":
        lines.append(style(f"  {app.flash_status}", T.WARN, bold=True))
        lines.append("")
        lines.append(style("  This will ERASE ALL DATA on the selected drive.", T.BAD))
        lines.append("")
        lines.append(style("  y = yes, flash   Esc = cancel", T.ALT, dim=True))
    elif app.flash_step == "flashing":
        lines.append(style(f"  {app.flash_status}", T.ACCENT))
        lines.append("")
        lines.append(style("  ⏳ This may take 10-20 minutes…", T.ALT, dim=True))
    elif app.flash_step == "done":
        lines.append(style(f"  {app.flash_status}", T.GOOD, bold=True))
        lines.append("")
        lines.append(style("  Press any key to close", T.ALT, dim=True))
    else:
        lines.append(style(f"  {app.flash_status}", T.BAD))

    while len(lines) < height:
        lines.append("")
    return lines


def render_footer(app: App, width: int) -> str:
    if app.pane == PANE_DOWNLOADS:
        hints = "↑↓ move · d download · p pause · x cancel · e export · ? help · q quit"
    elif app.pane == PANE_INSTALLERS:
        hints = "↑↓ move · Enter details · d download · c catalog · f flash · S sort · e export · / filter · q quit"
    else:
        hints = "↑↓ move · Enter details · d download · S sort · e export · / filter · ? help · q quit"
    return style(hints, T.ALT, dim=True)


def render(app: App, cols: int, rows: int) -> list[str]:
    """Full frame render. Returns all screen lines."""
    lines: list[str] = []

    # header
    header = render_header(cols)
    lines.extend(header)

    # loading state
    if app.loading:
        lines.append("")
        lines.append(cell(app.loading_msg, cols, color=T.ACCENT))
        lines.append("")
        while len(lines) < rows:
            lines.append("")
        return lines

    # export prompt overlay
    if app.export_prompt:
        ep = render_export_prompt(cols, rows - len(lines))
        lines.extend(ep)
        return lines

    # flash prompt overlay
    if app.flash_prompt:
        fp = render_flash_prompt(app, cols, rows - len(lines))
        lines.extend(fp)
        return lines

    # help overlay
    if app.help:
        hp = render_help(cols, rows - len(lines))
        lines.extend(hp)
        return lines

    # detail overlay
    if app.detail is not None:
        dp = render_detail(app, cols, rows - len(lines))
        lines.extend(dp)
        return lines

    # main content: rail + table
    content_h = rows - len(lines) - 1  # footer
    rail = render_rail(app, content_h)
    main_w = cols - RAIL_W - 2

    if app.pane == PANE_FIRMWARES:
        main = render_firmware_table(app, main_w, content_h)
    elif app.pane == PANE_INSTALLERS:
        main = render_installer_table(app, main_w, content_h)
    else:
        main = render_downloads(app, main_w, content_h, app.tick)

    # merge rail + main
    for i in range(content_h):
        r = rail[i] if i < len(rail) else ""
        m = main[i] if i < len(main) else ""
        lines.append(r + "  " + m)

    # footer
    footer = render_footer(app, cols)
    lines.append(style("─" * cols, T.RULE))
    lines.append(footer)

    # status bar
    if app.status:
        lines.append(style(f"  {app.status}", T.WARN))

    # pad to rows
    while len(lines) < rows:
        lines.append("")

    return lines[:rows]
