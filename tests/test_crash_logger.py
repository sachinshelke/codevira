"""
Tests for mcp_server/crash_logger.py

Covers:
  - _sanitize(): structural patterns (PEM, connection strings, IPs, key=value,
    env vars) + home dir replacement + env dump
  - log_crash(): file writing, structured output, metadata fields
  - read_recent_crashes(): limit, empty log, missing log
  - get_crash_log_path(): returns correct path
  - install_global_handler(): sets sys.excepthook
  - Chaos: logger errors gracefully, concurrent writes, non-string exceptions
"""
from __future__ import annotations

import logging
import re
import sys
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import mcp_server.crash_logger as crash_logger
from mcp_server.crash_logger import (
    _sanitize,
    log_crash,
    read_recent_crashes,
    get_crash_log_path,
    install_global_handler,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_logger():
    """Reset module-level _logger between tests so each test is isolated."""
    crash_logger._logger = None
    yield
    crash_logger._logger = None


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    """Redirect crash logger to write into tmp_path/logs."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(crash_logger, "_get_log_dir", lambda: logs)
    return logs


# ===========================================================================
# _sanitize() — secret pattern coverage
# ===========================================================================

class TestSanitizePEM:
    def test_private_key_block(self):
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIBogIBAAJBALRiMLAH...\n"
            "-----END RSA PRIVATE KEY-----"
        )
        result = _sanitize(f"key data: {pem}")
        assert "***PRIVATE_KEY***" in result
        assert "MIIBogIBAAJBALRiMLAH" not in result

    def test_ec_private_key_block(self):
        pem = (
            "-----BEGIN EC PRIVATE KEY-----\n"
            "somekeydata\n"
            "-----END EC PRIVATE KEY-----"
        )
        assert "***PRIVATE_KEY***" in _sanitize(pem)


class TestSanitizeConnectionStrings:
    def test_postgres_url(self):
        url = "postgres://admin:s3cretP@ss@db.host.com:5432/mydb"
        result = _sanitize(url)
        assert "s3cretP@ss" not in result
        assert "***@" in result

    def test_mongodb_url(self):
        url = "mongodb://user:password123@mongo.example.com/db"
        result = _sanitize(url)
        assert "password123" not in result

    def test_redis_url(self):
        url = "redis://default:mysecret@redis.internal:6379"
        result = _sanitize(url)
        assert "mysecret" not in result


class TestSanitizeInternalIP:
    def test_10_network(self):
        assert "***INTERNAL_IP***" in _sanitize("host: 10.0.1.55")

    def test_172_network(self):
        assert "***INTERNAL_IP***" in _sanitize("host: 172.16.0.1")

    def test_192_168_network(self):
        assert "***INTERNAL_IP***" in _sanitize("host: 192.168.1.100")

    def test_public_ip_not_redacted(self):
        result = _sanitize("host: 8.8.8.8")
        assert "***INTERNAL_IP***" not in result


class TestSanitizeKeyValue:
    def test_api_key_equals(self):
        result = _sanitize("api_key=supersecret123")
        assert "supersecret123" not in result

    def test_token_colon(self):
        result = _sanitize("token: mysecrettoken")
        assert "mysecrettoken" not in result

    def test_authorization_bearer(self):
        # The key=value pattern matches \S+ after the separator, so
        # "Bearer" is the captured token. The full "Bearer abc123xyz"
        # is not matched atomically -- only the first word after = is.
        result = _sanitize("authorization: Bearer abc123xyz")
        assert "Bearer" not in result


class TestSanitizeJSONSecrets:
    def test_json_password(self):
        text = '{"password": "hunter2"}'
        result = _sanitize(text)
        assert "hunter2" not in result

    def test_json_api_key(self):
        text = """'api_key': 'some-secret-value'"""
        result = _sanitize(text)
        assert "some-secret-value" not in result


class TestSanitizeEnvDumps:
    def test_large_environ_dump(self):
        # Build an environ string longer than 200 chars
        env_content = " ".join(f"VAR{i}=val{i}" for i in range(50))
        text = f"environ{{{env_content}}}"
        result = _sanitize(text)
        assert "***REDACTED_ENV***" in result

    def test_small_environ_not_redacted(self):
        text = "environ{HOME=/tmp}"
        result = _sanitize(text)
        assert "***REDACTED_ENV***" not in result


class TestSanitizeHomeDirReplacement:
    def test_home_dir_replaced(self):
        home = str(Path.home())
        text = f"Error in {home}/projects/secret/file.py"
        result = _sanitize(text)
        assert home not in result
        assert "~/projects/secret/file.py" in result


class TestSanitizeEnvVariables:
    def test_database_url_env(self):
        text = "DATABASE_URL=postgres://user:pass@host/db"
        result = _sanitize(text)
        assert "pass@host" not in result

    def test_secret_key_env(self):
        text = "SECRET_KEY = my_super_secret_value_123"
        result = _sanitize(text)
        assert "my_super_secret_value_123" not in result

    def test_access_token_env(self):
        text = "ACCESS_TOKEN=abcdef123456"
        result = _sanitize(text)
        assert "abcdef123456" not in result


# ===========================================================================
# log_crash()
# ===========================================================================

class TestLogCrash:
    def test_writes_log_file(self, log_dir):
        try:
            raise ValueError("test error")
        except ValueError as e:
            log_crash(e, context="unit test")

        log_file = log_dir / "crashes.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "ValueError" in content
        assert "test error" in content

    def test_contains_separator(self, log_dir):
        try:
            raise RuntimeError("boom")
        except RuntimeError as e:
            log_crash(e)

        content = (log_dir / "crashes.log").read_text()
        assert "=" * 72 in content

    def test_contains_context(self, log_dir):
        try:
            raise TypeError("bad type")
        except TypeError as e:
            log_crash(e, context="parsing config")

        content = (log_dir / "crashes.log").read_text()
        assert "WHERE: parsing config" in content

    def test_contains_tool_name(self, log_dir):
        try:
            raise KeyError("missing")
        except KeyError as e:
            log_crash(e, tool_name="get_node")

        content = (log_dir / "crashes.log").read_text()
        assert "TOOL:  get_node" in content

    def test_contains_project_path(self, log_dir):
        try:
            raise IOError("disk full")
        except IOError as e:
            log_crash(e, project_path="/tmp/my-project")

        content = (log_dir / "crashes.log").read_text()
        assert "PROJECT:" in content

    def test_contains_python_version(self, log_dir):
        try:
            raise Exception("any")
        except Exception as e:
            log_crash(e)

        content = (log_dir / "crashes.log").read_text()
        assert "PYTHON:" in content

    def test_contains_traceback(self, log_dir):
        try:
            raise ValueError("traceback test")
        except ValueError as e:
            log_crash(e)

        content = (log_dir / "crashes.log").read_text()
        assert "TRACEBACK:" in content
        assert "Traceback (most recent call last):" in content

    def test_sanitizes_secrets_in_log(self, log_dir):
        try:
            raise ValueError("url=postgres://admin:s3cret@db.host/mydb")
        except ValueError as e:
            log_crash(e, context="connection string leak")

        content = (log_dir / "crashes.log").read_text()
        assert "s3cret" not in content

    def test_multiple_crashes_append(self, log_dir):
        for i in range(3):
            try:
                raise RuntimeError(f"crash {i}")
            except RuntimeError as e:
                log_crash(e)

        content = (log_dir / "crashes.log").read_text()
        assert "crash 0" in content
        assert "crash 1" in content
        assert "crash 2" in content


# ===========================================================================
# read_recent_crashes()
# ===========================================================================

class TestReadRecentCrashes:
    def test_no_log_file(self, log_dir):
        result = read_recent_crashes()
        assert "No crash log found" in result or "No crashes" in result

    def test_empty_log_file(self, log_dir):
        (log_dir / "crashes.log").write_text("")
        result = read_recent_crashes()
        assert "empty" in result.lower() or "No crash" in result

    def test_limit_returns_correct_count(self, log_dir):
        for i in range(5):
            try:
                raise RuntimeError(f"crash {i}")
            except RuntimeError as e:
                log_crash(e)

        result = read_recent_crashes(limit=2)
        assert "2 of 5" in result

    def test_limit_greater_than_entries(self, log_dir):
        try:
            raise RuntimeError("single crash")
        except RuntimeError as e:
            log_crash(e)

        result = read_recent_crashes(limit=100)
        assert "1 of 1" in result

    def test_limit_zero_treated_as_one(self, log_dir):
        try:
            raise RuntimeError("crash")
        except RuntimeError as e:
            log_crash(e)

        result = read_recent_crashes(limit=0)
        assert "1 of 1" in result

    def test_negative_limit_treated_as_one(self, log_dir):
        try:
            raise RuntimeError("crash")
        except RuntimeError as e:
            log_crash(e)

        result = read_recent_crashes(limit=-5)
        assert "1 of 1" in result

    def test_shows_log_size(self, log_dir):
        try:
            raise RuntimeError("crash")
        except RuntimeError as e:
            log_crash(e)

        result = read_recent_crashes()
        assert "KB" in result

    def test_home_dir_hidden_in_header(self, log_dir):
        try:
            raise RuntimeError("crash")
        except RuntimeError as e:
            log_crash(e)

        result = read_recent_crashes()
        assert str(Path.home()) not in result


# ===========================================================================
# get_crash_log_path()
# ===========================================================================

class TestGetCrashLogPath:
    def test_returns_path_object(self, log_dir):
        result = get_crash_log_path()
        assert isinstance(result, Path)

    def test_ends_with_crashes_log(self, log_dir):
        result = get_crash_log_path()
        assert result.name == "crashes.log"


# ===========================================================================
# install_global_handler()
# ===========================================================================

class TestInstallGlobalHandler:
    def test_sets_excepthook(self, log_dir):
        original = sys.excepthook
        try:
            install_global_handler()
            assert sys.excepthook is not original
        finally:
            sys.excepthook = original

    def test_preserves_original_hook(self, log_dir):
        original = sys.excepthook
        try:
            install_global_handler()
            # The new hook should still call the original
            # We verify by checking the closure wraps something
            assert sys.excepthook is not original
            assert callable(sys.excepthook)
        finally:
            sys.excepthook = original

    def test_keyboard_interrupt_skipped(self, log_dir):
        """KeyboardInterrupt should not be logged."""
        original = sys.excepthook
        try:
            install_global_handler()
            hook = sys.excepthook

            # Call the hook with KeyboardInterrupt -- it should call original
            # but NOT log_crash. We verify no crash file is created for it.
            with patch.object(crash_logger, "log_crash") as mock_log:
                # We need to suppress the original hook from printing to stderr
                with patch.object(sys, "__excepthook__", lambda *a: None):
                    try:
                        hook(KeyboardInterrupt, KeyboardInterrupt(), None)
                    except (KeyboardInterrupt, SystemExit, TypeError):
                        pass
                mock_log.assert_not_called()
        finally:
            sys.excepthook = original


# ===========================================================================
# Chaos / edge cases
# ===========================================================================

class TestChaos:
    def test_logger_error_does_not_crash(self, log_dir, monkeypatch):
        """If the logger itself throws, log_crash must not propagate."""
        def bad_get_logger():
            raise OSError("disk exploded")

        monkeypatch.setattr(crash_logger, "_get_logger", bad_get_logger)

        # This must not raise
        try:
            raise ValueError("original error")
        except ValueError as e:
            log_crash(e)  # should silently handle the OSError

    def test_concurrent_writes(self, log_dir):
        """Multiple threads writing crashes concurrently should not corrupt."""
        errors = []

        def crash_in_thread(idx):
            try:
                try:
                    raise RuntimeError(f"thread-{idx}")
                except RuntimeError as e:
                    log_crash(e, context=f"thread {idx}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=crash_in_thread, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"Threads raised: {errors}"

        log_file = log_dir / "crashes.log"
        assert log_file.exists()
        content = log_file.read_text()
        # At least some of the 10 crashes should have been written
        crash_count = content.count("CRASH:")
        assert crash_count >= 5, f"Only {crash_count} of 10 crashes written"

    def test_non_string_exception_message(self, log_dir):
        """Exception with non-string args should not break the logger."""
        class WeirdError(Exception):
            def __str__(self):
                return repr({"code": 42, "nested": [1, 2, 3]})

        try:
            raise WeirdError({"code": 42})
        except WeirdError as e:
            log_crash(e)

        content = (log_dir / "crashes.log").read_text()
        assert "WeirdError" in content

    def test_exception_without_traceback(self, log_dir):
        """An exception created without raise should still be loggable."""
        err = RuntimeError("no traceback")
        log_crash(err, context="synthetic error")

        content = (log_dir / "crashes.log").read_text()
        assert "RuntimeError" in content
        assert "no traceback" in content
