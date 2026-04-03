# Architecture

`dgx_top` is split into three layers:

## 1. Collectors

[`dgxtop/collectors.py`](../dgxtop/collectors.py) gathers live data from:
- `psutil` for host CPU, memory, swap, disk, network, and process data
- Docker SDK for container discovery, restart, and stop actions
- cgroup v2 files for per-container memory breakdown
- NVML for GPU device stats and per-process GPU memory
- `nvidia-smi pmon` for best-effort per-process GPU load

## 2. Models

[`dgxtop/models.py`](../dgxtop/models.py) defines the typed snapshots passed from collectors into the UI:
- `ProcessInfo`
- `ContainerInfo`
- `EntityRow`
- `SystemSnapshot`
- `DashboardSnapshot`
- `HistoryPoint`

## 3. Textual UI

[`dgxtop/app.py`](../dgxtop/app.py) renders the dashboard:
- two-line host summary
- unified main table
- optional detail pane
- lower-third trend panel
- confirmation screens for destructive actions

## Data Flow

1. The UI schedules periodic refreshes.
2. `DashboardCollector.sample()` builds a `DashboardSnapshot`.
3. The app sorts and renders rows based on the active field.
4. History points are stored for CPU, RAM, GPU, VRAM, and host network rates, then persisted to a local JSONL history file.
5. The trend panel compresses the visible window to the current terminal width.

## Packaging

- Root launcher: [`dgx_top.py`](../dgx_top.py)
- Module entry point: `python -m dgxtop`
- Installed console script: `dgx-top`
