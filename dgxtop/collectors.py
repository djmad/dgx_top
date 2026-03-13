from __future__ import annotations

import datetime as dt
import glob
import os
import re
import signal
import subprocess
import time
from collections import defaultdict
from pathlib import Path

import psutil

try:
    import docker
    from docker.errors import APIError, DockerException, NotFound
except ImportError:  # pragma: no cover - dependency failure fallback
    docker = None
    APIError = Exception
    DockerException = Exception
    NotFound = Exception

try:
    import pynvml
except ImportError:  # pragma: no cover - dependency failure fallback
    pynvml = None

from .models import ContainerInfo, ContainerMemory, DashboardSnapshot, EntityRow, ProcessInfo, SystemSnapshot


CGROUP_PATTERNS = (
    re.compile(r"docker-([0-9a-f]{64})\.scope"),
    re.compile(r"/docker/([0-9a-f]{64})"),
    re.compile(r"/containers/([0-9a-f]{64})"),
)

DEFAULT_HOST_PROCESS_LIMIT = 18
HOST_PROCESS_THRESHOLD_BYTES = 128 * 1024 * 1024
HOST_PROCESS_THRESHOLD_CPU = 0.5
HISTORY_WINDOW_MIN = 60
HISTORY_WINDOW_MAX = 24 * 60 * 60


class DashboardCollector:
    def __init__(self) -> None:
        self._docker = self._init_docker()
        self._gpu_ready = self._init_nvml()
        self._gpu_handles = self._load_gpu_handles() if self._gpu_ready else []
        self._cgroup_cache: dict[str, Path | None] = {}
        self._proc_cpu_prev: dict[tuple[int, float], tuple[float, float]] = {}
        self._proc_history: dict[int, dict[str, float | int]] = {}  # pid -> {rss, cpu, timestamp}
        self._prev_net = psutil.net_io_counters()
        self._prev_net_ts = time.time()
        self._last_pmon_ts = 0.0
        self._pmon_gpu_percent: dict[int, float | None] = {}

        psutil.cpu_percent(interval=None)

    def _init_docker(self):
        if docker is None:
            return None
        try:
            client = docker.from_env()
            client.ping()
            return client
        except Exception:
            return None

    def _init_nvml(self) -> bool:
        if pynvml is None:
            return False
        try:
            pynvml.nvmlInit()
            return True
        except Exception:
            return False

    def _load_gpu_handles(self) -> list:
        handles = []
        try:
            count = pynvml.nvmlDeviceGetCount()
            for index in range(count):
                handles.append(pynvml.nvmlDeviceGetHandleByIndex(index))
        except Exception:
            return []
        return handles

    def shutdown(self) -> None:
        if self._gpu_ready:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass

    def restart_container(self, container_id: str) -> str:
        if self._docker is None:
            raise RuntimeError("Docker is not available")
        return self._container_action(container_id, "restart")

    def stop_container(self, container_id: str) -> str:
        if self._docker is None:
            raise RuntimeError("Docker is not available")
        return self._container_action(container_id, "stop")

    def terminate_process(self, pid: int) -> str:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError as error:
            raise RuntimeError(f"Process {pid} is no longer running") from error
        except PermissionError as error:
            raise RuntimeError(f"Permission denied terminating pid {pid}") from error
        return f"Sent SIGTERM to pid {pid}"

    def _container_action(self, container_id: str, action: str) -> str:
        assert self._docker is not None

        try:
            container = self._docker.containers.get(container_id)
            getattr(container, action)(timeout=10)
        except NotFound as error:
            short_id = container_id[:12]
            raise RuntimeError(f"Container {short_id} no longer exists") from error
        except APIError as error:
            short_id = container_id[:12]
            detail = getattr(error, "explanation", None) or str(error)
            raise RuntimeError(f"Docker {action} failed for {short_id}: {detail}") from error
        except DockerException as error:
            short_id = container_id[:12]
            raise RuntimeError(f"Docker {action} failed for {short_id}: {error}") from error

        action_label = f"{action}ed" if action.endswith("t") else f"{action}ped"
        return f"{action_label.capitalize()} {container.name}"

    def sample(self, include_stopped: bool = False) -> DashboardSnapshot:
        now = time.time()
        gpu_state = self._read_gpu_state(now)
        processes, host_processes = self._read_processes(now, gpu_state["process_memory"], gpu_state["process_percent"])
        containers = self._read_containers(processes, include_stopped)

        rows = self._build_rows(containers, host_processes, gpu_state["util_percent"], now)
        system = self._read_system(include_stopped, gpu_state, containers, now)

        return DashboardSnapshot(
            system=system,
            rows=rows,
            containers=containers,
            host_processes={f"host:{proc.pid}": proc for proc in host_processes},
            timestamp=now,
        )

    def _read_processes(
        self,
        now: float,
        gpu_memory_by_pid: dict[int, int],
        gpu_percent_by_pid: dict[int, float | None],
    ) -> tuple[dict[str, list[ProcessInfo]], list[ProcessInfo]]:
        container_processes: dict[str, list[ProcessInfo]] = defaultdict(list)
        host_processes: list[ProcessInfo] = []
        seen_cpu_keys: set[tuple[int, float]] = set()

        attrs = ["pid", "ppid", "name", "cmdline", "username", "status", "create_time"]
        for proc in psutil.process_iter(attrs=attrs):
            try:
                info = proc.info
                create_time = float(info["create_time"])
                cpu_key = (proc.pid, create_time)
                cpu_times = proc.cpu_times()
                proc_cpu_time = cpu_times.user + cpu_times.system
                cpu_percent = self._calc_process_cpu_percent(cpu_key, proc_cpu_time, now)
                seen_cpu_keys.add(cpu_key)

                mem = proc.memory_info()
                command = " ".join(info["cmdline"]) if info["cmdline"] else info["name"] or str(proc.pid)
                container_id = self._container_id_for_pid(proc.pid)
                process = ProcessInfo(
                    pid=proc.pid,
                    ppid=info["ppid"],
                    name=info["name"] or str(proc.pid),
                    command=command,
                    username=info.get("username") or "?",
                    cpu_percent=cpu_percent,
                    rss_bytes=mem.rss,
                    gpu_memory_bytes=gpu_memory_by_pid.get(proc.pid, 0),
                    gpu_percent=gpu_percent_by_pid.get(proc.pid),
                    container_id=container_id,
                    status=info.get("status") or "",
                )

                if container_id:
                    container_processes[container_id].append(process)
                elif self._is_relevant_host_process(process):
                    host_processes.append(process)
                    # Update history for activity tracking
                    self._update_process_history(process, now)
            except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
                continue

        self._proc_cpu_prev = {key: value for key, value in self._proc_cpu_prev.items() if key in seen_cpu_keys}

        host_processes.sort(key=lambda proc: (proc.ram_sum_bytes, proc.cpu_percent), reverse=True)
        return container_processes, host_processes[:DEFAULT_HOST_PROCESS_LIMIT]

    def _read_containers(self, processes: dict[str, list[ProcessInfo]], include_stopped: bool) -> dict[str, ContainerInfo]:
        containers: dict[str, ContainerInfo] = {}

        if self._docker is None:
            return containers

        try:
            docker_containers = self._docker.containers.list(all=True)
        except Exception:
            return containers

        for container in docker_containers:
            try:
                container.reload()
                status = container.status or container.attrs.get("State", {}).get("Status", "unknown")
                if status != "running" and not include_stopped:
                    continue
                status = self._shorten_docker_status(status)

                attrs = container.attrs or {}
                state = attrs.get("State", {})
                config = attrs.get("Config", {})
                host_config = attrs.get("HostConfig", {})
                memory = self._read_cgroup_memory(container.id)
                members = sorted(processes.get(container.id, []), key=lambda proc: (proc.ram_sum_bytes, proc.cpu_percent), reverse=True)

                main_pid = state.get("Pid") or (members[0].pid if members else None)
                info = ContainerInfo(
                    container_id=container.id,
                    name=container.name,
                    image=self._container_image(container),
                    status=status,
                    main_pid=main_pid if isinstance(main_pid, int) and main_pid > 0 else None,
                    processes=members,
                    cpu_percent=sum(proc.cpu_percent for proc in members),
                    gpu_percent=self._aggregate_gpu_percent(members),
                    rss_bytes=sum(proc.rss_bytes for proc in members),
                    gpu_memory_bytes=sum(proc.gpu_memory_bytes for proc in members),
                    memory=memory,
                    command=self._join_command(config.get("Entrypoint"), config.get("Cmd")),
                    ports=self._format_ports(attrs.get("NetworkSettings", {}).get("Ports", {})),
                    runtime=host_config.get("Runtime", ""),
                    uptime=self._state_uptime(state),
                )
                containers[container.id] = info
            except Exception:
                continue

        return containers

    def _build_rows(
        self,
        containers: dict[str, ContainerInfo],
        host_processes: list[ProcessInfo],
        system_gpu_percent: float | None,
        now: float,
    ) -> list[EntityRow]:
        rows: list[EntityRow] = []
        total_gpu_memory = sum(container.gpu_memory_bytes for container in containers.values()) + sum(
            proc.gpu_memory_bytes for proc in host_processes
        )

        for container in containers.values():
            gpu_percent = container.gpu_percent
            if (
                gpu_percent is None
                and system_gpu_percent is not None
                and total_gpu_memory
                and container.gpu_memory_bytes / total_gpu_memory >= 0.85
            ):
                gpu_percent = system_gpu_percent
            rows.append(
                EntityRow(
                    key=f"docker:{container.container_id}",
                    kind="docker",
                    name=container.name,
                    pid=container.main_pid,
                    image=container.image,
                    command=container.command,
                    cpu_percent=container.cpu_percent,
                    gpu_percent=gpu_percent,
                    ram_sum_bytes=container.ram_sum_bytes,
                    ram_rss_bytes=container.rss_bytes,
                    ram_cgroup_bytes=container.memory.total_bytes or None,
                    gpu_memory_bytes=container.gpu_memory_bytes,
                    status=container.status,
                )
            )

        for proc in host_processes:
            gpu_percent = proc.gpu_percent
            if (
                gpu_percent is None
                and system_gpu_percent is not None
                and total_gpu_memory
                and proc.gpu_memory_bytes / total_gpu_memory >= 0.85
            ):
                gpu_percent = system_gpu_percent
            # Derive status based on activity for host processes
            status = self._derive_process_status(proc, now)
            rows.append(
                EntityRow(
                    key=f"host:{proc.pid}",
                    kind="host",
                    name=proc.name,
                    pid=proc.pid,
                    image=None,
                    command=proc.command,
                    cpu_percent=proc.cpu_percent,
                    gpu_percent=gpu_percent,
                    ram_sum_bytes=proc.ram_sum_bytes,
                    ram_rss_bytes=proc.rss_bytes,
                    ram_cgroup_bytes=None,
                    gpu_memory_bytes=proc.gpu_memory_bytes,
                    status=status,
                )
            )

        return rows

    def _read_system(
        self,
        include_stopped: bool,
        gpu_state: dict,
        containers: dict[str, ContainerInfo],
        now: float,
    ) -> SystemSnapshot:
        cpu_percent = psutil.cpu_percent(interval=None)
        cpu_temp = self._cpu_temp()
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage("/")

        net = psutil.net_io_counters()
        delta_t = max(now - self._prev_net_ts, 1e-6)
        recv_rate = max(0.0, (net.bytes_recv - self._prev_net.bytes_recv) / delta_t)
        send_rate = max(0.0, (net.bytes_sent - self._prev_net.bytes_sent) / delta_t)
        self._prev_net = net
        self._prev_net_ts = now

        running = sum(1 for container in containers.values() if container.status == "running")
        stopped = 0
        if include_stopped and self._docker is not None:
            try:
                all_containers = self._docker.containers.list(all=True)
                stopped = sum(1 for container in all_containers if container.status != "running")
            except Exception:
                stopped = 0
        elif self._docker is not None:
            try:
                all_containers = self._docker.containers.list(all=True)
                stopped = sum(1 for container in all_containers if container.status != "running")
            except Exception:
                stopped = 0

        return SystemSnapshot(
            cpu_percent=cpu_percent,
            cpu_temp_c=cpu_temp,
            load_avg=os.getloadavg(),
            ram_used_bytes=vm.used,
            ram_total_bytes=vm.total,
            ram_percent=vm.percent,
            swap_used_bytes=swap.used,
            swap_total_bytes=swap.total,
            disk_used_bytes=disk.used,
            disk_total_bytes=disk.total,
            disk_percent=disk.percent,
            net_recv_rate=recv_rate,
            net_send_rate=send_rate,
            gpu_name=gpu_state["name"],
            gpu_percent=gpu_state["util_percent"],
            gpu_temp_c=gpu_state["temp_c"],
            gpu_memory_used_bytes=gpu_state["memory_used"],
            gpu_memory_total_bytes=gpu_state["memory_total"],
            gpu_memory_percent=gpu_state["memory_percent"],
            running_containers=running,
            stopped_containers=stopped,
        )

    def _read_gpu_state(self, now: float) -> dict:
        state = {
            "name": None,
            "util_percent": None,
            "temp_c": None,
            "memory_used": 0,
            "memory_total": 0,
            "memory_percent": None,
            "process_memory": {},
            "process_percent": {},
        }

        if not self._gpu_ready or not self._gpu_handles:
            return state

        total_util = 0.0
        total_temp = 0.0
        temp_count = 0
        names: list[str] = []

        for handle in self._gpu_handles:
            try:
                name = pynvml.nvmlDeviceGetName(handle)
                names.append(name.decode() if isinstance(name, bytes) else str(name))
            except Exception:
                pass
            try:
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                total_util += float(util.gpu)
            except Exception:
                pass
            try:
                total_temp += float(pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU))
                temp_count += 1
            except Exception:
                pass
            try:
                mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                state["memory_used"] += int(mem.used)
                state["memory_total"] += int(mem.total)
            except Exception:
                pass

            for proc in self._gpu_processes_for_handle(handle):
                pid = int(proc.pid)
                used = int(getattr(proc, "usedGpuMemory", 0) or 0)
                state["process_memory"][pid] = max(state["process_memory"].get(pid, 0), used)

        if self._gpu_handles:
            state["util_percent"] = total_util / len(self._gpu_handles)
        if temp_count:
            state["temp_c"] = total_temp / temp_count
        if state["memory_used"] == 0 and state["process_memory"]:
            state["memory_used"] = sum(state["process_memory"].values())
        if state["memory_total"]:
            state["memory_percent"] = state["memory_used"] / state["memory_total"] * 100.0

        if names:
            state["name"] = names[0] if len(names) == 1 else f"{names[0]} x{len(names)}"

        if now - self._last_pmon_ts >= 4.0:
            self._pmon_gpu_percent = self._read_pmon_gpu_percent()
            self._last_pmon_ts = now
        state["process_percent"] = self._pmon_gpu_percent
        return state

    def _gpu_processes_for_handle(self, handle) -> list:
        all_procs = []
        getter_groups = [
            (
                getattr(pynvml, "nvmlDeviceGetComputeRunningProcesses_v2", None),
                getattr(pynvml, "nvmlDeviceGetComputeRunningProcesses", None),
            ),
            (
                getattr(pynvml, "nvmlDeviceGetGraphicsRunningProcesses_v2", None),
                getattr(pynvml, "nvmlDeviceGetGraphicsRunningProcesses", None),
            ),
        ]
        for primary, fallback in getter_groups:
            getter = primary or fallback
            if getter is None:
                continue
            try:
                all_procs.extend(getter(handle))
            except Exception:
                continue
        return all_procs

    def _read_pmon_gpu_percent(self) -> dict[int, float | None]:
        try:
            result = subprocess.run(
                ["nvidia-smi", "pmon", "-c", "1", "-s", "um"],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
        except Exception:
            return {}

        process_gpu: dict[int, float | None] = {}
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            try:
                pid = int(parts[1])
            except ValueError:
                continue
            sm = self._parse_percent_field(parts[3])
            mem = self._parse_percent_field(parts[4]) if len(parts) > 4 else None
            process_gpu[pid] = sm if sm is not None else mem
        return process_gpu

    def _parse_percent_field(self, value: str) -> float | None:
        if value == "-":
            return None
        try:
            return float(value)
        except ValueError:
            return None

    def _cpu_temp(self) -> float | None:
        try:
            temps = psutil.sensors_temperatures()
        except Exception:
            return self._cpu_temp_from_sysfs()

        if not temps:
            return self._cpu_temp_from_sysfs()

        for name, entries in temps.items():
            lname = name.lower()
            for entry in entries:
                label = (entry.label or "").lower()
                if "cpu" in lname or "core" in label or "package" in label:
                    return float(entry.current)
        for entries in temps.values():
            if entries:
                return float(entries[0].current)
        return self._cpu_temp_from_sysfs()

    def _cpu_temp_from_sysfs(self) -> float | None:
        candidates = glob.glob("/sys/class/thermal/thermal_zone*/temp")
        for path in candidates:
            try:
                raw = Path(path).read_text().strip()
                value = float(raw)
                if value > 1000:
                    value /= 1000.0
                if 0 < value < 150:
                    return value
            except Exception:
                continue
        return None

    def _calc_process_cpu_percent(self, key: tuple[int, float], proc_cpu_time: float, now: float) -> float:
        previous = self._proc_cpu_prev.get(key)
        self._proc_cpu_prev[key] = (proc_cpu_time, now)
        if previous is None:
            return 0.0
        previous_cpu, previous_ts = previous
        elapsed = now - previous_ts
        if elapsed <= 0:
            return 0.0
        return max(0.0, (proc_cpu_time - previous_cpu) / elapsed * 100.0)

    def _container_id_for_pid(self, pid: int) -> str | None:
        cgroup_file = Path(f"/proc/{pid}/cgroup")
        try:
            content = cgroup_file.read_text()
        except Exception:
            return None

        for pattern in CGROUP_PATTERNS:
            match = pattern.search(content)
            if match:
                return match.group(1)
        return None

    def _read_cgroup_memory(self, container_id: str) -> ContainerMemory:
        path = self._find_cgroup_dir(container_id)
        if path is None:
            return ContainerMemory()

        memory = ContainerMemory()
        memory.total_bytes = self._read_int_file(path / "memory.current")
        memory.peak_bytes = self._read_int_file(path / "memory.peak")

        stat_path = path / "memory.stat"
        try:
            for line in stat_path.read_text().splitlines():
                key, value = line.split(maxsplit=1)
                amount = int(value)
                if key == "anon":
                    memory.anon_bytes = amount
                elif key == "file":
                    memory.file_bytes = amount
                elif key == "kernel":
                    memory.kernel_bytes = amount
                elif key == "shmem":
                    memory.shmem_bytes = amount
                elif key == "pagetables":
                    memory.pagetables_bytes = amount
        except Exception:
            pass
        return memory

    def _find_cgroup_dir(self, container_id: str) -> Path | None:
        if container_id in self._cgroup_cache:
            return self._cgroup_cache[container_id]

        direct_candidates = [
            Path(f"/sys/fs/cgroup/system.slice/docker-{container_id}.scope"),
            Path(f"/sys/fs/cgroup/docker/{container_id}"),
        ]
        for candidate in direct_candidates:
            if candidate.exists():
                self._cgroup_cache[container_id] = candidate
                return candidate

        matches = glob.glob(f"/sys/fs/cgroup/**/docker-{container_id}.scope", recursive=True)
        if matches:
            path = Path(matches[0])
            self._cgroup_cache[container_id] = path
            return path

        self._cgroup_cache[container_id] = None
        return None

    def _read_int_file(self, path: Path) -> int:
        try:
            value = path.read_text().strip()
            return 0 if value == "max" else int(value)
        except Exception:
            return 0

    def _state_uptime(self, state: dict) -> str:
        started_at = state.get("StartedAt")
        if not started_at or started_at.startswith("0001-01-01"):
            return ""
        try:
            started = dt.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            delta = dt.datetime.now(dt.timezone.utc) - started
            seconds = max(0, int(delta.total_seconds()))
        except Exception:
            return ""
        return self._format_duration(seconds)

    def _format_duration(self, seconds: int) -> str:
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"
        if seconds < 86400:
            return f"{seconds // 3600}h"
        return f"{seconds // 86400}d"

    def _container_image(self, container) -> str:
        try:
            tags = container.image.tags
            if tags:
                return tags[0]
        except Exception:
            pass
        return getattr(container.image, "short_id", "<none>")

    def _aggregate_gpu_percent(self, processes: list[ProcessInfo]) -> float | None:
        numeric = [proc.gpu_percent for proc in processes if proc.gpu_percent is not None]
        if not numeric:
            return None
        return sum(numeric)

    def _format_ports(self, ports: dict) -> str:
        if not ports:
            return ""
        items = []
        for container_port, mappings in ports.items():
            if not mappings:
                items.append(container_port)
                continue
            for mapping in mappings:
                items.append(f"{mapping.get('HostPort', '?')}->{container_port}")
        return ", ".join(items[:4])

    def _join_command(self, entrypoint, command) -> str:
        values = []
        if entrypoint:
            values.extend(entrypoint if isinstance(entrypoint, list) else [str(entrypoint)])
        if command:
            values.extend(command if isinstance(command, list) else [str(command)])
        return " ".join(values)

    def _is_relevant_host_process(self, process: ProcessInfo) -> bool:
        return (
            process.gpu_memory_bytes > 0
            or process.cpu_percent >= HOST_PROCESS_THRESHOLD_CPU
            or process.rss_bytes >= HOST_PROCESS_THRESHOLD_BYTES
        )

    def _derive_process_status(self, process: ProcessInfo, now: float) -> str:
        """Derive status based on activity in 5-second window.

        Returns:
            "GPU" if gpu_percent > 0.01 or gpu_memory_bytes > 0
            "CPU" if cpu_percent > 0.01 and not GPU-active
            "RAM" if rss changed significantly in 5s window and not CPU/GPU-active
            "idle" if no significant activity
        """
        # Check for GPU activity
        gpu_percent = process.gpu_percent or 0.0
        if gpu_percent > 0.01 or process.gpu_memory_bytes > 0:
            return "GPU"

        # Check for CPU activity
        if process.cpu_percent > 0.01:
            return "CPU"

        # Check for memory activity (rss changed in 5-second window)
        pid = process.pid
        if pid in self._proc_history:
            prev = self._proc_history[pid]
            prev_rss = prev.get("rss", 0)
            prev_time = prev.get("timestamp", 0)
            time_diff = now - prev_time

            if time_diff >= 5.0 and prev_rss > 0:
                rss_diff = abs(process.rss_bytes - prev_rss)
                # Consider it active if rss changed by more than 1MB or 10%
                if rss_diff > 1024 * 1024 or (prev_rss > 0 and rss_diff / prev_rss > 0.1):
                    return "RAM"

        return "idle"

    def _update_process_history(self, process: ProcessInfo, now: float) -> None:
        """Update the process history cache for activity tracking."""
        self._proc_history[process.pid] = {
            "rss": process.rss_bytes,
            "cpu": process.cpu_percent,
            "timestamp": now,
        }

    def _shorten_docker_status(self, status: str) -> str:
        """Shorten Docker status values to fit in 7-char column."""
        mapping = {
            "running": "run",
            "exited": "exit",
            "paused": "pause",
            "restarting": "rest",
            "dead": "dead",
            "created": "creat",
            "removed": "rmov",
            "unknown": "unkn",
        }
        return mapping.get(status, status[:7])
