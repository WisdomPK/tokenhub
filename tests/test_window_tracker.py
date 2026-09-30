# -*- coding: utf-8 -*-
"""Pure-geometry unit tests for src/window_tracker.py (no real windows needed)."""
import os
import sys
import unittest
from unittest import mock

# Make `src` importable regardless of how the runner invokes us
# (both `PY -m unittest tests.test_window_tracker` and
#  `PY -m unittest discover -s tests` from the project root).
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)

from window_tracker import PROCESS_NAME, Rect, WindowTracker, set_dpi_awareness


class RectTest(unittest.TestCase):
    def test_width_height_center(self):
        r = Rect(100, 50, 500, 350)
        self.assertEqual(r.width(), 400)
        self.assertEqual(r.height(), 300)
        self.assertEqual(r.center_x(), 300)
        self.assertEqual((r.left, r.top, r.right, r.bottom), (100, 50, 500, 350))

    def test_odd_width_center_floors(self):
        # (left+right)//2 with odd span -> floor
        self.assertEqual(Rect(0, 0, 801, 600).center_x(), 400)
        self.assertEqual(Rect(0, 0, 799, 600).center_x(), 399)

    def test_frozen(self):
        r = Rect(0, 0, 10, 10)
        with self.assertRaises(AttributeError):
            r.left = 5  # dataclass(frozen=True) -> FrozenInstanceError (AttributeError subclass)


class HotzoneTest(unittest.TestCase):
    """in_hotzone boundary contract (must match docstring exactly):

    x is INCLUSIVE on both edges:  [center_x - w*ratio/2, center_x + w*ratio/2]
    y is INCLUSIVE at top, EXCLUSIVE at top+h:  [rect.top, rect.top + h)
    """

    def setUp(self):
        self.t = WindowTracker()
        # width 800, center 400, ratio 0.5 -> half = 200 -> x in [200, 600]
        self.rect = Rect(0, 0, 800, 600)

    def test_center_hit(self):
        self.assertTrue(self.t.in_hotzone(self.rect, 10, 0.5, 400, 5))

    def test_x_boundaries_inclusive(self):
        self.assertTrue(self.t.in_hotzone(self.rect, 10, 0.5, 200, 5))  # left edge, inclusive
        self.assertTrue(self.t.in_hotzone(self.rect, 10, 0.5, 600, 5))  # right edge, inclusive

    def test_x_outside(self):
        self.assertFalse(self.t.in_hotzone(self.rect, 10, 0.5, 199, 5))
        self.assertFalse(self.t.in_hotzone(self.rect, 10, 0.5, 601, 5))

    def test_y_boundaries(self):
        self.assertTrue(self.t.in_hotzone(self.rect, 10, 0.5, 400, 0))    # top, inclusive
        self.assertTrue(self.t.in_hotzone(self.rect, 10, 0.5, 400, 9))    # inside
        self.assertFalse(self.t.in_hotzone(self.rect, 10, 0.5, 400, 10))  # top+h, exclusive
        self.assertFalse(self.t.in_hotzone(self.rect, 10, 0.5, 400, -1))  # above window

    def test_ratio_zero_is_center_line(self):
        self.assertTrue(self.t.in_hotzone(self.rect, 10, 0.0, 400, 5))
        self.assertFalse(self.t.in_hotzone(self.rect, 10, 0.0, 401, 5))

    def test_ratio_one_is_full_width(self):
        self.assertTrue(self.t.in_hotzone(self.rect, 10, 1.0, 0, 5))
        self.assertTrue(self.t.in_hotzone(self.rect, 10, 1.0, 799, 5))

    def test_odd_width_float_boundary(self):
        # width 801 -> half = 200.25 -> x in [199.75, 600.25]
        r = Rect(0, 0, 801, 600)
        self.assertTrue(self.t.in_hotzone(r, 10, 0.5, 200, 5))
        self.assertTrue(self.t.in_hotzone(r, 10, 0.5, 600, 5))
        self.assertFalse(self.t.in_hotzone(r, 10, 0.5, 199, 5))
        self.assertFalse(self.t.in_hotzone(r, 10, 0.5, 601, 5))

    def test_zero_height_is_empty(self):
        self.assertFalse(self.t.in_hotzone(self.rect, 0, 0.5, 400, 0))

    def test_offset_zero_matches_legacy(self):
        # offset=0 必须与旧行为完全一致：y in [top, top+h)
        self.assertTrue(self.t.in_hotzone(self.rect, 10, 0.5, 400, 0, offset=0))
        self.assertTrue(self.t.in_hotzone(self.rect, 10, 0.5, 400, 9, offset=0))
        self.assertFalse(self.t.in_hotzone(self.rect, 10, 0.5, 400, 10, offset=0))

    def test_offset_shifted_band(self):
        # offset=48 -> y in [48, 58)
        self.assertFalse(self.t.in_hotzone(self.rect, 10, 0.5, 400, 47, offset=48))  # 带上方
        self.assertTrue(self.t.in_hotzone(self.rect, 10, 0.5, 400, 48, offset=48))   # 上边界含
        self.assertTrue(self.t.in_hotzone(self.rect, 10, 0.5, 400, 57, offset=48))   # 带内
        self.assertFalse(self.t.in_hotzone(self.rect, 10, 0.5, 400, 58, offset=48))  # 带下方
        # 原顶边位置不再命中
        self.assertFalse(self.t.in_hotzone(self.rect, 10, 0.5, 400, 0, offset=48))

    def test_offset_horizontal_unchanged(self):
        # 水平判定不受 offset 影响：x 边界仍 inclusive
        self.assertTrue(self.t.in_hotzone(self.rect, 10, 0.5, 200, 50, offset=48))
        self.assertTrue(self.t.in_hotzone(self.rect, 10, 0.5, 600, 50, offset=48))
        self.assertFalse(self.t.in_hotzone(self.rect, 10, 0.5, 199, 50, offset=48))

    def test_offset_non_positive_height_is_empty(self):
        self.assertFalse(self.t.in_hotzone(self.rect, 0, 0.5, 400, 48, offset=48))


class BarGeometryTest(unittest.TestCase):
    """bar_geometry contract (must match docstring exactly):

    x = center_x - bar_w//2, clamped to [rect.left, rect.right - bar_w]
        (empty range -> rect.left), then clamped to the monitor work area
        [work_area.left, work_area.right - bar_w] (empty range -> work_area.left).
    y = rect.top + offset when visible, work_area.top - bar_h when hidden
        (fully off-screen above the work area; falls back to rect.top - bar_h
        when the work area cannot be resolved).
    work_area is injectable for pure-geometry testing; when None the real
    monitor under the rect is queried via Win32.
    """

    def setUp(self):
        self.t = WindowTracker()
        # width 800, center 500
        self.rect = Rect(100, 200, 900, 800)
        # a work area wide enough to never interfere with the window clamp
        self.wa = Rect(0, 0, 4000, 2000)

    def test_visible_centered(self):
        self.assertEqual(self.t.bar_geometry(self.rect, 300, 40, True, work_area=self.wa), (350, 200))

    def test_hidden(self):
        # 隐藏位完全离屏：work_area.top(0) - bar_h = -40（不再是 rect.top - bar_h = 160）
        self.assertEqual(self.t.bar_geometry(self.rect, 300, 40, False, work_area=self.wa), (350, -40))

    def test_visible_param_only_affects_y(self):
        self.assertEqual(
            self.t.bar_geometry(self.rect, 300, 40, True, work_area=self.wa)[0],
            self.t.bar_geometry(self.rect, 300, 40, False, work_area=self.wa)[0],
        )

    def test_odd_bar_width_floors_center(self):
        # center 500, bar 301 -> 500 - 150 = 350
        self.assertEqual(self.t.bar_geometry(self.rect, 301, 40, True, work_area=self.wa), (350, 200))

    def test_window_narrower_than_bar_clamps_left(self):
        # width 200 < bar 300 -> x pinned to rect.left
        r = Rect(100, 200, 300, 800)
        self.assertEqual(self.t.bar_geometry(r, 300, 40, True, work_area=self.wa), (100, 200))
        self.assertEqual(self.t.bar_geometry(r, 300, 40, False, work_area=self.wa), (100, -40))

    def test_window_narrower_than_bar_clamps_left_other_side(self):
        # width 200 < bar 300, window far right -> still pinned to rect.left
        r = Rect(1000, 200, 1200, 800)
        self.assertEqual(self.t.bar_geometry(r, 300, 40, True, work_area=self.wa), (1000, 200))

    def test_work_area_clamps_right(self):
        # window hugs right edge; work area ends at 1000 -> x forced to 700
        r = Rect(800, 0, 900, 600)
        wa = Rect(0, 0, 1000, 900)
        self.assertEqual(self.t.bar_geometry(r, 300, 40, True, work_area=wa), (700, 0))

    def test_work_area_clamps_left(self):
        # window sticks out of the work area's left edge -> x forced to 200
        r = Rect(100, 0, 200, 600)
        wa = Rect(200, 0, 1200, 900)
        self.assertEqual(self.t.bar_geometry(r, 300, 40, True, work_area=wa), (200, 0))

    def test_work_area_narrower_than_bar(self):
        # work area width 200 < bar 300 -> x pinned to work_area.left
        r = Rect(100, 0, 900, 600)
        wa = Rect(0, 0, 200, 900)
        self.assertEqual(self.t.bar_geometry(r, 300, 40, True, work_area=wa), (0, 0))

    def test_visible_with_offset(self):
        # offset=48 -> visible y = rect.top + 48
        self.assertEqual(
            self.t.bar_geometry(self.rect, 300, 40, True, offset=48, work_area=self.wa),
            (350, 248),
        )

    def test_hidden_ignores_offset(self):
        # hidden 位置不受 offset 影响：仍为 work_area.top - bar_h（完全离屏）
        self.assertEqual(
            self.t.bar_geometry(self.rect, 300, 40, False, offset=48, work_area=self.wa),
            (350, -40),
        )
        self.assertEqual(
            self.t.bar_geometry(self.rect, 300, 40, False, offset=0, work_area=self.wa),
            (350, -40),
        )

    def test_visible_offset_zero_matches_legacy(self):
        self.assertEqual(
            self.t.bar_geometry(self.rect, 300, 40, True, offset=0, work_area=self.wa),
            (350, 200),
        )

    def test_hidden_negative_work_area_top(self):
        # 副屏位于主屏上方：work_area.top = -1080 → hidden y = -1080 - 40 = -1120
        wa = Rect(0, -1080, 4000, 0)
        self.assertEqual(self.t.bar_geometry(self.rect, 300, 40, False, work_area=wa), (350, -1120))

    def test_hidden_positive_work_area_top(self):
        # work_area.top = 100 → hidden y = 100 - 40 = 60（完全在屏幕上方，仍离屏）
        wa = Rect(0, 100, 4000, 1200)
        self.assertEqual(self.t.bar_geometry(self.rect, 300, 40, False, work_area=wa), (350, 60))

    def test_visible_unaffected_by_work_area_top(self):
        # visible y 只看 rect.top + offset，不受 work_area.top 影响
        wa = Rect(0, 100, 4000, 1200)
        self.assertEqual(
            self.t.bar_geometry(self.rect, 300, 40, True, offset=48, work_area=wa),
            (350, 248),
        )

    def test_hidden_offscreen_when_window_not_at_top(self):
        # 窗口不在屏幕顶部（rect.top=300）：hidden 必须离屏（-40），
        # 不能掉回 rect.top - bar_h（260，会"挂"在窗口上方可见）
        r = Rect(100, 300, 900, 800)
        self.assertEqual(self.t.bar_geometry(r, 300, 40, False, work_area=self.wa), (350, -40))
        self.assertEqual(self.t.bar_geometry(r, 300, 40, True, offset=48, work_area=self.wa), (350, 348))

    def test_hidden_falls_back_to_rect_top_when_work_area_unresolvable(self):
        # work_area 解析失败（未注入且 monitor 查询失败）→ 回退 rect.top - bar_h（不更糟）
        with mock.patch.object(self.t, "_monitor_work_area", return_value=None):
            self.assertEqual(self.t.bar_geometry(self.rect, 300, 40, False), (350, 160))


class ConstantsTest(unittest.TestCase):
    def test_process_name_default(self):
        self.assertEqual(PROCESS_NAME, "OpenCode.exe")

    def test_set_dpi_awareness_never_raises(self):
        set_dpi_awareness()  # silent no-op on failure is the contract


if __name__ == "__main__":
    unittest.main()