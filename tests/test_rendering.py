import unittest

from dgxtop.app import fmt_bytes, normalize_series, render_two_line_chart


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


if __name__ == "__main__":
    unittest.main()
