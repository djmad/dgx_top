from __future__ import annotations

import argparse
import asyncio
import math
import time
from bisect import bisect_left, bisect_right
from collections import deque
from enum import Enum

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static

from .collectors import DashboardCollector, HISTORY_WINDOW_MAX, HISTORY_WINDOW_MIN
from .history_store import HistoryStore
from .models import ContainerInfo, DashboardSnapshot, EntityRow, HistoryPoint, ProcessInfo


class WatchdogMode(Enum):
    OFF = "off"
    BIGGEST = "biggest"
    MANUAL = "manual"


def fmt_bytes(value: int | None) -> str:
    if value is None:
        return "--"
    if value <= 0:
        return "0B"
    units = ["B", "K", "M", "G", "T"]
    amount = float(value)
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            return f"{amount:.1f}{unit}" if unit != "B" else f"{int(amount)}B"
        amount /= 1024.0
    return f"{amount:.1f}P"


def fmt_rate(value: float) -> str:
    return f"{fmt_bytes(int(value))}/s"


def fmt_percent(value: float | None) -> str:
    return "--" if value is None else f"{value:.1f}"


def fmt_temp(value: float | None) -> str:
    return "--" if value is None else f"{value:.0f}C"


def fmt_history_window(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


LOWER_BARS = " ▁▂▃▄"
VERTICAL_BARS = " ▁▂▃▄▅▆▇█"
NAME_COLUMN_KEY = "name"
NAME_COLUMN_MIN_WIDTH = 8
REFRESH_INTERVAL_SECONDS = 2.0
MAX_HISTORY_POINTS = max(1, int(HISTORY_WINDOW_MAX / REFRESH_INTERVAL_SECONDS) + 2)


def build_history_window_steps(min_seconds: int, max_seconds: int) -> list[int]:
    steps = [min_seconds]
    current = min_seconds
    snap_points = (3600, 86400)

    for snap_point in snap_points:
        while current < min(max_seconds, snap_point):
            next_value = current * 2
            current = snap_point if current < snap_point <= next_value else next_value
            if current > max_seconds:
                break
            if current != steps[-1]:
                steps.append(current)

    while current < max_seconds:
        current = min(max_seconds, current * 2)
        if current != steps[-1]:
            steps.append(current)

    return steps


HISTORY_WINDOW_STEPS = build_history_window_steps(HISTORY_WINDOW_MIN, HISTORY_WINDOW_MAX)


def next_history_window(current: int) -> int:
    index = bisect_right(HISTORY_WINDOW_STEPS, current)
    return HISTORY_WINDOW_STEPS[min(index, len(HISTORY_WINDOW_STEPS) - 1)]


def previous_history_window(current: int) -> int:
    index = bisect_left(HISTORY_WINDOW_STEPS, current) - 1
    return HISTORY_WINDOW_STEPS[max(index, 0)]


def build_timeline_series(
    points: list[HistoryPoint],
    width: int,
    history_window: int,
    now: float,
    value_getter,
) -> list[float | None]:
    bucket_count = max(8, width)
    if history_window <= 0:
        return [None] * bucket_count

    seconds_per_bucket = history_window / bucket_count
    end_time = math.ceil(now / max(seconds_per_bucket, 1e-6)) * seconds_per_bucket
    start_time = end_time - history_window
    buckets: list[list[float]] = [[] for _ in range(bucket_count)]
    for point in points:
        if point.timestamp < start_time or point.timestamp > now:
            continue
        value = value_getter(point)
        if value is None or math.isnan(value):
            continue
        position = (point.timestamp - start_time) / history_window
        index = min(bucket_count - 1, max(0, int(position * bucket_count)))
        buckets[index].append(value)

    if seconds_per_bucket > REFRESH_INTERVAL_SECONDS:
        return [None if not bucket else max(bucket) for bucket in buckets]

    series = [None if not bucket else sum(bucket) / len(bucket) for bucket in buckets]
    max_gap = max(1, int(math.ceil(REFRESH_INTERVAL_SECONDS / max(seconds_per_bucket, 1e-6))))
    return interpolate_small_gaps(series, max_gap)


def interpolate_small_gaps(values: list[float | None], max_gap: int) -> list[float | None]:
    if max_gap <= 0:
        return values

    filled = list(values)
    index = 0
    while index < len(filled):
        if filled[index] is not None:
            index += 1
            continue

        gap_start = index
        while index < len(filled) and filled[index] is None:
            index += 1
        gap_end = index
        gap_size = gap_end - gap_start

        left_index = gap_start - 1
        right_index = gap_end
        if gap_size > max_gap or left_index < 0 or right_index >= len(filled):
            continue
        left_value = filled[left_index]
        right_value = filled[right_index]
        if left_value is None or right_value is None:
            continue

        step = (right_value - left_value) / (gap_size + 1)
        for offset in range(gap_size):
            filled[gap_start + offset] = left_value + (step * (offset + 1))

    return filled


def normalize_series(values: list[float | None], width: int) -> list[float | None]:
    width = max(8, width)
    cleaned = [None if value is None or math.isnan(value) else max(0.0, min(100.0, value)) for value in values]
    if not cleaned:
        return [None] * width
    if len(cleaned) > width:
        bucket = len(cleaned) / width
        compressed: list[float | None] = []
        for index in range(width):
            start = int(index * bucket)
            end = max(start + 1, int((index + 1) * bucket))
            chunk = [value for value in cleaned[start:end] if value is not None]
            compressed.append(None if not chunk else sum(chunk) / len(chunk))
        cleaned = compressed
    elif len(cleaned) < width:
        cleaned = ([None] * (width - len(cleaned))) + cleaned
    return cleaned


def render_two_line_chart(values: list[float | None], width: int) -> tuple[str, str]:
    cleaned = normalize_series(values, width)
    top_chars: list[str] = []
    bottom_chars: list[str] = []
    for value in cleaned:
        if value is None:
            top_chars.append(" ")
            bottom_chars.append(" ")
            continue
        level = int(round(value / 100.0 * 8))
        level = max(0, min(8, level))
        if level == 0:
            top_chars.append(" ")
            bottom_chars.append(" ")
        elif level <= 4:
            top_chars.append(" ")
            bottom_chars.append(LOWER_BARS[level])
        else:
            top_chars.append(LOWER_BARS[level - 4])
            bottom_chars.append("█")
    return "".join(top_chars), "".join(bottom_chars)


def scale_chart_values(values: list[float | None], scale_max: float = 100.0) -> list[float | None]:
    return [
        None if value is None else (0.0 if scale_max <= 0 else max(0.0, min(100.0, value / scale_max * 100.0)))
        for value in values
    ]


def render_tall_chart(values: list[float | None], width: int, height: int) -> list[str]:
    cleaned = normalize_series(values, width)
    chart_height = max(1, height)
    levels = [None if value is None else int(round(value / 100.0 * chart_height * 8)) for value in cleaned]
    lines: list[str] = []
    for row in range(chart_height):
        row_base = (chart_height - row - 1) * 8
        chars: list[str] = []
        for level in levels:
            if level is None:
                chars.append(" ")
                continue
            fill = max(0, min(8, level - row_base))
            chars.append(VERTICAL_BARS[fill])
        lines.append("".join(chars))
    return lines


def render_metric_block(
    label: str,
    latest: float | None,
    values: list[float | None],
    width: int,
    *,
    prefix_value: str,
    scale_max: float = 100.0,
) -> tuple[str, str]:
    prefix = f"{label} {prefix_value} "
    chart_width = max(8, width - len(prefix))
    scaled_values = scale_chart_values(values, scale_max)
    top, bottom = render_two_line_chart(scaled_values, chart_width)
    return f"{prefix}{top}", f"{' ' * len(prefix)}{bottom}"


def render_history_metric_block(
    label: str,
    latest: float | None,
    points: list[HistoryPoint],
    width: int,
    *,
    prefix_value: str,
    history_window: int,
    now: float,
    value_getter,
    scale_max: float = 100.0,
) -> tuple[str, str]:
    prefix = f"{label} {prefix_value} "
    chart_width = max(8, width - len(prefix))
    values = build_timeline_series(points, chart_width, history_window, now, value_getter)
    scaled_values = scale_chart_values(values, scale_max)
    top, bottom = render_two_line_chart(scaled_values, chart_width)
    return f"{prefix}{top}", f"{' ' * len(prefix)}{bottom}"


def clamp_column_content_width(
    total_width: int,
    other_render_widths: list[int],
    *,
    cell_padding: int = 1,
    min_content_width: int = NAME_COLUMN_MIN_WIDTH,
) -> int:
    remaining_render_width = total_width - sum(other_render_widths)
    return max(min_content_width, remaining_render_width - (2 * cell_padding))


def build_name_cell_text(row: EntityRow, *, include_command: bool = True) -> str:
    pid_value = str(row.pid) if row.pid else "--"
    pid = f"{pid_value:>6}"
    parts = [pid, row.name]
    if include_command and row.command:
        parts.append(row.command)
    return " | ".join(parts)


class ConfirmActionScreen(ModalScreen[bool]):
    CSS = """
    Screen {
        align: center middle;
    }

    #confirm-box {
        width: 60;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: round $primary;
    }

    #confirm-actions {
        height: auto;
        margin-top: 1;
    }

    #confirm-actions Button {
        margin-right: 1;
    }
    """

    BINDINGS = [
        Binding("escape,n", "cancel", "Cancel"),
        Binding("y", "confirm", "Confirm"),
    ]

    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self.title = title
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(f"{self.title}\n{self.message}", id="confirm-message")
            with Horizontal(id="confirm-actions"):
                yield Button("Cancel", id="cancel", variant="default")
                yield Button("Confirm", id="confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)


class DgxTopApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }

    #summary {
        height: 2;
        padding: 0 1;
    }

    #summary.hidden {
        display: none;
    }

    #main-area {
        height: 2fr;
    }

    #main-area.hidden {
        display: none;
    }

    #rows {
        width: 2fr;
    }

    #details {
        width: 1fr;
        min-width: 36;
        display: none;
        padding: 0 1;
        border: round $panel;
    }

    #details.visible {
        display: block;
    }

    #trends {
        height: 1fr;
        padding: 0 1;
        border: round $panel;
    }

    #trends.fullscreen {
        padding: 0;
        border: none;
    }

    #footer {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("d", "toggle_details", "Detail"),
        Binding("x", "toggle_stopped", "Stopped"),
        Binding("c", "sort_cpu", "CPU"),
        Binding("g", "toggle_graph_mode", "Graphs"),
        Binding("shift+g", "sort_gpu", "GPU"),
        Binding("m", "sort_ram_sum", "RAM"),
        Binding("v", "sort_gpu_mem", "VRAM"),
        Binding("plus,equals", "expand_history", "History+"),
        Binding("minus", "shrink_history", "History-"),
        Binding("k", "kill_selected", "Kill"),
        Binding("r", "restart_selected", "Restart"),
        Binding("w", "cycle_watchdog", "Watchdog"),
        Binding("W", "set_watchdog_target", "WD target"),
    ]

    SORT_COLUMNS = [
        ("type", "kind"),
        ("name", "name"),
        ("cpu", "cpu_percent"),
        ("gpu", "gpu_percent"),
        ("ram_sum", "ram_sum_bytes"),
        ("ram_rss", "ram_rss_bytes"),
        ("ram_cgrp", "ram_cgroup_bytes"),
        ("gpu_mem", "gpu_memory_bytes"),
        ("status", "status"),
    ]

    def __init__(
        self,
        history_store: HistoryStore | None = None,
        watchdog_mode: WatchdogMode = WatchdogMode.OFF,
        watchdog_grace_bytes: int = 1 * 1024 ** 3,
        watchdog_container: str | None = None,
    ) -> None:
        super().__init__()
        self.collector = DashboardCollector()
        self.snapshot: DashboardSnapshot | None = None
        self.history_window = 3600
        self.show_stopped = False
        self.details_visible = False
        self.graph_mode = False
        self.sort_field = "ram_sum_bytes"
        self.sort_desc = True
        self.selected_key: str | None = None
        self._visible_keys: list[str] = []
        self._refresh_lock = asyncio.Lock()
        self.history: deque[HistoryPoint] = deque()
        self.history_store = history_store or HistoryStore(
            max_age_seconds=HISTORY_WINDOW_MAX,
            max_points=MAX_HISTORY_POINTS,
        )
        self._history_store_error: str | None = None
        self.watchdog_mode = watchdog_mode
        self.watchdog_grace_bytes = watchdog_grace_bytes
        self.watchdog_container = watchdog_container
        self._watchdog_last_kill: float = 0.0

    def compose(self) -> ComposeResult:
        yield Static("", id="summary")
        with Horizontal(id="main-area"):
            yield DataTable(id="rows")
            yield Static("", id="details")
        yield Static("", id="trends")
        yield Static("", id="footer")

    def on_mount(self) -> None:
        table = self.query_one("#rows", DataTable)
        table.zebra_stripes = True
        table.cursor_type = "row"
        table.show_horizontal_scrollbar = False
        table.show_cursor = True
        table.show_header = True
        table.add_column("T", width=1, key="type")
        table.add_column("PID / Name", width=20, key=NAME_COLUMN_KEY)
        table.add_column("CPU%", width=5, key="cpu")
        table.add_column("GPU%", width=5, key="gpu")
        table.add_column("RAM SUM", width=7, key="ram_sum")
        table.add_column("RAM RSS", width=7, key="ram_rss")
        table.add_column("RAM CGRP", width=7, key="ram_cgrp")
        table.add_column("GPU MEM", width=7, key="gpu_mem")
        table.add_column("Status", width=7, key="status")

        self._refresh_footer()
        self._load_persisted_history()

        self.set_interval(REFRESH_INTERVAL_SECONDS, self._schedule_refresh)
        self._schedule_refresh()

    def on_unmount(self) -> None:
        self.collector.shutdown()

    async def refresh_dashboard(self) -> None:
        if self._refresh_lock.locked():
            return
        async with self._refresh_lock:
            snapshot = await asyncio.to_thread(self.collector.sample, self.show_stopped)
            self.snapshot = snapshot
            point = HistoryPoint(
                timestamp=snapshot.timestamp,
                cpu_percent=snapshot.system.cpu_percent,
                ram_percent=snapshot.system.ram_percent,
                gpu_percent=snapshot.system.gpu_percent,
                gpu_memory_percent=snapshot.system.gpu_memory_percent,
                net_recv_rate=snapshot.system.net_recv_rate,
                net_send_rate=snapshot.system.net_send_rate,
            )
            self._append_history_point(point, persist=True)
            self._refresh_summary()
            self._refresh_table()
            self._refresh_details()
            self._refresh_trends()
            await self._watchdog_check()

    def _load_persisted_history(self) -> None:
        try:
            self.history.clear()
            self.history.extend(self.history_store.load(time.time()))
        except OSError as error:
            self._report_history_store_error(f"Unable to load chart history: {error}")

    def _append_history_point(self, point: HistoryPoint, *, persist: bool) -> None:
        self.history.append(point)
        trimmed = self._trim_history(point.timestamp)
        if not persist:
            return
        try:
            self.history_store.append(point)
            if trimmed:
                self.history_store.replace(self.history)
            self._history_store_error = None
        except OSError as error:
            self._report_history_store_error(f"Unable to store chart history: {error}")

    def _trim_history(self, now: float) -> bool:
        cutoff = now - HISTORY_WINDOW_MAX
        trimmed = False
        while self.history and self.history[0].timestamp < cutoff:
            self.history.popleft()
            trimmed = True
        while len(self.history) > MAX_HISTORY_POINTS:
            self.history.popleft()
            trimmed = True
        return trimmed

    def _report_history_store_error(self, message: str) -> None:
        if message == self._history_store_error:
            return
        self._history_store_error = message
        self.notify(message, severity="warning")

    def _refresh_footer(self) -> None:
        graph_hint = "g table" if self.graph_mode else "g graphs"
        self.query_one("#footer", Static).update(
            f"Keys: q quit  {graph_hint}  shift+g gpu  d detail  c cpu  m ram-sum  v vram  x stopped  k kill  r restart  +/- zoom  w watchdog  W target"
        )

    def _schedule_refresh(self) -> None:
        # Sampling can take longer than the 2s timer on busy systems.
        # Let the lock inside refresh_dashboard() drop overlapping refreshes
        # instead of canceling the in-flight worker before it can render.
        self.run_worker(self.refresh_dashboard())

    def _refresh_summary(self) -> None:
        assert self.snapshot is not None
        system = self.snapshot.system
        wd_label = self.watchdog_mode.value
        if self.watchdog_mode == WatchdogMode.MANUAL:
            wd_label += f"({self.watchdog_container or '?'})"
        if self.watchdog_mode != WatchdogMode.OFF:
            wd_label += f" grace:{fmt_bytes(self.watchdog_grace_bytes)}"
        line_one = (
            f"DGX_TOP  refresh:{int(REFRESH_INTERVAL_SECONDS)}s  history:{fmt_history_window(self.history_window)}  "
            f"sort:{self.sort_label()}  show:{'all' if self.show_stopped else 'running'}  "
            f"docker:{system.running_containers} up / {system.stopped_containers} stopped  "
            f"watchdog:{wd_label}"
        )
        gpu_name = system.gpu_name or "GPU n/a"
        gpu_total = fmt_bytes(system.gpu_memory_total_bytes) if system.gpu_memory_total_bytes > 0 else "--"
        gpu_util = f"{fmt_percent(system.gpu_percent)}%" if system.gpu_percent is not None else "--"
        gpu_percent = f"{fmt_percent(system.gpu_memory_percent)}%" if system.gpu_memory_percent is not None else "--"
        line_two = (
            f"CPU {system.cpu_percent:.0f}%  {fmt_temp(system.cpu_temp_c)}  "
            f"load {system.load_avg[0]:.2f} {system.load_avg[1]:.2f} {system.load_avg[2]:.2f}  "
            f"RAM {fmt_bytes(system.ram_used_bytes)}/{fmt_bytes(system.ram_total_bytes)} {system.ram_percent:.0f}%  "
            f"SWAP {fmt_bytes(system.swap_used_bytes)}/{fmt_bytes(system.swap_total_bytes)}  "
            f"{gpu_name} {gpu_util}  {fmt_temp(system.gpu_temp_c)}  "
            f"VRAM {fmt_bytes(system.gpu_memory_used_bytes)}/{gpu_total} "
            f"{gpu_percent}  NET {fmt_rate(system.net_recv_rate)} down {fmt_rate(system.net_send_rate)} up"
        )
        self.query_one("#summary", Static).update(f"{line_one}\n{line_two}")

    def _sorted_rows(self) -> list[EntityRow]:
        assert self.snapshot is not None

        def sort_value(row: EntityRow):
            value = getattr(row, self.sort_field)
            if value is None:
                return -1.0 if self.sort_desc else float("inf")
            return value

        rows = sorted(self.snapshot.rows, key=sort_value, reverse=self.sort_desc)
        if not self.show_stopped:
            rows = [row for row in rows if row.status in ("running", "run") or row.kind == "host"]
        return rows

    def _refresh_table(self) -> None:
        assert self.snapshot is not None
        table = self.query_one("#rows", DataTable)
        self._resize_name_column(table)
        rows = self._sorted_rows()
        existing_selection = self.selected_key

        table.clear(columns=False)
        wd_target = self._watchdog_target()
        wd_target_name = wd_target.name if wd_target is not None else None
        visible_keys: list[str] = []
        for row in rows:
            visible_keys.append(row.key)
            type_cell = Text("D", style="bold green") if row.kind == "docker" else Text("H", style="bold cyan")
            if row.name == wd_target_name:
                type_cell.append("w", style="bold orange1")
            name_cell = self._name_cell(row)
            table.add_row(
                type_cell,
                name_cell,
                f"{row.cpu_percent:.1f}",
                fmt_percent(row.gpu_percent),
                fmt_bytes(row.ram_sum_bytes),
                fmt_bytes(row.ram_rss_bytes),
                fmt_bytes(row.ram_cgroup_bytes),
                fmt_bytes(row.gpu_memory_bytes),
                row.status,
                key=row.key,
            )

        self._visible_keys = visible_keys
        if not visible_keys:
            self.selected_key = None
            return

        if existing_selection not in visible_keys:
            self.selected_key = visible_keys[0]
        else:
            self.selected_key = existing_selection

        target_index = visible_keys.index(self.selected_key)
        table.move_cursor(row=target_index)

    def _name_cell(self, row: EntityRow) -> Text:
        cell = Text(no_wrap=True, overflow="ellipsis")
        cell.append(build_name_cell_text(row, include_command=not self.details_visible))
        return cell

    def _resize_name_column(self, table: DataTable) -> None:
        if NAME_COLUMN_KEY not in table.columns:
            return

        other_render_widths = [
            column.get_render_width(table)
            for key, column in table.columns.items()
            if key != NAME_COLUMN_KEY
        ]
        name_width = clamp_column_content_width(
            table.size.width,
            other_render_widths,
            cell_padding=table.cell_padding,
        )
        name_column = table.columns[NAME_COLUMN_KEY]
        if name_column.width == name_width:
            return
        name_column.width = name_width
        table._require_update_dimensions = True
        table.refresh()

    def _refresh_details(self) -> None:
        panel = self.query_one("#details", Static)
        if not self.details_visible or self.selected_key is None or self.snapshot is None:
            panel.remove_class("visible")
            panel.update("")
            return

        panel.add_class("visible")
        entity = self.current_entity()
        if entity is None:
            panel.update("")
            return
        panel.update(self._detail_text(entity))

    def _detail_text(self, entity) -> str:
        if isinstance(entity, ContainerInfo):
            lines = [
                entity.name,
                f"id: {entity.container_id[:12]}   runtime: {entity.runtime or '--'}   status: {entity.status}",
                f"pid: {entity.main_pid or '--'}   uptime: {entity.uptime or '--'}   ports: {entity.ports or '--'}",
                f"image: {entity.image}",
            ]
            if entity.command:
                lines.append(f"cmd: {entity.command}")
            lines.append(f"net: {fmt_rate(entity.net_recv_rate)} down | {fmt_rate(entity.net_send_rate)} up")
            lines.append(
                "memory: "
                f"anon {fmt_bytes(entity.memory.anon_bytes)} | file {fmt_bytes(entity.memory.file_bytes)} | "
                f"kernel {fmt_bytes(entity.memory.kernel_bytes)} | shmem {fmt_bytes(entity.memory.shmem_bytes)} | "
                f"ptables {fmt_bytes(entity.memory.pagetables_bytes)} | peak {fmt_bytes(entity.memory.peak_bytes)}"
            )
            if entity.processes:
                lines.append("")
                lines.append("top inner processes")
                for process in entity.processes[:8]:
                    lines.append(
                        f"{process.pid:>6}  cpu {process.cpu_percent:>5.1f}  rss {fmt_bytes(process.rss_bytes):>7}  "
                        f"gpu {fmt_bytes(process.gpu_memory_bytes):>7}  {process.name}"
                    )
            return "\n".join(lines)

        process: ProcessInfo = entity
        source_label = "ebpf" if process.net_source == "ebpf" else "ns"
        net_line = f"net({source_label}): {fmt_rate(process.net_recv_rate)} down | {fmt_rate(process.net_send_rate)} up"
        if process.net_source != "ebpf" and process.net_namespace_processes > 1:
            net_line += f" | shared by {process.net_namespace_processes} pids"
        lines = [
            process.name,
            f"pid: {process.pid}   ppid: {process.ppid}   user: {process.username}   status: {process.status or '--'}",
            f"cpu: {process.cpu_percent:.1f}%   rss: {fmt_bytes(process.rss_bytes)}   gpu mem: {fmt_bytes(process.gpu_memory_bytes)}",
            net_line,
            f"cmd: {process.command}",
        ]
        return "\n".join(lines)

    def _refresh_trends(self) -> None:
        now = self.snapshot.timestamp if self.snapshot else 0.0
        window_points = [point for point in self.history if now - point.timestamp <= self.history_window]
        trends = self.query_one("#trends", Static)
        if not window_points:
            trends.update("Window: --")
            return

        if self.graph_mode:
            trends.update(self._render_fullscreen_trends(window_points, trends.size.width, trends.size.height))
            return

        lines = [f"Window: {fmt_history_window(self.history_window)}"]
        total_width = max(48, trends.size.width - 4)
        block_width = max(22, (total_width - 2) // 2)
        series = [
            render_history_metric_block(
                "CPU ",
                window_points[-1].cpu_percent,
                window_points,
                block_width,
                prefix_value=f"{fmt_percent(window_points[-1].cpu_percent):>5}%",
                history_window=self.history_window,
                now=now,
                value_getter=lambda point: point.cpu_percent,
            ),
            render_history_metric_block(
                "GPU ",
                window_points[-1].gpu_percent,
                window_points,
                block_width,
                prefix_value=f"{fmt_percent(window_points[-1].gpu_percent):>5}%",
                history_window=self.history_window,
                now=now,
                value_getter=lambda point: point.gpu_percent,
            ),
            render_history_metric_block(
                "RAM ",
                window_points[-1].ram_percent,
                window_points,
                block_width,
                prefix_value=f"{fmt_percent(window_points[-1].ram_percent):>5}%",
                history_window=self.history_window,
                now=now,
                value_getter=lambda point: point.ram_percent,
            ),
            render_history_metric_block(
                "VRAM",
                window_points[-1].gpu_memory_percent,
                window_points,
                block_width,
                prefix_value=f"{fmt_percent(window_points[-1].gpu_memory_percent):>5}%",
                history_window=self.history_window,
                now=now,
                value_getter=lambda point: point.gpu_memory_percent,
            ),
        ]
        net_scale = max(
            max((point.net_recv_rate for point in window_points), default=0.0),
            max((point.net_send_rate for point in window_points), default=0.0),
            1.0,
        )
        series.extend(
            [
                render_metric_block(
                    "DOWN",
                    window_points[-1].net_recv_rate,
                    build_timeline_series(
                        window_points,
                        max(8, block_width - len(f"DOWN {fmt_rate(window_points[-1].net_recv_rate):>8} ")),
                        self.history_window,
                        now,
                        lambda point: point.net_recv_rate,
                    ),
                    block_width,
                    prefix_value=f"{fmt_rate(window_points[-1].net_recv_rate):>8}",
                    scale_max=net_scale,
                ),
                render_metric_block(
                    "UP  ",
                    window_points[-1].net_send_rate,
                    build_timeline_series(
                        window_points,
                        max(8, block_width - len(f"UP   {fmt_rate(window_points[-1].net_send_rate):>8} ")),
                        self.history_window,
                        now,
                        lambda point: point.net_send_rate,
                    ),
                    block_width,
                    prefix_value=f"{fmt_rate(window_points[-1].net_send_rate):>8}",
                    scale_max=net_scale,
                ),
            ]
        )
        for left, right in ((series[0], series[1]), (series[2], series[3]), (series[4], series[5])):
            lines.append(f"{left[0]}  {right[0]}")
            lines.append(f"{left[1]}  {right[1]}")
        trends.update("\n".join(lines))

    def _render_fullscreen_trends(self, window_points: list[HistoryPoint], width: int, height: int) -> str:
        total_width = max(48, width)
        block_width = max(22, (total_width - 2) // 2)
        network_scale = max(
            max((point.net_recv_rate for point in window_points), default=0.0),
            max((point.net_send_rate for point in window_points), default=0.0),
            1.0,
        )
        metrics = [
            (
                "CPU",
                f"{fmt_percent(window_points[-1].cpu_percent):>5}%",
                scale_chart_values(
                    build_timeline_series(window_points, block_width, self.history_window, window_points[-1].timestamp, lambda point: point.cpu_percent)
                ),
            ),
            (
                "GPU",
                f"{fmt_percent(window_points[-1].gpu_percent):>5}%",
                scale_chart_values(
                    build_timeline_series(window_points, block_width, self.history_window, window_points[-1].timestamp, lambda point: point.gpu_percent)
                ),
            ),
            (
                "RAM",
                f"{fmt_percent(window_points[-1].ram_percent):>5}%",
                scale_chart_values(
                    build_timeline_series(window_points, block_width, self.history_window, window_points[-1].timestamp, lambda point: point.ram_percent)
                ),
            ),
            (
                "VRAM",
                f"{fmt_percent(window_points[-1].gpu_memory_percent):>5}%",
                scale_chart_values(
                    build_timeline_series(
                        window_points,
                        block_width,
                        self.history_window,
                        window_points[-1].timestamp,
                        lambda point: point.gpu_memory_percent,
                    )
                ),
            ),
            (
                "DOWN",
                f"{fmt_rate(window_points[-1].net_recv_rate):>8}",
                scale_chart_values(
                    build_timeline_series(
                        window_points,
                        block_width,
                        self.history_window,
                        window_points[-1].timestamp,
                        lambda point: point.net_recv_rate,
                    ),
                    network_scale,
                ),
            ),
            (
                "UP",
                f"{fmt_rate(window_points[-1].net_send_rate):>8}",
                scale_chart_values(
                    build_timeline_series(
                        window_points,
                        block_width,
                        self.history_window,
                        window_points[-1].timestamp,
                        lambda point: point.net_send_rate,
                    ),
                    network_scale,
                ),
            ),
        ]

        metric_rows = 3
        per_row_height = max(4, max(12, height - 1) // metric_rows)
        chart_height = max(3, per_row_height - 1)
        lines = [f"Fullscreen graphs  window:{fmt_history_window(self.history_window)}  net-scale:{fmt_rate(network_scale)}"]

        for row_index in range(0, len(metrics), 2):
            left_label, left_value, left_values = metrics[row_index]
            right_label, right_value, right_values = metrics[row_index + 1]
            left_header = f"{left_label} {left_value}".ljust(block_width)
            right_header = f"{right_label} {right_value}".ljust(block_width)
            lines.append(f"{left_header}  {right_header}")
            left_chart = render_tall_chart(left_values, block_width, chart_height)
            right_chart = render_tall_chart(right_values, block_width, chart_height)
            for left_line, right_line in zip(left_chart, right_chart):
                lines.append(f"{left_line}  {right_line}")

        return "\n".join(lines)

    def current_entity(self):
        if self.snapshot is None or self.selected_key is None:
            return None
        if self.selected_key.startswith("docker:"):
            return self.snapshot.containers.get(self.selected_key.split(":", 1)[1])
        return self.snapshot.host_processes.get(self.selected_key)

    def sort_label(self) -> str:
        mapping = {
            "cpu_percent": "CPU",
            "gpu_percent": "GPU%",
            "ram_sum_bytes": "RAM_SUM",
            "ram_rss_bytes": "RAM_RSS",
            "ram_cgroup_bytes": "RAM_CGRP",
            "gpu_memory_bytes": "GPU_MEM",
        }
        suffix = "↓" if self.sort_desc else "↑"
        return f"{mapping.get(self.sort_field, self.sort_field)}{suffix}"

    def _set_sort(self, field: str) -> None:
        if self.sort_field == field:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_field = field
            self.sort_desc = True
        if self.snapshot is not None:
            self._refresh_summary()
            self._refresh_table()
            self._refresh_details()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.cursor_row < len(getattr(self, "_visible_keys", [])):
            self.selected_key = self._visible_keys[event.cursor_row]
            self._refresh_details()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.cursor_row < len(getattr(self, "_visible_keys", [])):
            self.selected_key = self._visible_keys[event.cursor_row]
            self._refresh_details()

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        if 0 <= event.column_index < len(self.SORT_COLUMNS):
            self._set_sort(self.SORT_COLUMNS[event.column_index][1])

    def action_sort_cpu(self) -> None:
        self._set_sort("cpu_percent")

    def action_sort_gpu(self) -> None:
        self._set_sort("gpu_percent")

    def action_sort_ram_sum(self) -> None:
        self._set_sort("ram_sum_bytes")

    def action_sort_gpu_mem(self) -> None:
        self._set_sort("gpu_memory_bytes")

    def action_toggle_details(self) -> None:
        self.details_visible = not self.details_visible
        self._refresh_details()
        if self.snapshot is not None:
            self._refresh_table()

    def action_toggle_graph_mode(self) -> None:
        self.graph_mode = not self.graph_mode
        summary = self.query_one("#summary", Static)
        main_area = self.query_one("#main-area", Horizontal)
        trends = self.query_one("#trends", Static)
        if self.graph_mode:
            summary.add_class("hidden")
            main_area.add_class("hidden")
            trends.add_class("fullscreen")
        else:
            summary.remove_class("hidden")
            main_area.remove_class("hidden")
            trends.remove_class("fullscreen")
        self._refresh_footer()
        self._refresh_trends()

    def on_resize(self) -> None:
        if self.snapshot is None:
            return
        self._refresh_table()
        self._refresh_trends()

    def action_toggle_stopped(self) -> None:
        self.show_stopped = not self.show_stopped
        self.run_worker(self.refresh_dashboard())

    def action_expand_history(self) -> None:
        self.history_window = next_history_window(self.history_window)
        self._refresh_summary()
        self._refresh_trends()

    def action_shrink_history(self) -> None:
        self.history_window = previous_history_window(self.history_window)
        self._refresh_summary()
        self._refresh_trends()

    def action_kill_selected(self) -> None:
        entity = self.current_entity()
        if entity is None:
            self.notify("No row selected", severity="warning")
            return

        if isinstance(entity, ContainerInfo):
            title = "Stop container"
            message = f"Stop {entity.name}?"
        else:
            title = "Terminate process"
            message = f"Send SIGTERM to pid {entity.pid} ({entity.name})?"

        async def handle_confirmation(confirmed: bool | None) -> None:
            if not confirmed:
                return
            await self._kill_entity(entity)

        self.push_screen(ConfirmActionScreen(title, message), callback=handle_confirmation)

    async def _kill_entity(self, entity: ContainerInfo | ProcessInfo) -> None:
        try:
            if isinstance(entity, ContainerInfo):
                message = await asyncio.to_thread(self.collector.stop_container, entity.container_id)
            else:
                message = await asyncio.to_thread(self.collector.terminate_process, entity.pid)
            self.notify(message)
            await self.refresh_dashboard()
        except Exception as error:
            self.notify(str(error), severity="error")

    def action_restart_selected(self) -> None:
        entity = self.current_entity()
        if not isinstance(entity, ContainerInfo):
            self.notify("Restart works on docker rows only", severity="warning")
            return

        async def handle_confirmation(confirmed: bool | None) -> None:
            if not confirmed:
                return
            await self._restart_container(entity)

        self.push_screen(
            ConfirmActionScreen("Restart container", f"Restart {entity.name}?"),
            callback=handle_confirmation,
        )

    async def _restart_container(self, entity: ContainerInfo) -> None:
        try:
            message = await asyncio.to_thread(self.collector.restart_container, entity.container_id)
            self.notify(message)
            await self.refresh_dashboard()
        except Exception as error:
            self.notify(str(error), severity="error")

    # ── Memory watchdog ──────────────────────────────────────────────

    def _watchdog_target(self) -> ContainerInfo | None:
        """Return the container the watchdog would kill, or None."""
        if self.watchdog_mode == WatchdogMode.OFF or self.snapshot is None:
            return None
        containers = self.snapshot.containers
        running = {k: c for k, c in containers.items() if c.status == "running"}
        if not running:
            return None
        if self.watchdog_mode == WatchdogMode.MANUAL:
            if self.watchdog_container is None:
                return None
            for c in running.values():
                if c.name == self.watchdog_container or c.container_id.startswith(self.watchdog_container):
                    return c
            return None
        # BIGGEST mode: container with highest rss_bytes
        return max(running.values(), key=lambda c: c.rss_bytes)

    async def _watchdog_check(self) -> None:
        """Kill target container if system RAM headroom is below grace."""
        if self.watchdog_mode == WatchdogMode.OFF or self.snapshot is None:
            return
        system = self.snapshot.system
        free_bytes = system.ram_total_bytes - system.ram_used_bytes
        if free_bytes >= self.watchdog_grace_bytes:
            return
        # Cooldown: don't kill more than once per 10 seconds
        now = time.time()
        if now - self._watchdog_last_kill < 10.0:
            return
        target = self._watchdog_target()
        if target is None:
            self.notify("Watchdog: no target container found!", severity="warning")
            return
        self._watchdog_last_kill = now
        self.notify(
            f"WATCHDOG: killing {target.name} (free RAM {fmt_bytes(free_bytes)} < grace {fmt_bytes(self.watchdog_grace_bytes)})",
            severity="error",
        )
        try:
            message = await asyncio.to_thread(self.collector.kill_container, target.container_id)
            self.notify(f"Watchdog: {message}", severity="warning")
        except Exception as error:
            self.notify(f"Watchdog kill failed: {error}", severity="error")

    def action_cycle_watchdog(self) -> None:
        modes = list(WatchdogMode)
        idx = modes.index(self.watchdog_mode)
        self.watchdog_mode = modes[(idx + 1) % len(modes)]
        label = self.watchdog_mode.value
        if self.watchdog_mode == WatchdogMode.MANUAL:
            label += f" ({self.watchdog_container or 'none'})"
        self.notify(f"Watchdog: {label}")
        self._refresh_footer()
        self._refresh_summary()

    def action_set_watchdog_target(self) -> None:
        """Set the currently selected container as the watchdog target."""
        entity = self.current_entity()
        if not isinstance(entity, ContainerInfo):
            self.notify("Select a container row first", severity="warning")
            return
        self.watchdog_container = entity.name
        if self.watchdog_mode != WatchdogMode.MANUAL:
            self.watchdog_mode = WatchdogMode.MANUAL
        self.notify(f"Watchdog target: {entity.name}")
        self._refresh_footer()
        self._refresh_summary()


def _parse_grace(value: str) -> int:
    """Parse a human-friendly byte size like '1G', '512M', '2048'."""
    value = value.strip().upper()
    multipliers = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
    if value and value[-1] in multipliers:
        return int(float(value[:-1]) * multipliers[value[-1]])
    return int(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="DGX Top — Docker/GPU monitoring dashboard")
    parser.add_argument(
        "--watchdog",
        choices=["off", "biggest", "manual"],
        default="off",
        help="Memory watchdog mode: off (default), biggest (kill largest container), manual (kill selected container)",
    )
    parser.add_argument(
        "--watchdog-grace",
        default="1G",
        metavar="SIZE",
        help="Free RAM threshold before watchdog kills (default: 1G). Supports K/M/G/T suffixes.",
    )
    parser.add_argument(
        "--watchdog-container",
        default=None,
        metavar="NAME",
        help="Container name or ID prefix for manual watchdog mode",
    )
    args = parser.parse_args()

    app = DgxTopApp(
        watchdog_mode=WatchdogMode(args.watchdog),
        watchdog_grace_bytes=_parse_grace(args.watchdog_grace),
        watchdog_container=args.watchdog_container,
    )
    app.run()
