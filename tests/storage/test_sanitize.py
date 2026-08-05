"""Tests for storage/sanitize.py — secret scrubbing for memory stores."""

from __future__ import annotations

import pytest

from mcp_server.storage import sanitize


class TestPathsSurviveScrubbing:
    """4.0 Step 3.1 — the long-b64 rule allowed `/` unconditionally, so any
    absolute path over 40 chars was redacted as if it were a secret.

    Measured on a real store before the fix: 596 of 655 working-memory edit
    observations (91%) had their path destroyed, e.g.
      "Edit: touched /<redacted:long-b64>-mcp/mcp_server/graph/template.html"
    That silently gutted the capture layer the anchor work depends on.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/Users/sachin/Documents/Projects/LogisticsOS/agent-mcp/mcp_server/graph/template.html",
            "/Users/someone/Documents/Projects/Agentic/LH/apps/website/app/page.tsx",
            "/private/tmp/claude-502/-Users-x-Documents-Projects-LogisticsOS-agent-mcp/s.py",
            "src/very/deeply/nested/module/path/that/exceeds/forty/characters/file.py",
        ],
    )
    def test_absolute_and_long_relative_paths_are_not_redacted(self, path):
        out = sanitize.scrub_sensitive(f"Edit: touched {path}")
        assert "<redacted" not in out, f"path destroyed: {out}"
        assert path in out

    @pytest.mark.parametrize(
        "secret",
        [
            "dGhpcyBpcyBhIHZlcnkgbG9uZyBiYXNlNjQgc3RyaW5nIGhlcmU+Pj4+",
            "aGVsbG8gd29ybGQgdGhpcyBpcyBhIHRlc3Q/dmFsdWU+K2Zvb2Jhcg==",
            "AbCdEf0123456789AbCdEf0123456789AbCdEf0123456789",
        ],
    )
    def test_long_opaque_tokens_are_still_caught(self, secret):
        assert "<redacted" in sanitize.scrub_sensitive(f"token={secret}")

    def test_slashed_base64_with_padding_is_still_caught(self):
        """The slash case is narrowed, not removed: `+`/`=` still trips it."""
        blob = "ab/cd+ef/gh0123456789ABCDEFabcdef0123456789XYZ=="
        assert "<redacted" in sanitize.scrub_sensitive(f"sig {blob}")

    def test_a_slashed_secret_is_redacted_WHOLE_not_shredded(self):
        """Asserting `"<redacted" in out` is not enough — a PARTIAL redaction
        satisfies it while printing the rest of the key.

        The narrowing shipped as two regexes: a no-slash one first, then a
        slash-aware one gated on `+`/`=`. The first fired on the pre-slash
        half, breaking the run so the second could no longer match it whole,
        and the tail went out in the clear. This asserts on the SECRET being
        absent rather than on a marker being present.
        """
        head, tail = "Q" * 45, "/Zm9vYmFy+secretTAIL1234"
        out = sanitize.scrub_sensitive(f"key={head}{tail}")
        assert "secretTAIL1234" not in out, f"tail survived scrubbing: {out}"
        assert "Zm9vYmFy" not in out, f"tail survived scrubbing: {out}"
        assert head not in out

    def test_every_other_secret_class_is_unaffected(self):
        for text in (
            "api_key: sk-abc123",
            "Bearer eyJhbGciOiJIUzI1NiJ9",
            "AKIAIOSFODNN7EXAMPLE",
            "password: hunter2",
        ):
            assert "<redacted" in sanitize.scrub_sensitive(text)
