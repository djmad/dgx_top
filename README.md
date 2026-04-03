# dgx_top

`dgx_top` is a terminal dashboard for NVIDIA DGX Spark style machines and similar Linux hosts that run Docker workloads on shared GPU hardware.

It combines:
- host CPU, RAM, swap, disk, network, and temperature stats
- Docker container discovery with per-container aggregation
- relevant non-Docker host processes in the same overview
- per-process GPU memory mapping
- best-effort per-process GPU load via `nvidia-smi pmon`
- keyboard-first controls with optional mouse sorting

## Features

- Unified table for Docker containers and high-signal host processes
- Compact two-line host summary at the top
- Separate CPU, GPU, RAM sum, RSS, cgroup memory, and GPU memory columns
- Lower-third two-line trend graphs for CPU, GPU, RAM, VRAM, and host network up/down
- Persistent trend history across restarts, stored as JSONL under `~/.local/state/dgxtop/history.jsonl` by default
- Optional detail pane with commands, ports, memory split, and inner processes
- Best-effort per-container network throughput in the detail pane for running Docker containers
- Optional per-process `ebpf` traffic in the detail pane when the dashboard runs with root privileges; otherwise it falls back to namespace traffic
- Stop / terminate and restart actions from inside the dashboard
- Public-package friendly layout with `pyproject.toml`, tests, and docs

## Requirements

- Linux
- Python 3.10+
- Docker daemon access for container discovery and actions
- NVIDIA drivers with `nvidia-smi` available for GPU metrics
- Optional CPU temperature sensors exposed through `psutil` or `/sys/class/thermal`

## Installation

### Local development

```bash
cd /path/to/dgx_top
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### Package install

```bash
cd /path/to/dgx_top
python3 -m pip install .
```

## Usage

### Run from source

```bash
cd /path/to/dgx_top
.venv/bin/python dgx_top.py
```

### Run as a module

```bash
python3 -m dgxtop
```

### Run after install

```bash
dgx-top
```

## Controls

- `q`: quit
- `d`: toggle detail pane
- `c`: sort by CPU
- `g`: toggle full-screen graph view
- `shift+g`: sort by GPU load
- `m`: sort by RAM sum
- `v`: sort by GPU memory
- `x`: toggle stopped containers
- `k`: stop selected container or send `SIGTERM` to the selected host process
- `r`: restart selected container
- `+`: double the visible trend history window
- `-`: halve the visible trend history window

## Notes

- GPU memory comes from NVML per-process data.
- On NVIDIA GB10 systems the aggregate GPU memory total may report as unsupported; in that case the header falls back to per-process used memory and leaves total as `--`.
- Per-process GPU load is best-effort and depends on `nvidia-smi pmon`.
- Per-process `ebpf` network traffic requires running the dashboard with privileges that allow `bpftrace`; unprivileged runs fall back to network-namespace traffic.
- The root `dgx_top.py` file is a thin source launcher. The package entry point is `dgxtop`.
- Set `DGX_TOP_HISTORY_FILE` to override the history file location.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Development](docs/DEVELOPMENT.md)
