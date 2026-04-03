import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dgxtop.history_store import HistoryStore, default_history_path
from dgxtop.models import HistoryPoint


class HistoryStoreTests(unittest.TestCase):
    def test_default_history_path_uses_env_override(self):
        with patch.dict(os.environ, {"DGX_TOP_HISTORY_FILE": "~/custom-history.jsonl"}):
            self.assertEqual(default_history_path(), Path("~/custom-history.jsonl").expanduser())

    def test_load_discards_invalid_and_stale_points_then_compacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "history.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": 10.0,
                                "cpu_percent": 1.0,
                                "ram_percent": 2.0,
                                "gpu_percent": None,
                                "gpu_memory_percent": None,
                                "net_recv_rate": 3.0,
                                "net_send_rate": 4.0,
                            }
                        ),
                        "not-json",
                        json.dumps(
                            {
                                "timestamp": 95.0,
                                "cpu_percent": 11.0,
                                "ram_percent": 12.0,
                                "gpu_percent": 13.0,
                                "gpu_memory_percent": 14.0,
                                "net_recv_rate": 15.0,
                                "net_send_rate": 16.0,
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            store = HistoryStore(path, max_age_seconds=20, max_points=10)
            points = store.load(100.0)

            self.assertEqual(len(points), 1)
            self.assertEqual(points[0].timestamp, 95.0)
            self.assertEqual(path.read_text(encoding="utf-8").strip().count("\n"), 0)

    def test_load_keeps_latest_max_points(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "history.jsonl"
            store = HistoryStore(path, max_age_seconds=1_000, max_points=3)

            for timestamp in (10.0, 20.0, 30.0, 40.0):
                store.append(
                    HistoryPoint(
                        timestamp=timestamp,
                        cpu_percent=timestamp,
                        ram_percent=timestamp,
                        gpu_percent=None,
                        gpu_memory_percent=None,
                    )
                )

            points = store.load(100.0)

            self.assertEqual([point.timestamp for point in points], [20.0, 30.0, 40.0])


if __name__ == "__main__":
    unittest.main()
