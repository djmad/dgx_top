from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

from .models import HistoryPoint


DEFAULT_HISTORY_FILENAME = "history.jsonl"


def default_history_path() -> Path:
    custom_path = os.environ.get("DGX_TOP_HISTORY_FILE")
    if custom_path:
        return Path(custom_path).expanduser()

    state_home = os.environ.get("XDG_STATE_HOME")
    base_dir = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base_dir / "dgxtop" / DEFAULT_HISTORY_FILENAME


class HistoryStore:
    def __init__(self, path: Path | None = None, *, max_age_seconds: int, max_points: int) -> None:
        self.path = path or default_history_path()
        self.max_age_seconds = max_age_seconds
        self.max_points = max_points

    def load(self, now: float) -> list[HistoryPoint]:
        if not self.path.exists():
            return []

        cutoff = now - self.max_age_seconds
        points: list[HistoryPoint] = []
        needs_compaction = False

        with self.path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    point = self._decode_point(json.loads(line))
                except (TypeError, ValueError, json.JSONDecodeError):
                    needs_compaction = True
                    continue
                if point.timestamp < cutoff:
                    needs_compaction = True
                    continue
                points.append(point)

        if len(points) > self.max_points:
            points = points[-self.max_points :]
            needs_compaction = True

        if needs_compaction:
            self.replace(points)

        return points

    def append(self, point: HistoryPoint) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self._encode_point(point), separators=(",", ":")))
            handle.write("\n")

    def replace(self, points: Iterable[HistoryPoint]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            for point in points:
                handle.write(json.dumps(self._encode_point(point), separators=(",", ":")))
                handle.write("\n")

    def _encode_point(self, point: HistoryPoint) -> dict[str, float | None]:
        return {
            "timestamp": point.timestamp,
            "cpu_percent": point.cpu_percent,
            "ram_percent": point.ram_percent,
            "gpu_percent": point.gpu_percent,
            "gpu_memory_percent": point.gpu_memory_percent,
            "net_recv_rate": point.net_recv_rate,
            "net_send_rate": point.net_send_rate,
        }

    def _decode_point(self, payload: dict[str, object]) -> HistoryPoint:
        return HistoryPoint(
            timestamp=float(payload["timestamp"]),
            cpu_percent=float(payload["cpu_percent"]),
            ram_percent=float(payload["ram_percent"]),
            gpu_percent=self._optional_float(payload.get("gpu_percent")),
            gpu_memory_percent=self._optional_float(payload.get("gpu_memory_percent")),
            net_recv_rate=float(payload.get("net_recv_rate", 0.0)),
            net_send_rate=float(payload.get("net_send_rate", 0.0)),
        )

    @staticmethod
    def _optional_float(value: object) -> float | None:
        return None if value is None else float(value)
