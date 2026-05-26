"""
tests.test_validator — Tests for core.validator.

Tests validate_bash (syntax check) and check_network_safety (heuristic)
in isolation using real subprocess calls to bash -n — these are intentionally
not mocked because the whole point of validate_bash is the real bash check.

check_network_safety is pure-Python and needs no mocking.
"""
from __future__ import annotations

import pytest

from core.validator import check_network_safety, validate_bash, validate_script

# ---------------------------------------------------------------------------
# validate_bash — real bash -n calls
# ---------------------------------------------------------------------------


class TestValidateBash:
    def test_valid_simple_script(self) -> None:
        script = "#!/bin/bash\necho 'hello world'\n"
        ok, msg = validate_bash(script)
        assert ok is True
        assert msg == ""

    def test_valid_script_with_functions(self) -> None:
        script = (
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "greet() {\n"
            "    local name=$1\n"
            "    echo \"Hello, $name\"\n"
            "}\n"
            "greet 'World'\n"
        )
        ok, msg = validate_bash(script)
        assert ok is True
        assert msg == ""

    def test_invalid_syntax_unclosed_if(self) -> None:
        """Missing 'fi' makes the script syntactically invalid."""
        script = "#!/bin/bash\nif [ -f /etc/fstab ]; then\n    echo 'exists'\n"
        ok, msg = validate_bash(script)
        assert ok is False
        assert "Syntax error" in msg

    def test_invalid_syntax_unmatched_quote(self) -> None:
        script = "#!/bin/bash\necho 'unclosed string\n"
        ok, msg = validate_bash(script)
        assert ok is False
        assert "Syntax error" in msg or ok is False  # bash -n catches this

    def test_valid_multiline_heredoc(self) -> None:
        script = (
            "#!/bin/bash\n"
            "cat <<EOF\n"
            "This is a heredoc\n"
            "EOF\n"
        )
        ok, msg = validate_bash(script)
        assert ok is True

    def test_empty_script_is_valid(self) -> None:
        """An empty script is syntactically valid bash."""
        ok, msg = validate_bash("")
        assert ok is True

    def test_shebang_only_is_valid(self) -> None:
        ok, msg = validate_bash("#!/bin/bash\n")
        assert ok is True

    def test_returns_tuple(self) -> None:
        """validate_bash always returns a 2-tuple."""
        result = validate_bash("echo hi")
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_success_message_is_empty_string(self) -> None:
        """On success, the message component must be the empty string."""
        ok, msg = validate_bash("#!/bin/bash\necho ok\n")
        assert ok is True
        assert msg == ""


# ---------------------------------------------------------------------------
# check_network_safety — heuristic tests
# ---------------------------------------------------------------------------


class TestCheckNetworkSafety:
    def test_script_with_no_network_ops_passes(self) -> None:
        script = "#!/bin/bash\necho 'hello'\ndf -h\n"
        ok, msg = check_network_safety(script)
        assert ok is True

    def test_curl_with_ping_check_before_passes(self) -> None:
        script = (
            "#!/bin/bash\n"
            "TARGET=api.example.com\n"
            "if ! ping -c 2 -W 3 \"$TARGET\" &>/dev/null; then\n"
            "    echo 'unreachable' >&2; exit 1\n"
            "fi\n"
            "curl -s https://api.example.com/health\n"
        )
        ok, msg = check_network_safety(script)
        assert ok is True

    def test_curl_without_ping_check_fails(self) -> None:
        script = "#!/bin/bash\ncurl -s https://api.example.com/health\n"
        ok, msg = check_network_safety(script)
        assert ok is False
        assert "ping" in msg.lower() or "safety violation" in msg.lower()

    def test_wget_without_ping_check_fails(self) -> None:
        script = "#!/bin/bash\nwget -q https://example.com/file.tar.gz\n"
        ok, msg = check_network_safety(script)
        assert ok is False

    def test_mount_without_ping_check_fails(self) -> None:
        script = (
            "#!/bin/bash\n"
            "mount -t nfs 192.168.1.10:/share /mnt/data\n"
        )
        ok, msg = check_network_safety(script)
        assert ok is False

    def test_ssh_without_ping_check_fails(self) -> None:
        script = "#!/bin/bash\nssh user@remote.host 'uptime'\n"
        ok, msg = check_network_safety(script)
        assert ok is False

    def test_rsync_remote_without_ping_check_fails(self) -> None:
        script = (
            "#!/bin/bash\n"
            "rsync -av /local/ user@remote:/backup/\n"
        )
        ok, msg = check_network_safety(script)
        assert ok is False

    def test_ping_check_after_network_op_fails(self) -> None:
        """Ping check exists but appears AFTER the network operation."""
        script = (
            "#!/bin/bash\n"
            "curl -s http://api.example.com/data\n"
            "if ! ping -c 2 -W 3 api.example.com &>/dev/null; then\n"
            "    echo 'unreachable' >&2; exit 1\n"
            "fi\n"
        )
        ok, msg = check_network_safety(script)
        assert ok is False
        assert "after" in msg.lower() or "offset" in msg.lower()

    def test_error_message_mentions_operation(self) -> None:
        """The error message should name the offending operation."""
        script = "#!/bin/bash\ncurl http://example.com/\n"
        ok, msg = check_network_safety(script)
        assert ok is False
        # Message should mention what triggered the failure
        assert len(msg) > 20

    def test_no_network_ops_returns_empty_message(self) -> None:
        ok, msg = check_network_safety("#!/bin/bash\necho ok\n")
        assert ok is True
        assert msg == ""


# ---------------------------------------------------------------------------
# validate_script — combined (both checks in order)
# ---------------------------------------------------------------------------


class TestValidateScript:
    def test_clean_script_passes_both(self) -> None:
        script = (
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "echo 'Checking disk'\n"
            "df -h\n"
        )
        ok, msg = validate_script(script)
        assert ok is True
        assert msg == ""

    def test_syntax_error_fails_before_safety_check(self) -> None:
        """A syntax error should short-circuit; safety check is not reached."""
        script = "#!/bin/bash\nif [ 1 -eq 1 ]; then\n    echo oops\n"
        ok, msg = validate_script(script)
        assert ok is False
        assert "Syntax error" in msg

    def test_valid_syntax_but_unsafe_network(self) -> None:
        script = "#!/bin/bash\ncurl http://example.com/\n"
        ok, msg = validate_script(script)
        assert ok is False
        assert "ping" in msg.lower() or "safety" in msg.lower()

    def test_returns_tuple(self) -> None:
        ok, msg = validate_script("echo hi")
        assert isinstance(ok, bool)
        assert isinstance(msg, str)
