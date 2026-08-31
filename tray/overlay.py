"""X11 recording overlay for Voxtype (GTK3 / Cairo).

Shown while the daemon is `recording` or `transcribing`: a dark rounded card
at the bottom-center of the focused window's monitor, with a mic glyph, a
scrolling peak-history waveform, and a level meter with peak-hold.

Never takes focus (GTK POPUP / override-redirect) and is click-through.
Any error disables the overlay and is reported on stderr; the tray keeps
running. Set OVERLAY_ENABLED = False to turn it off without touching the tray.
"""
from __future__ import annotations

import math
import os
import socket
import struct
import sys
import time
from collections import deque
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

# ---------------------------------------------------------------------------
# Config (module-head constants; no config-file parsing)
# ---------------------------------------------------------------------------

OVERLAY_ENABLED = True

CARD_W = 400
CARD_H = 72
CARD_MARGIN_BOTTOM = 72
CARD_RADIUS = 14
PAD = 14

GLYPH_W = 20
GLYPH_H = 28
GLYPH_GAP = 12

BAR_W = 3
BAR_GAP = 2

METER_H = 4
METER_GAP = 8
PEAK_MARK_W = 2

DISPLAY_RANGE_DB = 50.0
PEAK_FALL_DB_PER_SEC = 20.0
ZONE_YELLOW_DB = -12.0
ZONE_RED_DB = -3.0

TICK_MS = 33
RECONNECT_S = 1.0
FRAME_STRUCT = struct.Struct("<Ifff")  # u32 seq, f32 rms, f32 peak, f32 peak_db
FRAME_BYTES = FRAME_STRUCT.size

CARD_RGBA = (0.09, 0.09, 0.11, 0.92)
CARD_OPAQUE_RGBA = (0.09, 0.09, 0.11, 1.0)
WAVE_RGBA = (0.345, 0.784, 0.627, 1.0)
WAVE_HOT_RGBA = (0.914, 0.659, 0.306, 1.0)
WAVE_IDLE_RGBA = (0.306, 0.329, 0.376, 1.0)
METER_TRACK_RGBA = (1.0, 1.0, 1.0, 0.15)
METER_GREEN_RGBA = (0.345, 0.784, 0.627, 1.0)
METER_YELLOW_RGBA = (0.914, 0.78, 0.25, 1.0)
METER_RED_RGBA = (0.90, 0.27, 0.29, 1.0)
PEAK_MARK_RGBA = (0.933, 0.941, 0.961, 1.0)
MIC_RECORDING_RGB = (0.90, 0.27, 0.29)
MIC_TRANSCRIBING_RGB = (0.91, 0.66, 0.31)
HOT_LEVEL = 0.85

AUDIO_SOCK = (
    Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    / "voxtype"
    / "audio.sock"
)

CONTENT_LEFT = PAD + GLYPH_W + GLYPH_GAP
CONTENT_RIGHT = CARD_W - PAD
WAVE_TOP = PAD
WAVE_BOTTOM = CARD_H - PAD - METER_H - METER_GAP
METER_TOP = CARD_H - PAD - METER_H
METER_BOTTOM = CARD_H - PAD
GLYPH_X = PAD
GLYPH_Y = (CARD_H - GLYPH_H) / 2.0


def _wave_capacity() -> int:
    wave_w = CONTENT_RIGHT - CONTENT_LEFT
    if wave_w < BAR_W or BAR_W <= 0:
        return 0
    return (wave_w - BAR_W) // (BAR_W + BAR_GAP) + 1


WAVE_CAPACITY = _wave_capacity()


def _db_to_norm(db: float) -> float:
    if not math.isfinite(db):
        return 0.0
    return max(0.0, min(1.0, (db + DISPLAY_RANGE_DB) / DISPLAY_RANGE_DB))


def _frame_norm(peak: float, peak_db: float) -> float:
    # Linear peak <= 0 or non-finite → silence floor (never trust log10(0)).
    if not math.isfinite(peak) or peak <= 0.0:
        return 0.0
    return _db_to_norm(peak_db)


def _rounded_rect(cr: cairo.Context, x: float, y: float, w: float, h: float, r: float) -> None:
    if w <= 0 or h <= 0:
        return
    r = max(0.0, min(r, w / 2.0, h / 2.0))
    cr.new_path()
    if r <= 0:
        cr.rectangle(x, y, w, h)
        return
    cr.arc(x + w - r, y + r, r, -math.pi / 2.0, 0.0)
    cr.arc(x + w - r, y + h - r, r, 0.0, math.pi / 2.0)
    cr.arc(x + r, y + h - r, r, math.pi / 2.0, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3.0 * math.pi / 2.0)
    cr.close_path()


def _workarea() -> Gdk.Rectangle:
    """Workarea of the focused window's monitor, else the primary monitor."""
    display = Gdk.Display.get_default()
    screen = Gdk.Screen.get_default()
    monitor = None
    if screen is not None and display is not None:
        try:
            active = screen.get_active_window()
        except Exception:
            active = None
        if active is not None:
            monitor = display.get_monitor_at_window(active)
    if monitor is None and display is not None:
        monitor = display.get_primary_monitor()
    if monitor is None and display is not None and display.get_n_monitors() > 0:
        monitor = display.get_monitor(0)
    if monitor is None:
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = 0, 0, CARD_W, CARD_H
        return rect
    return monitor.get_workarea()


class Overlay:
    def __init__(self) -> None:
        self._disabled = False
        self._visible = False
        self._state: str | None = None
        self._window: Gtk.Window | None = None
        self._area: Gtk.DrawingArea | None = None
        self._tick_id: int | None = None
        self._io_id: int | None = None
        self._sock: socket.socket | None = None
        self._buf = bytearray()
        self._pending: list[float] = []
        self._history: deque[float] = deque(maxlen=WAVE_CAPACITY)
        self._meter = 0.0
        self._peak_hold = 0.0
        self._last_tick: float | None = None
        self._last_connect_at = 0.0
        self._composited = True

    def set_state(self, state: str | None) -> None:
        if self._disabled:
            return
        try:
            self._state = state
            want = state in ("recording", "transcribing")
            if want:
                if not self._visible:
                    self._show()
                elif self._area is not None:
                    self._area.queue_draw()
                if state == "recording":
                    self._ensure_socket()
            else:
                self._hide()
        except Exception as exc:
            self._disable(exc)

    def shutdown(self) -> None:
        self._hide()
        if self._window is not None:
            self._window.destroy()
            self._window = None
            self._area = None

    # --- visibility --------------------------------------------------------

    def _show(self) -> None:
        if self._window is None:
            self._build_window()
        assert self._window is not None
        self._place()
        self._history.clear()
        self._pending.clear()
        self._meter = 0.0
        self._peak_hold = 0.0
        self._buf = bytearray()
        self._last_tick = time.monotonic()
        self._last_connect_at = 0.0
        self._visible = True
        self._window.show_all()
        self._apply_click_through()
        self._start_tick()
        if self._state == "recording":
            self._ensure_socket()

    def _hide(self) -> None:
        self._visible = False
        self._stop_tick()
        self._close_socket()
        self._history.clear()
        self._pending.clear()
        self._meter = 0.0
        self._peak_hold = 0.0
        self._buf = bytearray()
        self._last_tick = None
        if self._window is not None:
            self._window.hide()

    def _disable(self, exc: BaseException) -> None:
        if self._disabled:
            return
        self._disabled = True
        print(
            f"voxtype overlay disabled: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        try:
            self.shutdown()
        except Exception:
            pass

    # --- window ------------------------------------------------------------

    def _build_window(self) -> None:
        window = Gtk.Window(type=Gtk.WindowType.POPUP)
        window.set_decorated(False)
        window.set_keep_above(True)
        window.set_accept_focus(False)
        window.set_focus_on_map(False)
        window.set_can_focus(False)
        window.set_skip_taskbar_hint(True)
        window.set_skip_pager_hint(True)
        window.set_resizable(False)
        window.set_border_width(0)
        window.set_title("voxtype-osd")
        window.set_default_size(CARD_W, CARD_H)
        window.set_size_request(CARD_W, CARD_H)

        screen = window.get_screen()
        visual = screen.get_rgba_visual() if screen is not None else None
        if visual is not None:
            window.set_visual(visual)
        window.set_app_paintable(True)
        self._composited = bool(screen is not None and screen.is_composited())

        css = Gtk.CssProvider()
        css.load_from_data(b"* { background-color: transparent; }")

        area = Gtk.DrawingArea()
        area.set_app_paintable(True)
        area.set_can_focus(False)
        area.set_size_request(CARD_W, CARD_H)
        area.connect("draw", self._on_draw)
        window.add(area)
        for widget in (window, area):
            widget.get_style_context().add_provider(
                css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

        window.connect("realize", self._on_realize_or_map)
        window.connect("map", self._on_realize_or_map)

        self._window = window
        self._area = area

    def _place(self) -> None:
        assert self._window is not None
        work = _workarea()
        x = work.x + max(0, (work.width - CARD_W) // 2)
        y = work.y + work.height - CARD_MARGIN_BOTTOM - CARD_H
        if y < work.y:
            y = work.y + max(0, work.height - CARD_H)
        self._window.resize(CARD_W, CARD_H)
        self._window.move(int(x), int(y))

    def _on_realize_or_map(self, *_args) -> None:
        try:
            self._apply_click_through()
        except Exception as exc:
            self._disable(exc)

    def _apply_click_through(self) -> None:
        if self._disabled or self._window is None:
            return
        gdk_win = self._window.get_window()
        if gdk_win is None:
            return
        # GI binding is (region, offset_x, offset_y), not region-only.
        gdk_win.input_shape_combine_region(cairo.Region(), 0, 0)
        transparent = Gdk.RGBA(red=0.0, green=0.0, blue=0.0, alpha=0.0)
        gdk_win.set_background_rgba(transparent)
        self._window.set_keep_above(True)
        gdk_win.raise_()

    # --- tick --------------------------------------------------------------

    def _start_tick(self) -> None:
        if self._tick_id is not None:
            return
        self._tick_id = GLib.timeout_add(TICK_MS, self._on_tick)

    def _stop_tick(self) -> None:
        if self._tick_id is not None:
            GLib.source_remove(self._tick_id)
            self._tick_id = None

    def _on_tick(self) -> bool:
        try:
            if not self._visible or self._disabled:
                self._tick_id = None
                return False
            now = time.monotonic()
            elapsed = 0.0 if self._last_tick is None else now - self._last_tick
            self._last_tick = now

            if self._pending:
                bar = max(self._pending)
                self._meter = self._pending[-1]
                self._pending.clear()
            elif self._state == "recording":
                # Frame jitter vs. 33 ms tick: hold the last level instead of
                # flashing silence while still recording.
                self._meter *= 0.6
                bar = self._meter
            else:
                bar = 0.0
                self._meter = 0.0
            self._history.append(bar)

            if elapsed > 0.0 and math.isfinite(elapsed):
                fall = (PEAK_FALL_DB_PER_SEC / DISPLAY_RANGE_DB) * elapsed
                self._peak_hold = max(0.0, self._peak_hold - fall)
            self._peak_hold = max(self._peak_hold, bar)

            if self._state == "recording" and self._sock is None:
                self._ensure_socket()

            if self._area is not None:
                self._area.queue_draw()
            return True
        except Exception as exc:
            self._disable(exc)
            return False

    # --- audio socket ------------------------------------------------------

    def _ensure_socket(self) -> None:
        if self._sock is not None or self._disabled:
            return
        now = time.monotonic()
        if now - self._last_connect_at < RECONNECT_S:
            return
        self._last_connect_at = now
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            sock.connect(os.fspath(AUDIO_SOCK))
        except BlockingIOError:
            # Watch is IO_IN-only; an in-progress socket would hang mute and
            # the 1 s reconnect would never fire. Retry on the next throttle.
            sock.close()
            return
        except OSError:
            sock.close()
            return
        self._sock = sock
        self._buf = bytearray()
        self._io_id = GLib.io_add_watch(
            sock,
            GLib.PRIORITY_DEFAULT,
            GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR | GLib.IO_NVAL,
            self._on_io,
        )

    def _close_socket(self, *, remove_watch: bool = True) -> None:
        if remove_watch and self._io_id is not None:
            try:
                GLib.source_remove(self._io_id)
            except Exception:
                pass
        self._io_id = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._buf = bytearray()

    def _on_io(self, _channel, condition) -> bool:
        try:
            if self._disabled or self._sock is None:
                self._io_id = None
                return False
            eof = bool(condition & (GLib.IO_HUP | GLib.IO_ERR | GLib.IO_NVAL))
            if condition & (GLib.IO_IN | GLib.IO_HUP):
                try:
                    data = self._sock.recv(4096)
                except BlockingIOError:
                    data = None
                except OSError:
                    self._close_socket(remove_watch=False)
                    return False
                if data:
                    self._buf.extend(data)
                    while len(self._buf) >= FRAME_BYTES:
                        chunk = bytes(self._buf[:FRAME_BYTES])
                        del self._buf[:FRAME_BYTES]
                        try:
                            _seq, _rms, peak, peak_db = FRAME_STRUCT.unpack(chunk)
                        except struct.error:
                            continue
                        self._pending.append(_frame_norm(peak, peak_db))
                    if len(self._pending) > 256:
                        self._pending = self._pending[-128:]
                elif data == b"":
                    eof = True
            if eof:
                self._close_socket(remove_watch=False)
                return False
            return True
        except Exception as exc:
            self._disable(exc)
            return False

    # --- drawing -----------------------------------------------------------

    def _on_draw(self, _widget, cr: cairo.Context) -> bool:
        try:
            screen = self._window.get_screen() if self._window is not None else None
            self._composited = bool(screen is not None and screen.is_composited())
            self._draw_card(cr)
        except Exception as exc:
            self._disable(exc)
        return True

    def _draw_card(self, cr: cairo.Context) -> None:
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        card = CARD_RGBA if self._composited else CARD_OPAQUE_RGBA
        if self._composited:
            _rounded_rect(cr, 0.0, 0.0, float(CARD_W), float(CARD_H), float(CARD_RADIUS))
            cr.set_source_rgba(*card)
            cr.fill()
        else:
            cr.set_source_rgba(*card)
            cr.rectangle(0.0, 0.0, float(CARD_W), float(CARD_H))
            cr.fill()

        tint = (
            MIC_TRANSCRIBING_RGB
            if self._state == "transcribing"
            else MIC_RECORDING_RGB
        )
        _draw_mic(cr, GLYPH_X, GLYPH_Y, float(GLYPH_W), float(GLYPH_H), tint)
        _draw_waveform(cr, self._history)
        _draw_meter(cr, self._meter, self._peak_hold)


def _draw_mic(
    cr: cairo.Context, x: float, y: float, w: float, h: float, rgb: tuple[float, float, float]
) -> None:
    cx = x + w / 2.0
    cr.set_source_rgb(*rgb)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    stroke = max(1.0, w * 0.09)
    cr.set_line_width(stroke)

    capsule_w = max(2.0, w * 0.46)
    capsule_h = max(3.0, h * 0.52)
    _rounded_rect(cr, cx - capsule_w / 2.0, y, capsule_w, capsule_h, capsule_w / 2.0)
    cr.fill()

    arc_r = max(2.0, w * 0.36)
    arc_cy = y + capsule_h - capsule_w * 0.35
    cr.arc(cx, arc_cy, arc_r, 0.0, math.pi)
    cr.stroke()

    stand_top = arc_cy + arc_r
    foot_h = stroke
    foot_y = y + h - foot_h / 2.0
    if foot_y > stand_top:
        cr.move_to(cx, stand_top)
        cr.line_to(cx, foot_y)
        cr.stroke()

    foot_w = w * 0.5
    cr.move_to(cx - foot_w / 2.0, y + h - foot_h / 2.0)
    cr.line_to(cx + foot_w / 2.0, y + h - foot_h / 2.0)
    cr.stroke()


def _draw_waveform(cr: cairo.Context, history: deque[float]) -> None:
    wave_h = float(WAVE_BOTTOM - WAVE_TOP)
    if wave_h <= 0 or WAVE_CAPACITY <= 0:
        return
    pitch = BAR_W + BAR_GAP
    mid = WAVE_TOP + wave_h / 2.0
    shown = min(len(history), WAVE_CAPACITY)
    for index in range(shown):
        norm = history[len(history) - 1 - index]
        right = CONTENT_RIGHT - index * pitch
        left = right - BAR_W
        if left < CONTENT_LEFT:
            break
        bar_h = max(1.0, round(norm * wave_h))
        top = mid - bar_h / 2.0
        if not math.isfinite(norm) or norm <= 0.0:
            color = WAVE_IDLE_RGBA
        elif norm >= HOT_LEVEL:
            color = WAVE_HOT_RGBA
        else:
            color = WAVE_RGBA
        cr.set_source_rgba(*color)
        cr.rectangle(float(left), float(top), float(BAR_W), float(bar_h))
        cr.fill()


def _draw_meter(cr: cairo.Context, level: float, peak: float) -> None:
    x = float(CONTENT_LEFT)
    y = float(METER_TOP)
    w = float(CONTENT_RIGHT - CONTENT_LEFT)
    h = float(METER_H)
    if w <= 0 or h <= 0:
        return
    radius = h / 2.0
    _rounded_rect(cr, x, y, w, h, radius)
    cr.set_source_rgba(*METER_TRACK_RGBA)
    cr.fill()

    level = max(0.0, min(1.0, level if math.isfinite(level) else 0.0))
    filled = w * level
    if filled > 0.0:
        cr.save()
        _rounded_rect(cr, x, y, w, h, radius)
        cr.clip()
        cr.rectangle(x, y, filled, h)
        cr.clip()
        yellow_x = x + w * _db_to_norm(ZONE_YELLOW_DB)
        red_x = x + w * _db_to_norm(ZONE_RED_DB)
        cr.set_source_rgba(*METER_GREEN_RGBA)
        cr.rectangle(x, y, yellow_x - x, h)
        cr.fill()
        cr.set_source_rgba(*METER_YELLOW_RGBA)
        cr.rectangle(yellow_x, y, max(0.0, red_x - yellow_x), h)
        cr.fill()
        cr.set_source_rgba(*METER_RED_RGBA)
        cr.rectangle(red_x, y, max(0.0, x + w - red_x), h)
        cr.fill()
        cr.restore()

    peak = max(0.0, min(1.0, peak if math.isfinite(peak) else 0.0))
    if peak > 0.0:
        mark_w = float(PEAK_MARK_W)
        px = x + w * peak
        left = min(max(px - mark_w / 2.0, x), x + w - mark_w)
        cr.set_source_rgba(*PEAK_MARK_RGBA)
        cr.rectangle(left, y, mark_w, h)
        cr.fill()


_instance: Overlay | None = None
_failed = False


def set_state(state: str | None) -> None:
    """Update overlay visibility from the daemon state. Never raises."""
    global _instance, _failed
    if not OVERLAY_ENABLED or _failed:
        return
    try:
        if _instance is None:
            _instance = Overlay()
        _instance.set_state(state)
    except Exception as exc:
        _failed = True
        print(
            f"voxtype overlay disabled: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        if _instance is not None:
            try:
                _instance.shutdown()
            except Exception:
                pass
            _instance = None
