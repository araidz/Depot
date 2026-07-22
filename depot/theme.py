"""The single look knob: palette, glyphs, logo, and the sheen/ramp math.

Amber/gold palette for Depot — the warmth of "golden installer" against the
violet of Trawl. Change the constants here; nothing else touches color.
"""

from __future__ import annotations

import math

# -- palette (amber/gold) ----------------------------------------------------
ACCENT = "#f0a050"
TEXT = "#f5efe6"
ALT = "#d4b896"
GOOD = "#86d6a2"
WARN = "#f0c560"
BAD = "#ee7d92"
BRIGHT = "#ffd080"
RULE = "#7a6e5e"
PAUSED = "#8a8070"
DEEP = "#b07020"
SHEEN_PEAK = "#fff8f0"
WHITE = "#ffffff"
SHADE = "#6a4a10"

# -- glyphs ------------------------------------------------------------------
PTR = "❯"
DONE = "✓"
ERR = "✗"
DOWN = "↓"
UP = "↑"
BAR = "▌"
DOT = "·"
BLOCK = "█"
TRACK = "░"

# -- logo -------------------------------------------------------------------
LOGO_LINES: list[str] = [
    "▄▀▄ █▄▀ █   █▀▄ ▀▄▀",
    "█▀█ █ █ █▄▄ █▀▄  █ ",
]

# -- color math --------------------------------------------------------------


def _rgb(h: str) -> tuple[int, int, int]:
    n = int(h[1:], 16)
    return (n >> 16) & 255, (n >> 8) & 255, n & 255


def lerp_hex(a: str, b: str, t: float) -> str:
    ar, ag, ab = _rgb(a)
    br, bg, bb = _rgb(b)
    t = max(0.0, min(1.0, t))
    c = lambda x, y: round(x + (y - x) * t)
    return f"#{c(ar, br):02x}{c(ag, bg):02x}{c(ab, bb):02x}"


def progress_ramp(t: float, deep: str, mid: str, bright: str) -> str:
    return lerp_hex(deep, mid, t / 0.5) if t <= 0.5 else lerp_hex(mid, bright, (t - 0.5) / 0.5)


# -- sheen (cosine-bell sweep) ------------------------------------------------
SHEEN_RADIUS = 4.5
SHEEN_GAP = 8
SHEEN_SPEED = 0.45
SHEEN_MAX = 0.9
SHEEN_TICK_MS = 40


def sheen_period(width: int) -> int:
    return math.ceil(width + SHEEN_RADIUS * 2) + SHEEN_GAP


def sheen_center(tick: float, period: int) -> float:
    return (tick * SHEEN_SPEED) % period - SHEEN_RADIUS


def sheen_intensity(i: int, center: float) -> float:
    d = abs(i - center)
    if d >= SHEEN_RADIUS:
        return 0.0
    return 0.5 * (1 + math.cos(math.pi * d / SHEEN_RADIUS)) * SHEEN_MAX
