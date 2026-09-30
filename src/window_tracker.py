# -*- coding: utf-8 -*-
"""Win32 window discovery / tracking / hotzone geometry for TokenHub v3.

Lane B (window_tracker): frozen contract per spec appendix A.

Design notes:
- Every ctypes call declares explicit restype/argtypes so 64-bit handles are
  not truncated.
- Public entry points never raise: on any Win32 failure they return None/False
  (or a safe fallback value documented per method).
- DPI: coordinates are physical pixels once the process is PerMonitorV2 aware;
  call `set_dpi_awareness()` once at startup (main.py) before first use.
- Pure geometry (in_hotzone / bar_geometry with an injected work_area) is
  unit-testable without real windows.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Executable name (case-insensitive) of the desktop opencode window to track.
#: Configurable by reassigning this module constant before use.
PROCESS_NAME = "OpenCode.exe"

# ---------------------------------------------------------------------------
# Win32 plumbing
# ---------------------------------------------------------------------------

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == (HANDLE)(ULONG_PTR)-4
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MONITOR_DEFAULTTONEAREST = 2

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


# SetProcessDpiAwarenessContext exists on Windows 10 1703+; guard so the
# module still imports (and set_dpi_awareness stays a silent no-op) elsewhere.
_set_dpi_awareness_context_fn = getattr(_user32, "SetProcessDpiAwarenessContext", None)
if _set_dpi_awareness_context_fn is not None:
    _set_dpi_awareness_context_fn.restype = wintypes.BOOL
    _set_dpi_awareness_context_fn.argtypes = [ctypes.c_void_p]

_user32.EnumWindows.restype = wintypes.BOOL
_user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]

_user32.IsWindow.restype = wintypes.BOOL
_user32.IsWindow.argtypes = [wintypes.HWND]

_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [wintypes.HWND]

_user32.IsIconic.restype = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]

_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

_user32.GetWindowRect.restype = wintypes.BOOL
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

_user32.GetCursorPos.restype = wintypes.BOOL
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]

_user32.MonitorFromRect.restype = wintypes.HMONITOR
_user32.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]

_user32.MonitorFromPoint.restype = wintypes.HMONITOR
_user32.MonitorFromPoint.argtypes = [ctypes.POINTER(wintypes.POINT), wintypes.DWORD]

_user32.GetMonitorInfoW.restype = wintypes.BOOL
_user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]

_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]


def set_dpi_awareness() -> None:
    """Set PerMonitorV2 DPI awareness for this process.

    Called once at startup; any failure (unsupported API, already set,
    service session, ...) is silently ignored.
    """
    try:
        if _set_dpi_awareness_context_fn is None:
            return
        _set_dpi_awareness_context_fn(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rect:
    """Axis-aligned rectangle in physical screen pixels (left < right, top < bottom)."""

    left: int
    top: int
    right: int
    bottom: int

    def width(self) -> int:
        return self.right - self.left

    def height(self) -> int:
        return self.bottom - self.top

    def center_x(self) -> int:
        return (self.left + self.right) // 2


def _clamp(v: int, lo: int, hi: int) -> int:
    """Clamp v into [lo, hi]; an empty range (hi < lo) resolves to lo."""
    if hi < lo:
        return lo
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


# ---------------------------------------------------------------------------
# Window tracker
# ---------------------------------------------------------------------------


class WindowTracker:
    """Finds and tracks the desktop opencode window via the Win32 API.

    All methods are safe: failures return None/False instead of raising.
    """

    # -- discovery ----------------------------------------------------------

    def find_opencode_window(self) -> int | None:
        """Return the hwnd of the largest visible window owned by PROCESS_NAME.

        Enumerates top-level windows, keeps those that are visible and whose
        process image name matches PROCESS_NAME (case-insensitive), and
        returns the one with the largest area (multi-window processes, e.g.
        hidden splash + main window, resolve to the main window).
        Returns None when nothing matches or on API failure.
        """
        best_hwnd: int | None = None
        best_area = -1

        def _cb(hwnd, _lparam):
            nonlocal best_hwnd, best_area
            try:
                if not _user32.IsWindowVisible(hwnd):
                    return True
                pid = self._pid_of(hwnd)
                if pid is None or not self._pid_matches_name(pid):
                    return True
                rect = self.get_rect(hwnd)
                if rect is None:
                    return True
                area = rect.width() * rect.height()
                if area > best_area:
                    best_area = area
                    best_hwnd = int(hwnd)
            except Exception:
                pass
            return True  # keep enumerating

        try:
            if not _user32.EnumWindows(_WNDENUMPROC(_cb), 0):
                return None
        except Exception:
            return None
        return best_hwnd

    # -- state ---------------------------------------------------------------

    def is_valid(self, hwnd: int) -> bool:
        """True if hwnd is an alive, visible, non-minimized top-level window."""
        try:
            h = ctypes.c_void_p(hwnd)
            return bool(
                _user32.IsWindow(h)
                and _user32.IsWindowVisible(h)
                and not _user32.IsIconic(h)
            )
        except Exception:
            return False

    # -- geometry -------------------------------------------------------------

    def get_rect(self, hwnd: int) -> Rect | None:
        """Screen rect of hwnd in physical pixels; None on failure."""
        try:
            r = wintypes.RECT()
            if not _user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r)):
                return None
            return Rect(r.left, r.top, r.right, r.bottom)
        except Exception:
            return None

    def get_cursor_pos(self) -> tuple[int, int] | None:
        """Cursor position in physical screen pixels; None on failure."""
        try:
            p = wintypes.POINT()
            if not _user32.GetCursorPos(ctypes.byref(p)):
                return None
            return (p.x, p.y)
        except Exception:
            return None

    # -- hotzone ---------------------------------------------------------------

    def in_hotzone(
        self, rect: Rect, h: int, width_ratio: float, x: int, y: int, offset: int = 0
    ) -> bool:
        """True when (x, y) is inside the hotzone strip of `rect`.

        `offset` shifts the strip down from the window's top edge (by default 0,
        the strip starts at the very top edge). This lets the hotzone sit below
        the opencode session-tab row instead of overlapping it.

        Boundary contract (kept identical to the unit tests):
          x in [center_x - rect.width()*width_ratio/2, center_x + rect.width()*width_ratio/2]
              -> INCLUSIVE on both edges (exact float comparison).
          y in [rect.top + offset, rect.top + offset + h)
              -> INCLUSIVE at the top, EXCLUSIVE at the top + h.
        A non-positive h yields an empty zone (always False).
        """
        half = rect.width() * width_ratio / 2.0
        cx = rect.center_x()
        return (cx - half <= x <= cx + half) and (
            rect.top + offset <= y < rect.top + offset + h
        )

    def bar_geometry(
        self,
        rect: Rect,
        bar_w: int,
        bar_h: int,
        visible: bool,
        offset: int = 0,
        work_area: Rect | None = None,
    ) -> tuple[int, int]:
        """Position (x, y) for a bar of size bar_w x bar_h attached to `rect`.

        x: starts centered on the window (center_x - bar_w//2), is clamped
           into [rect.left, rect.right - bar_w] (an empty range resolves to
           rect.left), then clamped into the monitor work area of the display
           under the window ([work_area.left, work_area.right - bar_w]; an
           empty range resolves to work_area.left).
        y: rect.top + offset when visible (the bar expands below the top edge,
           e.g. below the session-tab row). When hidden the bar must be FULLY
           OFF-SCREEN, so y = work_area.top - bar_h (above the top edge of the
           work area of the monitor under the window) -- NOT rect.top - bar_h,
           which would leave the bar visibly "hanging" above a window that is
           not at the top of the screen. If the work area cannot be resolved
           (monitor query failed and none was injected), falls back to
           rect.top - bar_h so the bar never ends up lower than the old
           behavior.

        `work_area` is injectable for pure-geometry tests; when None the real
        monitor containing `rect` is queried (MonitorFromRect +
        GetMonitorInfoW). Never raises; falls back to the window-only clamp
        when the monitor query fails.
        """
        x = rect.center_x() - bar_w // 2
        x = _clamp(x, rect.left, rect.right - bar_w)
        if work_area is None:
            work_area = self._monitor_work_area(rect)
        if work_area is not None:
            x = _clamp(x, work_area.left, work_area.right - bar_w)
        if visible:
            y = rect.top + offset
        elif work_area is not None:
            y = work_area.top - bar_h
        else:
            y = rect.top - bar_h
        return (x, y)

    # -- internals --------------------------------------------------------------

    @staticmethod
    def _pid_of(hwnd) -> int | None:
        try:
            pid = wintypes.DWORD()
            if not _user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid)):
                return None
            return pid.value if pid.value else None
        except Exception:
            return None

    @staticmethod
    def _pid_matches_name(pid: int) -> bool:
        try:
            h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                buf = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(32768)
                if not _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    return False
                return os.path.basename(buf.value).lower() == PROCESS_NAME.lower()
            finally:
                _kernel32.CloseHandle(h)
        except Exception:
            return False

    @staticmethod
    def _monitor_work_area(rect: Rect) -> Rect | None:
        """Work area (excludes taskbar) of the monitor under `rect`; None on failure."""
        try:
            mr = wintypes.RECT(rect.left, rect.top, rect.right, rect.bottom)
            hm = _user32.MonitorFromRect(ctypes.byref(mr), _MONITOR_DEFAULTTONEAREST)
            if not hm:
                return None
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if not _user32.GetMonitorInfoW(hm, ctypes.byref(mi)):
                return None
            w = mi.rcWork
            return Rect(w.left, w.top, w.right, w.bottom)
        except Exception:
            return None


def get_work_area(x: int, y: int) -> Rect | None:
    """Work area (excludes taskbar) of the monitor containing point (x, y).

    Public helper for positioning child windows (dialogs) on the same display
    as their anchor — mixed-DPI multi-monitor setups must never fall back to
    the primary-screen-centered math. Physical pixels; None on failure.
    """
    try:
        p = wintypes.POINT(x, y)
        hm = _user32.MonitorFromPoint(ctypes.byref(p), _MONITOR_DEFAULTTONEAREST)
        if not hm:
            return None
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not _user32.GetMonitorInfoW(hm, ctypes.byref(mi)):
            return None
        w = mi.rcWork
        return Rect(w.left, w.top, w.right, w.bottom)
    except Exception:
        return None