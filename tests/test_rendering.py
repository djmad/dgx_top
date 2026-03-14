import unittest

from dgxtop.app import (
    HISTORY_WINDOW_STEPS,
    DgxTopApp,
    build_name_cell_text,
    build_history_window_steps,
    build_timeline_series,
    clamp_column_content_width,
    fmt_bytes,
    normalize_series,
    next_history_window,
    previous_history_window,
    render_tall_chart,
    render_two_line_chart,
    scale_chart_values,
)
from dgxtop.models import EntityRow, ProcessInfo
from dgxtop.models import HistoryPoint


class RenderingTests(unittest.TestCase):
    def test_fmt_bytes(self):
        self.assertEqual(fmt_bytes(None), "--")
        self.assertEqual(fmt_bytes(0), "0B")
        self.assertEqual(fmt_bytes(1024), "1.0K")
        self.assertEqual(fmt_bytes(1024 * 1024), "1.0M")

    def test_normalize_series_returns_requested_width(self):
        values = normalize_series([0.0, 50.0, 100.0], 12)
        self.assertEqual(len(values), 12)

    def test_normalize_series_leaves_missing_history_blank(self):
        values = normalize_series([25.0, 50.0], 8)
        self.assertEqual(values[:6], [None] * 6)
        self.assertEqual(values[6:], [25.0, 50.0])

    def test_build_timeline_series_virtually_prefills_missing_history(self):
        points = [
            HistoryPoint(timestamp=90.0, cpu_percent=20.0, ram_percent=0.0, gpu_percent=None, gpu_memory_percent=None),
            HistoryPoint(timestamp=95.0, cpu_percent=40.0, ram_percent=0.0, gpu_percent=None, gpu_memory_percent=None),
            HistoryPoint(timestamp=100.0, cpu_percent=60.0, ram_percent=0.0, gpu_percent=None, gpu_memory_percent=None),
        ]

        series = build_timeline_series(points, 10, 100, 100.0, lambda point: point.cpu_percent)

        self.assertEqual(series[:9], [None] * 9)
        self.assertEqual(series[9], 60.0)

    def test_build_timeline_series_interpolates_small_internal_gaps(self):
        points = [
            HistoryPoint(timestamp=10.0, cpu_percent=20.0, ram_percent=0.0, gpu_percent=None, gpu_memory_percent=None),
            HistoryPoint(timestamp=14.0, cpu_percent=60.0, ram_percent=0.0, gpu_percent=None, gpu_memory_percent=None),
        ]

        series = build_timeline_series(points, 10, 20, 20.0, lambda point: point.cpu_percent)

        self.assertEqual(series[5], 20.0)
        self.assertEqual(series[6], 40.0)
        self.assertEqual(series[7], 60.0)

    def test_build_timeline_series_uses_stable_compressed_buckets(self):
        points = [
            HistoryPoint(timestamp=90.0, cpu_percent=20.0, ram_percent=0.0, gpu_percent=None, gpu_memory_percent=None),
            HistoryPoint(timestamp=95.0, cpu_percent=60.0, ram_percent=0.0, gpu_percent=None, gpu_memory_percent=None),
        ]

        first = build_timeline_series(points, 10, 100, 100.1, lambda point: point.cpu_percent)
        second = build_timeline_series(points, 10, 100, 109.9, lambda point: point.cpu_percent)

        self.assertEqual(first, second)

    def test_history_window_steps_snap_to_exact_days(self):
        steps = build_history_window_steps(60, 14 * 24 * 60 * 60)
        self.assertIn(24 * 60 * 60, steps)
        self.assertIn(2 * 24 * 60 * 60, steps)
        self.assertIn(4 * 24 * 60 * 60, steps)
        self.assertIn(8 * 24 * 60 * 60, steps)
        self.assertIn(14 * 24 * 60 * 60, steps)
        self.assertNotIn(32 * 60 * 60, steps)

    def test_history_window_zoom_uses_exact_day_boundaries(self):
        self.assertEqual(next_history_window(16 * 60 * 60), 24 * 60 * 60)
        self.assertEqual(next_history_window(8 * 24 * 60 * 60), 14 * 24 * 60 * 60)
        self.assertEqual(previous_history_window(24 * 60 * 60), 16 * 60 * 60)
        self.assertEqual(previous_history_window(14 * 24 * 60 * 60), 8 * 24 * 60 * 60)
        self.assertEqual(HISTORY_WINDOW_STEPS[-1], 14 * 24 * 60 * 60)

    def test_two_line_chart_keeps_fixed_width(self):
        top, bottom = render_two_line_chart([0.0, 25.0, 50.0, 100.0], 16)
        self.assertEqual(len(top), 16)
        self.assertEqual(len(bottom), 16)

    def test_two_line_chart_full_value_uses_bottom_full_block(self):
        top, bottom = render_two_line_chart([100.0], 1)
        self.assertEqual(top, " " * 7 + "▄")
        self.assertEqual(bottom, " " * 7 + "█")

    def test_scale_chart_values_supports_custom_scale(self):
        scaled = scale_chart_values([0.0, 512.0, 1024.0], 1024.0)
        self.assertEqual(scaled, [0.0, 50.0, 100.0])

    def test_tall_chart_keeps_requested_dimensions(self):
        lines = render_tall_chart([0.0, 50.0, 100.0], 12, 4)
        self.assertEqual(len(lines), 4)
        self.assertTrue(all(len(line) == 12 for line in lines))

    def test_tall_chart_full_value_uses_solid_blocks(self):
        lines = render_tall_chart([100.0], 1, 3)
        self.assertEqual(lines, [(" " * 7) + "█"] * 3)

    def test_clamp_column_content_width_fills_remaining_space(self):
        width = clamp_column_content_width(80, [6, 7, 7, 9], cell_padding=1)
        self.assertEqual(width, 49)

    def test_clamp_column_content_width_respects_minimum(self):
        width = clamp_column_content_width(20, [10, 8], cell_padding=1)
        self.assertEqual(width, 8)

    def test_build_name_cell_text_appends_command_as_filler(self):
        row = EntityRow(
            key="host:1",
            kind="host",
            name="python",
            pid=123,
            image=None,
            command="python train.py --epochs 10",
            cpu_percent=0.0,
            gpu_percent=None,
            ram_sum_bytes=0,
            ram_rss_bytes=0,
            ram_cgroup_bytes=None,
            gpu_memory_bytes=0,
            status="running",
        )
        self.assertEqual(build_name_cell_text(row), "   123 | python | python train.py --epochs 10")

    def test_build_name_cell_text_can_hide_command(self):
        row = EntityRow(
            key="host:1",
            kind="host",
            name="python",
            pid=123,
            image=None,
            command="python train.py --epochs 10",
            cpu_percent=0.0,
            gpu_percent=None,
            ram_sum_bytes=0,
            ram_rss_bytes=0,
            ram_cgroup_bytes=None,
            gpu_memory_bytes=0,
            status="running",
        )
        self.assertEqual(build_name_cell_text(row, include_command=False), "   123 | python")

    def test_process_detail_text_shows_namespace_network_traffic(self):
        app = DgxTopApp()
        process = ProcessInfo(
            pid=4321,
            ppid=1,
            name="firefox",
            command="firefox",
            username="djmad",
            cpu_percent=12.5,
            rss_bytes=1024 * 1024,
            net_recv_rate=2048.0,
            net_send_rate=1024.0,
            net_namespace="net:[4026531840]",
            net_namespace_processes=7,
        )

        detail = app._detail_text(process)

        self.assertIn("net(ns): 2.0K/s down | 1.0K/s up | shared by 7 pids", detail)

if __name__ == "__main__":
    unittest.main()
