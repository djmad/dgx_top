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

## 3. History Store

[`dgxtop/history_store.py`](../dgxtop/history_store.py) persists trend data across restarts:
- Appends `HistoryPoint` records as newline-delimited JSON (JSONL) to `~/.local/state/dgxtop/history.jsonl` (overridable via `DGX_TOP_HISTORY_FILE`)
- On load, prunes records older than the configured maximum age and enforces a maximum point count; rewrites the file in place when compaction is needed
- Handles corrupt or missing lines gracefully

## 4. Textual UI

[`dgxtop/app.py`](../dgxtop/app.py) renders the dashboard:
- Two-line host summary including active watchdog state
- Unified main table with the watchdog target highlighted
- Optional detail pane
- Lower-third trend panel (zoomable with `+`/`-`, full-screen with `g`)
- Confirmation screens for destructive actions
- Watchdog engine that kills a container when free RAM falls below the grace threshold

## Data Flow

1. The UI schedules periodic refreshes via a lock-guarded async worker.
2. `DashboardCollector.sample()` builds a `DashboardSnapshot`.
3. The app sorts and renders rows based on the active sort field.
4. Each refresh appends a `HistoryPoint` to `HistoryStore`; the store handles persistence and compaction.
5. The trend panel compresses the in-memory history window to the current terminal width.
6. After rendering, `_watchdog_check()` compares free RAM against the grace threshold and kills the target container if needed (10-second cooldown).

## Watchdog

The watchdog runs inside the UI refresh loop. Two modes are supported:

- **biggest**: automatically targets the running container with the highest RSS
- **manual**: targets a user-selected container (set with `W`, cycled with `w`)

CLI flags `--watchdog` and `--watchdog-grace` configure the initial mode and grace threshold.

## Packaging

- Root launcher: [`dgx_top.py`](../dgx_top.py)
- Module entry point: `python -m dgxtop`
- Installed console script: `dgx-top`
