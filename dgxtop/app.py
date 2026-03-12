from __future__ import annotations

import asyncio
import math
from collections import deque

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static

from .collectors import DashboardCollector, HISTORY_WINDOW_MAX, HISTORY_WINDOW_MIN
from .models import ContainerInfo, DashboardSnapshot, EntityRow, HistoryPoint, ProcessInfo


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
NAME_COLUMN_KEY = "name"
NAME_COLUMN_MIN_WIDTH = 8


def normalize_series(values: list[float | None], width: int) -> list[float]:
    width = max(8, width)
    cleaned = [0.0 if value is None or math.isnan(value) else max(0.0, min(100.0, value)) for value in values]
    if not cleaned:
        return [0.0] * width
    if len(cleaned) > width:
        bucket = len(cleaned) / width
        compressed: list[float] = []
        for index in range(width):
            start = int(index * bucket)
            end = max(start + 1, int((index + 1) * bucket))
            chunk = cleaned[start:end]
            compressed.append(sum(chunk) / len(chunk))
        cleaned = compressed
    elif len(cleaned) < width:
        cleaned = ([cleaned[0]] * (width - len(cleaned))) + cleaned
    return cleaned


def render_two_line_chart(values: list[float | None], width: int) -> tuple[str, str]:
    cleaned = normalize_series(values, width)
    top_chars: list[str] = []
    bottom_chars: list[str] = []
    for value in cleaned:
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


def render_metric_block(label: str, latest: float | None, values: list[float | None], width: int) -> tuple[str, str]:
    prefix = f"{label} {fmt_percent(latest):>5}% "
    chart_width = max(8, width - len(prefix))
    top, bottom = render_two_line_chart(values, chart_width)
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

    #main-area {
        height: 2fr;
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
        Binding("g", "sort_gpu", "GPU"),
        Binding("m", "sort_ram_sum", "RAM"),
        Binding("v", "sort_gpu_mem", "VRAM"),
        Binding("plus,equals", "expand_history", "History+"),
        Binding("minus", "shrink_history", "History-"),
        Binding("k", "kill_selected", "Kill"),
        Binding("r", "restart_selected", "Restart"),
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

    def __init__(self) -> None:
        super().__init__()
        self.collector = DashboardCollector()
        self.snapshot: DashboardSnapshot | None = None
        self.history_window = 3600
        self.show_stopped = False
        self.details_visible = False
        self.sort_field = "ram_sum_bytes"
        self.sort_desc = True
        self.selected_key: str | None = None
        self._visible_keys: list[str] = []
        self._refresh_lock = asyncio.Lock()
        self.history: deque[HistoryPoint] = deque(maxlen=HISTORY_WINDOW_MAX)

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

        self.query_one("#footer", Static).update(
            "Keys: q quit  d detail  c cpu  g gpu  m ram-sum  v vram  x stopped  k kill  r restart  +/- zoom"
        )

        self.set_interval(2.0, self._schedule_refresh)
        self._schedule_refresh()

    def on_unmount(self) -> None:
        self.collector.shutdown()

    async def refresh_dashboard(self) -> None:
        if self._refresh_lock.locked():
            return
        async with self._refresh_lock:
            snapshot = await asyncio.to_thread(self.collector.sample, self.show_stopped)
            self.snapshot = snapshot
            self.history.append(
                HistoryPoint(
                    timestamp=snapshot.timestamp,
                    cpu_percent=snapshot.system.cpu_percent,
                    ram_percent=snapshot.system.ram_percent,
                    gpu_percent=snapshot.system.gpu_percent,
                    gpu_memory_percent=snapshot.system.gpu_memory_percent,
                )
            )
            self._refresh_summary()
            self._refresh_table()
            self._refresh_details()
            self._refresh_trends()

    def _schedule_refresh(self) -> None:
        self.run_worker(self.refresh_dashboard(), exclusive=True)

    def _refresh_summary(self) -> None:
        assert self.snapshot is not None
        system = self.snapshot.system
        line_one = (
            f"DGX_TOP  refresh:2s  history:{fmt_history_window(self.history_window)}  "
            f"sort:{self.sort_label()}  show:{'all' if self.show_stopped else 'running'}  "
            f"docker:{system.running_containers} up / {system.stopped_containers} stopped"
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
        visible_keys: list[str] = []
        for row in rows:
            visible_keys.append(row.key)
            type_cell = Text("D", style="bold green") if row.kind == "docker" else Text("H", style="bold cyan")
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
        lines = [
            process.name,
            f"pid: {process.pid}   ppid: {process.ppid}   user: {process.username}   status: {process.status or '--'}",
            f"cpu: {process.cpu_percent:.1f}%   rss: {fmt_bytes(process.rss_bytes)}   gpu mem: {fmt_bytes(process.gpu_memory_bytes)}",
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

        lines = [f"Window: {fmt_history_window(self.history_window)}"]
        total_width = max(48, trends.size.width - 4)
        block_width = max(22, (total_width - 2) // 2)
        series = [
            render_metric_block("CPU ", window_points[-1].cpu_percent, [point.cpu_percent for point in window_points], block_width),
            render_metric_block("GPU ", window_points[-1].gpu_percent, [point.gpu_percent for point in window_points], block_width),
            render_metric_block("RAM ", window_points[-1].ram_percent, [point.ram_percent for point in window_points], block_width),
            render_metric_block("VRAM", window_points[-1].gpu_memory_percent, [point.gpu_memory_percent for point in window_points], block_width),
        ]
        for left, right in ((series[0], series[1]), (series[2], series[3])):
            lines.append(f"{left[0]}  {right[0]}")
            lines.append(f"{left[1]}  {right[1]}")
        trends.update("\n".join(lines))

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

    def on_resize(self) -> None:
        if self.snapshot is None:
            return
        self._refresh_table()
        self._refresh_trends()

    def action_toggle_stopped(self) -> None:
        self.show_stopped = not self.show_stopped
        self.run_worker(self.refresh_dashboard(), exclusive=True)

    def action_expand_history(self) -> None:
        self.history_window = min(HISTORY_WINDOW_MAX, self.history_window * 2)
        self._refresh_summary()
        self._refresh_trends()

    def action_shrink_history(self) -> None:
        self.history_window = max(HISTORY_WINDOW_MIN, self.history_window // 2)
        self._refresh_summary()
        self._refresh_trends()

    async def action_kill_selected(self) -> None:
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

        confirmed = await self.push_screen_wait(ConfirmActionScreen(title, message))
        if not confirmed:
            return

        try:
            if isinstance(entity, ContainerInfo):
                message = await asyncio.to_thread(self.collector.stop_container, entity.container_id)
            else:
                message = await asyncio.to_thread(self.collector.terminate_process, entity.pid)
            self.notify(message)
            await self.refresh_dashboard()
        except Exception as error:
            self.notify(str(error), severity="error")

    async def action_restart_selected(self) -> None:
        entity = self.current_entity()
        if not isinstance(entity, ContainerInfo):
            self.notify("Restart works on docker rows only", severity="warning")
            return

        confirmed = await self.push_screen_wait(
            ConfirmActionScreen("Restart container", f"Restart {entity.name}?")
        )
        if not confirmed:
            return
        try:
            message = await asyncio.to_thread(self.collector.restart_container, entity.container_id)
            self.notify(message)
            await self.refresh_dashboard()
        except Exception as error:
            self.notify(str(error), severity="error")


def main() -> None:
    app = DgxTopApp()
    app.run()
