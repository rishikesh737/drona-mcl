"""
tests.test_tools — Unit tests for tool functions.

All subprocess calls and filesystem writes are mocked so these tests run
without a live system, Ollama, or any side effects.

Tested modules:
  - tools.system   (get_journal_logs, get_service_status, list_failed_services,
                    get_disk_usage, get_memory_usage)
  - tools.network  (list_network_sockets, ping_host, curl_health_check)
  - tools.scripts  (create_script, rollback_script, list_scripts)

execute_script is not unit-tested here because it requires interactive stdin;
it is covered by integration testing with a live terminal.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_completed_process(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess:
    """Build a mock CompletedProcess."""
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.stdout = stdout
    cp.stderr = stderr
    cp.returncode = returncode
    return cp


# ---------------------------------------------------------------------------
# tools.system
# ---------------------------------------------------------------------------


class TestSystemTools:
    @patch("tools.system.subprocess.run")
    def test_get_journal_logs_default(self, mock_run: MagicMock) -> None:
        from tools.system import get_journal_logs

        mock_run.return_value = _make_completed_process(stdout="May 24 12:00 kernel: ok")
        result = get_journal_logs()

        assert "Journal logs" in result
        assert "May 24 12:00 kernel: ok" in result
        # Verify list-form call (no shell=True)
        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert isinstance(cmd, list)
        assert "journalctl" in cmd
        assert kwargs.get("shell") is not True

    @patch("tools.system.subprocess.run")
    def test_get_journal_logs_with_unit(self, mock_run: MagicMock) -> None:
        from tools.system import get_journal_logs

        mock_run.return_value = _make_completed_process(stdout="nginx started")
        result = get_journal_logs(unit="nginx.service", lines=10)

        assert "nginx.service" in result
        cmd = mock_run.call_args[0][0]
        assert "-u" in cmd
        assert "nginx.service" in cmd

    @patch("tools.system.subprocess.run")
    def test_get_journal_logs_clamps_lines(self, mock_run: MagicMock) -> None:
        from tools.system import get_journal_logs

        mock_run.return_value = _make_completed_process(stdout="log")
        get_journal_logs(lines=9999)
        cmd = mock_run.call_args[0][0]
        # Should be clamped to 500
        n_idx = cmd.index("-n")
        assert int(cmd[n_idx + 1]) == 500

    @patch("tools.system.subprocess.run")
    def test_get_service_status(self, mock_run: MagicMock) -> None:
        from tools.system import get_service_status

        mock_run.return_value = _make_completed_process(stdout="● sshd.service - Active: active")
        result = get_service_status("sshd.service")

        assert "sshd.service" in result
        assert "Active" in result

    def test_get_service_status_empty_unit(self) -> None:
        from tools.system import get_service_status

        result = get_service_status("")
        assert "[ERROR]" in result
        assert "required" in result.lower()

    @patch("tools.system.subprocess.run")
    def test_list_failed_services_with_failures(self, mock_run: MagicMock) -> None:
        from tools.system import list_failed_services

        mock_run.return_value = _make_completed_process(
            stdout="  myapp.service  failed  failed  My App"
        )
        result = list_failed_services()
        assert "myapp.service" in result

    @patch("tools.system.subprocess.run")
    def test_list_failed_services_none(self, mock_run: MagicMock) -> None:
        from tools.system import list_failed_services

        mock_run.return_value = _make_completed_process(stdout="")
        result = list_failed_services()
        assert "No failed" in result

    @patch("tools.system.subprocess.run")
    def test_get_disk_usage_all(self, mock_run: MagicMock) -> None:
        from tools.system import get_disk_usage

        mock_run.return_value = _make_completed_process(
            stdout="Filesystem  Size  Used  Avail  Use%  Mounted on\n/dev/sda1  100G  50G  50G  50%  /"
        )
        result = get_disk_usage()
        assert "Disk usage" in result
        assert "/dev/sda1" in result

    @patch("tools.system.subprocess.run")
    def test_get_disk_usage_with_path(self, mock_run: MagicMock) -> None:
        from tools.system import get_disk_usage

        mock_run.return_value = _make_completed_process(stdout="/dev/sda2 ...")
        result = get_disk_usage("/var")
        assert "/var" in result
        cmd = mock_run.call_args[0][0]
        assert "/var" in cmd

    @patch("tools.system.subprocess.run")
    def test_get_memory_usage(self, mock_run: MagicMock) -> None:
        from tools.system import get_memory_usage

        mock_run.return_value = _make_completed_process(
            stdout="              total  used  free\nMem:          16G    8G    8G"
        )
        result = get_memory_usage()
        assert "Memory usage" in result
        assert "16G" in result

    @patch("tools.system.subprocess.run")
    def test_subprocess_timeout_returns_error(self, mock_run: MagicMock) -> None:
        from tools.system import get_disk_usage

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="df", timeout=30)
        result = get_disk_usage()
        assert "[ERROR]" in result
        assert "timed out" in result.lower()

    @patch("tools.system.subprocess.run")
    def test_command_not_found_returns_error(self, mock_run: MagicMock) -> None:
        from tools.system import get_memory_usage

        mock_run.side_effect = FileNotFoundError()
        result = get_memory_usage()
        assert "[ERROR]" in result
        assert "not found" in result.lower()


# ---------------------------------------------------------------------------
# tools.network
# ---------------------------------------------------------------------------


class TestNetworkTools:
    @patch("tools.network.subprocess.run")
    def test_list_network_sockets_success(self, mock_run: MagicMock) -> None:
        from tools.network import list_network_sockets

        mock_run.return_value = _make_completed_process(
            stdout="tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(('sshd',pid=1234))"
        )
        result = list_network_sockets()
        assert "sshd" in result
        assert "[ERROR]" not in result

    @patch("tools.network.subprocess.run")
    def test_list_network_sockets_failure(self, mock_run: MagicMock) -> None:
        from tools.network import list_network_sockets

        mock_run.return_value = _make_completed_process(returncode=1, stderr="Permission denied")
        result = list_network_sockets()
        assert "[ERROR]" in result

    @patch("tools.network.subprocess.run")
    def test_ping_host_reachable(self, mock_run: MagicMock) -> None:
        from tools.network import ping_host

        mock_run.return_value = _make_completed_process(
            stdout="PING 8.8.8.8: 3 packets transmitted, 3 received, rtt min/avg/max = 10/12/15 ms"
        )
        result = ping_host("8.8.8.8", count=3)
        assert "8.8.8.8" in result
        assert "[UNREACHABLE]" not in result

    @patch("tools.network.subprocess.run")
    def test_ping_host_unreachable(self, mock_run: MagicMock) -> None:
        from tools.network import ping_host

        mock_run.return_value = _make_completed_process(
            returncode=1,
            stdout="3 packets transmitted, 0 received, 100% packet loss",
        )
        result = ping_host("10.255.255.1")
        assert "[UNREACHABLE]" in result

    def test_ping_host_empty_host(self) -> None:
        from tools.network import ping_host

        result = ping_host("")
        assert "[ERROR]" in result
        assert "required" in result.lower()

    @patch("tools.network.subprocess.run")
    def test_ping_host_clamps_count(self, mock_run: MagicMock) -> None:
        from tools.network import ping_host

        mock_run.return_value = _make_completed_process(stdout="ok")
        ping_host("localhost", count=999)
        cmd = mock_run.call_args[0][0]
        c_idx = cmd.index("-c")
        assert int(cmd[c_idx + 1]) == 10

    @patch("tools.network.subprocess.run")
    def test_curl_health_check_success(self, mock_run: MagicMock) -> None:
        from tools.network import curl_health_check

        # First call returns status code, second returns body
        mock_run.side_effect = [
            _make_completed_process(stdout="200"),
            _make_completed_process(stdout='{"status": "ok"}'),
        ]
        result = curl_health_check("http://localhost:8080/health")
        assert "200" in result
        assert "ok" in result

    @patch("tools.network.subprocess.run")
    def test_curl_health_check_failure(self, mock_run: MagicMock) -> None:
        from tools.network import curl_health_check

        mock_run.return_value = _make_completed_process(
            returncode=7, stderr="Failed to connect"
        )
        result = curl_health_check("http://unreachable.invalid/")
        assert "[ERROR]" in result

    def test_curl_health_check_empty_url(self) -> None:
        from tools.network import curl_health_check

        result = curl_health_check("")
        assert "[ERROR]" in result
        assert "required" in result.lower()

    @patch("tools.network.subprocess.run")
    def test_curl_response_body_truncated(self, mock_run: MagicMock) -> None:
        from tools.network import curl_health_check

        large_body = "x" * 10_000
        mock_run.side_effect = [
            _make_completed_process(stdout="200"),
            _make_completed_process(stdout=large_body),
        ]
        result = curl_health_check("http://example.com/")
        assert "truncated" in result.lower()


# ---------------------------------------------------------------------------
# tools.scripts (filesystem-touching, patched)
# ---------------------------------------------------------------------------


class TestScriptTools:
    def test_create_script_rejects_path_traversal(self, tmp_path: Path) -> None:
        """Filenames with path separators are rejected before any disk write."""
        from tools.scripts import create_script

        # Patch workspace to tmp_path so nothing touches the real ai_workspace
        with patch("tools.scripts._WORKSPACE", tmp_path):
            result = create_script(
                filename="../escape.sh",
                content="#!/bin/bash\necho hi",
                description="test",
            )
        assert "[ERROR]" in result
        assert "path separator" in result.lower()

    def test_create_script_validation_failure_blocks_write(
        self, tmp_path: Path
    ) -> None:
        """A syntactically invalid script is rejected before disk write."""
        from tools.scripts import create_script

        bad_script = "#!/bin/bash\nif [ 1 -eq 1 ]; then\n    echo oops\n"
        with patch("tools.scripts._WORKSPACE", tmp_path):
            result = create_script(
                filename="bad.sh",
                content=bad_script,
                description="invalid script",
            )
        assert "[ERROR]" in result
        assert "validation failed" in result.lower()
        # File must NOT have been written
        assert not (tmp_path / "bad.sh").exists()

    def test_create_script_success_writes_file(self, tmp_path: Path) -> None:
        """A valid script is written to the workspace."""
        from tools.scripts import create_script

        good_script = "#!/bin/bash\nset -euo pipefail\necho 'hello'\n"
        with patch("tools.scripts._WORKSPACE", tmp_path):
            result = create_script(
                filename="hello.sh",
                content=good_script,
                description="say hello",
            )
        assert "[OK]" in result
        written = tmp_path / "hello.sh"
        assert written.exists()
        assert written.read_text() == good_script

    def test_create_script_creates_backup_of_existing(
        self, tmp_path: Path
    ) -> None:
        """Overwriting an existing script creates a .bak file first."""
        from tools.scripts import create_script

        good_script = "#!/bin/bash\necho 'v1'\n"
        updated_script = "#!/bin/bash\necho 'v2'\n"
        script_path = tmp_path / "hello.sh"
        script_path.write_text(good_script)

        with patch("tools.scripts._WORKSPACE", tmp_path):
            create_script("hello.sh", updated_script, "updated")

        bak_path = tmp_path / "hello.sh.bak"
        assert bak_path.exists()
        assert bak_path.read_text() == good_script
        assert script_path.read_text() == updated_script

    def test_rollback_restores_from_backup(self, tmp_path: Path) -> None:
        """rollback_script restores the .bak file."""
        from tools.scripts import rollback_script

        original = "#!/bin/bash\necho 'original'\n"
        current = "#!/bin/bash\necho 'modified'\n"

        script_path = tmp_path / "hello.sh"
        bak_path = tmp_path / "hello.sh.bak"
        script_path.write_text(current)
        bak_path.write_text(original)

        with patch("tools.scripts._WORKSPACE", tmp_path):
            result = rollback_script("hello.sh")

        assert "[OK]" in result
        assert script_path.read_text() == original

    def test_rollback_fails_without_backup(self, tmp_path: Path) -> None:
        """rollback_script returns an error if no .bak exists."""
        from tools.scripts import rollback_script

        (tmp_path / "hello.sh").write_text("#!/bin/bash\necho hi\n")
        with patch("tools.scripts._WORKSPACE", tmp_path):
            result = rollback_script("hello.sh")
        assert "[ERROR]" in result
        assert "backup" in result.lower()

    def test_list_scripts_empty_workspace(self, tmp_path: Path) -> None:
        from tools.scripts import list_scripts

        with patch("tools.scripts._WORKSPACE", tmp_path):
            result = list_scripts()
        assert "no scripts found" in result.lower()

    def test_list_scripts_shows_files(self, tmp_path: Path) -> None:
        from tools.scripts import list_scripts

        (tmp_path / "fix_nginx.sh").write_text("#!/bin/bash\necho hi\n")
        (tmp_path / "check_disk.sh").write_text("#!/bin/bash\ndf -h\n")
        with patch("tools.scripts._WORKSPACE", tmp_path):
            result = list_scripts()
        assert "fix_nginx.sh" in result
        assert "check_disk.sh" in result

    def test_create_script_adds_sh_extension(self, tmp_path: Path) -> None:
        """Filenames without .sh get the extension appended."""
        from tools.scripts import create_script

        good_script = "#!/bin/bash\necho hi\n"
        with patch("tools.scripts._WORKSPACE", tmp_path):
            result = create_script("no_ext", good_script, "test")
        assert "[OK]" in result
        assert (tmp_path / "no_ext.sh").exists()
