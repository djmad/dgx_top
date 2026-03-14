import signal
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from dgxtop.app import ConfirmActionScreen, DgxTopApp
from dgxtop.collectors import APIError, DashboardCollector, EbpfProcessTrafficCollector, NotFound
from dgxtop.models import ContainerInfo, ProcessInfo


class CollectorActionTests(unittest.TestCase):
    def test_ebpf_snapshot_commit_converts_bytes_to_rates(self):
        collector = EbpfProcessTrafficCollector.__new__(EbpfProcessTrafficCollector)
        collector._lock = threading.Lock()
        collector._rates = {}
        collector._tx_snapshot = {123: 4000}
        collector._rx_snapshot = {123: 2000, 456: 1000}

        EbpfProcessTrafficCollector._commit_snapshots(collector)

        self.assertEqual(collector._rates[123], (1000.0, 2000.0))
        self.assertEqual(collector._rates[456], (500.0, 0.0))

    def test_terminate_process_sends_sigterm(self):
        collector = DashboardCollector.__new__(DashboardCollector)

        with patch("dgxtop.collectors.os.kill") as kill:
            message = collector.terminate_process(4321)

        kill.assert_called_once_with(4321, signal.SIGTERM)
        self.assertEqual(message, "Sent SIGTERM to pid 4321")

    def test_terminate_process_reports_missing_pid(self):
        collector = DashboardCollector.__new__(DashboardCollector)

        with patch("dgxtop.collectors.os.kill", side_effect=ProcessLookupError):
            with self.assertRaisesRegex(RuntimeError, "Process 4321 is no longer running"):
                collector.terminate_process(4321)

    def test_terminate_process_reports_permission_denied(self):
        collector = DashboardCollector.__new__(DashboardCollector)

        with patch("dgxtop.collectors.os.kill", side_effect=PermissionError):
            with self.assertRaisesRegex(RuntimeError, "Permission denied terminating pid 4321"):
                collector.terminate_process(4321)

    def test_stop_container_reports_missing_container(self):
        collector = DashboardCollector.__new__(DashboardCollector)
        client = Mock()
        client.containers.get.side_effect = NotFound("gone")
        collector._docker = client

        with self.assertRaisesRegex(RuntimeError, "Container abcdef123456 no longer exists"):
            collector.stop_container("abcdef1234567890")

    def test_stop_container_reports_docker_api_error(self):
        collector = DashboardCollector.__new__(DashboardCollector)
        container = Mock(name="trainer")
        container.name = "trainer"
        container.stop.side_effect = APIError("daemon rejected request")
        client = Mock()
        client.containers.get.return_value = container
        collector._docker = client

        with self.assertRaisesRegex(RuntimeError, "Docker stop failed for abcdef123456"):
            collector.stop_container("abcdef1234567890")

    def test_read_containers_keeps_running_container_when_network_rates_are_enabled(self):
        collector = DashboardCollector.__new__(DashboardCollector)
        collector._docker = Mock()
        collector._cgroup_cache = {}
        collector._container_image = Mock(return_value="trainer:latest")
        collector._read_cgroup_memory = Mock()
        collector._read_cgroup_memory.return_value = Mock(
            total_bytes=0,
            peak_bytes=0,
            anon_bytes=0,
            file_bytes=0,
            kernel_bytes=0,
            shmem_bytes=0,
            pagetables_bytes=0,
        )
        collector._aggregate_gpu_percent = Mock(return_value=None)
        collector._join_command = Mock(return_value="")
        collector._format_ports = Mock(return_value="")
        collector._state_uptime = Mock(return_value="")
        collector._shorten_docker_status = DashboardCollector._shorten_docker_status.__get__(collector, DashboardCollector)
        collector._read_container_network_rates = Mock(return_value=(128.0, 64.0))

        container = Mock()
        container.id = "abcdef1234567890"
        container.name = "trainer"
        container.status = "running"
        container.attrs = {
            "State": {"Status": "running", "Pid": 1234},
            "Config": {},
            "HostConfig": {},
            "NetworkSettings": {"Ports": {}},
        }
        collector._docker.containers.list.return_value = [container]

        containers = collector._read_containers({}, include_stopped=False, now=100.0)

        self.assertIn("abcdef1234567890", containers)
        self.assertEqual(containers["abcdef1234567890"].net_recv_rate, 128.0)
        self.assertEqual(containers["abcdef1234567890"].net_send_rate, 64.0)


class AppActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_kill_selected_uses_confirm_callback_without_push_screen_wait(self):
        app = DgxTopApp()
        process = ProcessInfo(
            pid=4321,
            ppid=1,
            name="python",
            command="python train.py",
            username="djmad",
            cpu_percent=0.0,
            rss_bytes=1024,
        )
        app.current_entity = Mock(return_value=process)
        app.push_screen_wait = Mock(side_effect=AssertionError("push_screen_wait should not be used"))
        app.collector = Mock()
        app.collector.terminate_process.return_value = "Sent SIGTERM to pid 4321"
        app.refresh_dashboard = AsyncMock()
        app.notify = Mock()

        pushed: dict[str, object] = {}

        def fake_push_screen(screen, callback=None, **kwargs):
            pushed["screen"] = screen
            pushed["callback"] = callback
            pushed["kwargs"] = kwargs
            return None

        app.push_screen = fake_push_screen

        app.action_kill_selected()

        self.assertIsInstance(pushed["screen"], ConfirmActionScreen)
        self.assertIn("callback", pushed)
        await pushed["callback"](True)
        app.collector.terminate_process.assert_called_once_with(4321)
        app.refresh_dashboard.assert_awaited_once()
        app.notify.assert_called_with("Sent SIGTERM to pid 4321")

    async def test_restart_selected_uses_confirm_callback_without_push_screen_wait(self):
        app = DgxTopApp()
        container = ContainerInfo(
            container_id="abcdef1234567890",
            name="trainer",
            image="trainer:latest",
            status="running",
            main_pid=1234,
        )
        app.current_entity = Mock(return_value=container)
        app.push_screen_wait = Mock(side_effect=AssertionError("push_screen_wait should not be used"))
        app.collector = Mock()
        app.collector.restart_container.return_value = "Restarted trainer"
        app.refresh_dashboard = AsyncMock()
        app.notify = Mock()

        pushed: dict[str, object] = {}

        def fake_push_screen(screen, callback=None, **kwargs):
            pushed["screen"] = screen
            pushed["callback"] = callback
            pushed["kwargs"] = kwargs
            return None

        app.push_screen = fake_push_screen

        app.action_restart_selected()

        self.assertIsInstance(pushed["screen"], ConfirmActionScreen)
        self.assertIn("callback", pushed)
        await pushed["callback"](True)
        app.collector.restart_container.assert_called_once_with("abcdef1234567890")
        app.refresh_dashboard.assert_awaited_once()
        app.notify.assert_called_with("Restarted trainer")


if __name__ == "__main__":
    unittest.main()
