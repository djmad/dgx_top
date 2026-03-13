from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


EntityKind = Literal["docker", "host"]


@dataclass(slots=True)
class ProcessInfo:
    pid: int
    ppid: int
    name: str
    command: str
    username: str
    cpu_percent: float
    rss_bytes: int
    gpu_memory_bytes: int = 0
    gpu_percent: float | None = None
    container_id: str | None = None
    status: str = ""
    net_recv_rate: float = 0.0
    net_send_rate: float = 0.0
    net_namespace: str | None = None
    net_namespace_processes: int = 0
    net_source: str = "ns"
    # History tracking for activity detection
    prev_rss_bytes: int | None = None
    prev_cpu_percent: float | None = None
    last_update_time: float | None = None

    @property
    def ram_sum_bytes(self) -> int:
        return self.rss_bytes + self.gpu_memory_bytes


@dataclass(slots=True)
class ContainerMemory:
    total_bytes: int = 0
    peak_bytes: int = 0
    anon_bytes: int = 0
    file_bytes: int = 0
    kernel_bytes: int = 0
    shmem_bytes: int = 0
    pagetables_bytes: int = 0


@dataclass(slots=True)
class ContainerInfo:
    container_id: str
    name: str
    image: str
    status: str
    main_pid: int | None
    processes: list[ProcessInfo] = field(default_factory=list)
    cpu_percent: float = 0.0
    gpu_percent: float | None = None
    rss_bytes: int = 0
    gpu_memory_bytes: int = 0
    memory: ContainerMemory = field(default_factory=ContainerMemory)
    command: str = ""
    ports: str = ""
    runtime: str = ""
    uptime: str = ""
    net_recv_rate: float = 0.0
    net_send_rate: float = 0.0

    @property
    def ram_sum_bytes(self) -> int:
        base = max(self.rss_bytes, self.memory.total_bytes)
        return base + self.gpu_memory_bytes


@dataclass(slots=True)
class EntityRow:
    key: str
    kind: EntityKind
    name: str
    pid: int | None
    image: str | None
    command: str | None
    cpu_percent: float
    gpu_percent: float | None
    ram_sum_bytes: int
    ram_rss_bytes: int
    ram_cgroup_bytes: int | None
    gpu_memory_bytes: int
    status: str


@dataclass(slots=True)
class SystemSnapshot:
    cpu_percent: float
    cpu_temp_c: float | None
    load_avg: tuple[float, float, float]
    ram_used_bytes: int
    ram_total_bytes: int
    ram_percent: float
    swap_used_bytes: int
    swap_total_bytes: int
    disk_used_bytes: int
    disk_total_bytes: int
    disk_percent: float
    net_recv_rate: float
    net_send_rate: float
    gpu_name: str | None = None
    gpu_percent: float | None = None
    gpu_temp_c: float | None = None
    gpu_memory_used_bytes: int = 0
    gpu_memory_total_bytes: int = 0
    gpu_memory_percent: float | None = None
    running_containers: int = 0
    stopped_containers: int = 0


@dataclass(slots=True)
class DashboardSnapshot:
    system: SystemSnapshot
    rows: list[EntityRow]
    containers: dict[str, ContainerInfo]
    host_processes: dict[str, ProcessInfo]
    timestamp: float


@dataclass(slots=True)
class HistoryPoint:
    timestamp: float
    cpu_percent: float
    ram_percent: float
    gpu_percent: float | None
    gpu_memory_percent: float | None
    net_recv_rate: float = 0.0
    net_send_rate: float = 0.0
