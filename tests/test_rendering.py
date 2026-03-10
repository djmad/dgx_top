import unittest

from dgxtop.app import build_name_cell_text, clamp_column_content_width, fmt_bytes, normalize_series, render_two_line_chart
from dgxtop.models import EntityRow


class RenderingTests(unittest.TestCase):
    def test_fmt_bytes(self):
        self.assertEqual(fmt_bytes(None), "--")
        self.assertEqual(fmt_bytes(0), "0B")
        self.assertEqual(fmt_bytes(1024), "1.0K")
        self.assertEqual(fmt_bytes(1024 * 1024), "1.0M")

    def test_normalize_series_returns_requested_width(self):
        values = normalize_series([0.0, 50.0, 100.0], 12)
        self.assertEqual(len(values), 12)

    def test_two_line_chart_keeps_fixed_width(self):
        top, bottom = render_two_line_chart([0.0, 25.0, 50.0, 100.0], 16)
        self.assertEqual(len(top), 16)
        self.assertEqual(len(bottom), 16)

    def test_two_line_chart_full_value_uses_bottom_full_block(self):
        top, bottom = render_two_line_chart([100.0], 1)
        self.assertEqual(top, "▄" * 8)
        self.assertEqual(bottom, "█" * 8)

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

if __name__ == "__main__":
    unittest.main()
