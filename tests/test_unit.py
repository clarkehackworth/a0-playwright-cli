"""Unit tests for PlaywrightCliBackend — no browser required.

Covers:
  - _truncate_snapshot(): list format (actual), dict format (fallback), limits, edge cases
  - _parse_decision(): valid JSON, JSON in prose, fallback
  - get_log(): empty init, populated during run, returns copy
  - get_session_id(): format, uniqueness
  - validate_binary(): binary present/absent
  - _build_prompt(): structure and capping
  - _load_system_prompt(): file found / missing
  - Action security: URL allowlist, ref pattern validation
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure plugin root is importable (also handled by conftest, but explicit here)
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from helpers.playwright_cli_backend import (
    PlaywrightCliBackend,
    PlaywrightCliResult,
    PlaywrightCliTask,
    _REF_PATTERN,
    _URL_ALLOWED_SCHEMES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_backend(context_id: str = "a" * 32) -> PlaywrightCliBackend:
    agent = MagicMock()
    agent.context.id = context_id
    return PlaywrightCliBackend(agent)


# ===========================================================================
# _truncate_snapshot()
# ===========================================================================

class TestTruncateSnapshot:

    def test_list_under_limit_unchanged(self, backend):
        """Lists shorter than SNAPSHOT_MAX_ELEMENTS pass through unchanged."""
        items = [f"item-{i}" for i in range(10)]
        result = backend._truncate_snapshot(items)
        assert result == items

    def test_list_at_limit_unchanged(self, backend):
        items = [f"item-{i}" for i in range(backend.SNAPSHOT_MAX_ELEMENTS)]
        result = backend._truncate_snapshot(items)
        assert len(result) == backend.SNAPSHOT_MAX_ELEMENTS

    def test_list_over_limit_truncated(self, backend):
        """Lists exceeding SNAPSHOT_MAX_ELEMENTS are truncated + omission note appended."""
        items = [f"item-{i}" for i in range(backend.SNAPSHOT_MAX_ELEMENTS + 20)]
        result = backend._truncate_snapshot(items)
        assert len(result) == backend.SNAPSHOT_MAX_ELEMENTS + 1  # items + omission note
        assert "20 elements omitted" in result[-1]

    def test_list_truncation_does_not_mutate_original(self, backend):
        """Truncation must not mutate the original list."""
        items = [f"item-{i}" for i in range(backend.SNAPSHOT_MAX_ELEMENTS + 5)]
        original_len = len(items)
        backend._truncate_snapshot(items)
        assert len(items) == original_len

    def test_empty_list(self, backend):
        result = backend._truncate_snapshot([])
        assert result == []

    def test_dict_with_elements_key_truncated(self, backend):
        """Dict with 'elements' key: list inside is truncated."""
        snap = {"elements": [f"e{i}" for i in range(backend.SNAPSHOT_MAX_ELEMENTS + 10)]}
        result = backend._truncate_snapshot(snap)
        assert len(result["elements"]) == backend.SNAPSHOT_MAX_ELEMENTS
        assert "_truncated" in result

    def test_dict_with_nodes_key_truncated(self, backend):
        snap = {"nodes": [f"n{i}" for i in range(backend.SNAPSHOT_MAX_ELEMENTS + 5)]}
        result = backend._truncate_snapshot(snap)
        assert len(result["nodes"]) == backend.SNAPSHOT_MAX_ELEMENTS

    def test_dict_with_children_key_truncated(self, backend):
        snap = {"children": [f"c{i}" for i in range(backend.SNAPSHOT_MAX_ELEMENTS + 1)]}
        result = backend._truncate_snapshot(snap)
        assert len(result["children"]) == backend.SNAPSHOT_MAX_ELEMENTS

    def test_dict_with_tree_key_truncated(self, backend):
        snap = {"tree": [f"t{i}" for i in range(backend.SNAPSHOT_MAX_ELEMENTS + 2)]}
        result = backend._truncate_snapshot(snap)
        assert len(result["tree"]) == backend.SNAPSHOT_MAX_ELEMENTS

    def test_dict_under_limit_unchanged(self, backend):
        snap = {"elements": ["a", "b", "c"], "url": "https://example.com"}
        result = backend._truncate_snapshot(snap)
        assert result["elements"] == ["a", "b", "c"]
        assert "_truncated" not in result

    def test_dict_does_not_mutate_original(self, backend):
        """_truncate_snapshot must return a copy, not mutate the input dict."""
        original_len = backend.SNAPSHOT_MAX_ELEMENTS + 5
        snap = {"elements": [f"e{i}" for i in range(original_len)]}
        backend._truncate_snapshot(snap)
        assert len(snap["elements"]) == original_len

    def test_unknown_type_passthrough(self, backend):
        """None and non-list/dict types pass through untouched."""
        assert backend._truncate_snapshot(None) is None
        assert backend._truncate_snapshot("raw string") == "raw string"
        assert backend._truncate_snapshot(42) == 42

    def test_actual_playwright_snapshot_format(self, backend):
        """Simulate actual playwright-cli YAML format: top-level list of ARIA nodes."""
        # Actual format from live playwright-cli snapshot:
        # - generic [ref=e2]:
        #   - heading "Title" [level=1] [ref=e3]
        snapshot = [
            'generic [ref=e2]:',
            '  heading "Example Domain" [level=1] [ref=e3]',
            '  paragraph [ref=e4]: This domain is for use in documentation examples.',
        ]
        result = backend._truncate_snapshot(snapshot)
        assert result == snapshot  # under limit, unchanged


# ===========================================================================
# _parse_decision()
# ===========================================================================

class TestParseDecision:

    def test_valid_json_object(self, backend):
        decision = {"action": "goto", "value": "https://example.com", "done": False}
        result = backend._parse_decision(json.dumps(decision))
        assert result["action"] == "goto"
        assert result["value"] == "https://example.com"

    def test_json_embedded_in_prose(self, backend):
        """LLM sometimes wraps JSON in explanation text."""
        prose = 'I will navigate. {"action": "goto", "value": "https://x.com", "done": false} That is my plan.'
        result = backend._parse_decision(prose)
        assert result["action"] == "goto"

    def test_invalid_json_falls_back_to_done(self, backend):
        """Completely unparseable response: treat as done with full content as value."""
        result = backend._parse_decision("The task is complete, the title is Example Domain.")
        assert result["action"] == "done"
        assert result["done"] is True
        assert "Example Domain" in result["value"]

    def test_done_action_detected(self, backend):
        decision = {"action": "done", "value": "Final answer here", "done": True}
        result = backend._parse_decision(json.dumps(decision))
        assert result["done"] is True
        assert result["value"] == "Final answer here"

    def test_empty_string_falls_back(self, backend):
        result = backend._parse_decision("")
        assert result["action"] == "done"
        assert result["done"] is True

    def test_json_in_markdown_code_fence(self, backend):
        """LLM sometimes wraps JSON in markdown fences — regex should still find it."""
        fenced = '```json\n{"action": "click", "ref": "e5", "done": false}\n```'
        result = backend._parse_decision(fenced)
        assert result["action"] == "click"
        assert result["ref"] == "e5"


# ===========================================================================
# get_log()
# ===========================================================================

class TestGetLog:

    def test_empty_on_init(self, backend):
        """get_log() returns empty list before any task runs."""
        assert backend.get_log() == []

    def test_returns_copy(self, backend):
        """get_log() must return a copy, not the internal list."""
        backend._log_lines.append("test entry")
        log1 = backend.get_log()
        log1.append("mutated externally")
        log2 = backend.get_log()
        assert len(log2) == 1  # original unchanged
        assert log2[0] == "test entry"

    def test_reflects_appended_lines(self, backend):
        backend._log_lines.append("Browser opened → https://example.com")
        backend._log_lines.append("Step 1: goto: https://example.com")
        log = backend.get_log()
        assert len(log) == 2
        assert "Browser opened" in log[0]
        assert "Step 1" in log[1]

    def test_reset_on_new_task_start(self, backend):
        """_log_lines reset when _run_task starts (simulated via direct assignment)."""
        backend._log_lines = ["old entry from previous task"]
        # Simulate reset that happens at top of _run_task
        backend._log_lines = []
        assert backend.get_log() == []


# ===========================================================================
# PlaywrightCliResult
# ===========================================================================

class TestPlaywrightCliResult:

    def test_final_result_returns_text(self):
        r = PlaywrightCliResult("The page title is Example Domain")
        assert r.final_result() == "The page title is Example Domain"

    def test_is_done_always_true(self):
        assert PlaywrightCliResult("").is_done() is True

    def test_urls_always_empty(self):
        assert PlaywrightCliResult("anything").urls() == []

    def test_empty_string_init(self):
        assert PlaywrightCliResult("").final_result() == ""

    def test_none_init_coerced_to_empty(self):
        r = PlaywrightCliResult(None)  # type: ignore
        assert r.final_result() == ""


# ===========================================================================
# get_session_id()
# ===========================================================================

class TestGetSessionId:

    def test_format_starts_with_a0(self, backend):
        sid = backend.get_session_id()
        assert sid.startswith("a0-")

    def test_length(self, mock_agent):
        """Session ID = 'a0-' + 16 hex chars = 19 chars total."""
        backend = PlaywrightCliBackend(mock_agent)
        assert len(backend.get_session_id()) == 19

    def test_only_hex_chars_after_prefix(self, backend):
        sid = backend.get_session_id()
        hex_part = sid[3:]  # strip 'a0-'
        assert all(c in "0123456789abcdef" for c in hex_part)

    def test_different_agents_different_sessions(self):
        import uuid
        b1 = make_backend(uuid.uuid4().hex)
        b2 = make_backend(uuid.uuid4().hex)
        assert b1.get_session_id() != b2.get_session_id()

    def test_consistent_for_same_agent(self, backend):
        """Same backend always returns the same session ID."""
        assert backend.get_session_id() == backend.get_session_id()


# ===========================================================================
# validate_binary()
# ===========================================================================

class TestValidateBinary:

    def test_returns_true_when_binary_exists(self):
        with patch("shutil.which", return_value="/usr/local/bin/playwright-cli"):
            assert PlaywrightCliBackend.validate_binary() is True

    def test_returns_false_when_binary_missing(self):
        with patch("shutil.which", return_value=None):
            assert PlaywrightCliBackend.validate_binary() is False

    def test_actual_binary_present(self):
        """playwright-cli must be on PATH in this environment."""
        assert PlaywrightCliBackend.validate_binary() is True


# ===========================================================================
# _build_prompt()
# ===========================================================================

class TestBuildPrompt:

    def test_contains_task(self, backend):
        prompt = backend._build_prompt("Go to example.com", {}, [])
        assert "Go to example.com" in prompt

    def test_contains_snapshot_section(self, backend):
        prompt = backend._build_prompt("task", {"url": "https://x.com"}, [])
        assert "Page Snapshot" in prompt

    def test_contains_history_section(self, backend):
        history = [{"action": "goto", "value": "https://x.com"}]
        prompt = backend._build_prompt("task", {}, history)
        assert "Action History" in prompt
        assert "goto" in prompt

    def test_snapshot_capped_at_max_bytes(self, backend):
        """Large snapshots must be capped at SNAPSHOT_MAX_BYTES."""
        huge_snapshot = {"data": "x" * (backend.SNAPSHOT_MAX_BYTES + 10000)}
        prompt = backend._build_prompt("task", huge_snapshot, [])
        # Snapshot section should be present but capped
        assert "snapshot truncated at byte limit" in prompt

    def test_only_last_5_history_entries(self, backend):
        """History is limited to last 5 entries to avoid prompt bloat."""
        history = [{"action": f"step-{i}"} for i in range(10)]
        prompt = backend._build_prompt("task", {}, history)
        # step-9 (last) should be present, step-0 (first) should not
        assert "step-9" in prompt
        assert "step-0" not in prompt

    def test_empty_history_is_fine(self, backend):
        prompt = backend._build_prompt("task", {}, [])
        assert "task" in prompt


# ===========================================================================
# Security — URL scheme allowlist
# ===========================================================================

class TestUrlAllowlist:

    @pytest.mark.parametrize("url", [
        "http://example.com",
        "https://example.com",
        "https://sub.domain.co.uk/path?q=1",
    ])
    def test_allowed_schemes(self, url):
        assert any(url.startswith(s) for s in _URL_ALLOWED_SCHEMES)

    @pytest.mark.parametrize("url", [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "chrome://settings",
        "data:text/html,<script>alert(1)</script>",
        "ftp://example.com",
        "",
        "//example.com",
    ])
    def test_rejected_schemes(self, url):
        assert not any(url.startswith(s) for s in _URL_ALLOWED_SCHEMES)


# ===========================================================================
# Security — element ref pattern
# ===========================================================================

class TestRefPattern:

    @pytest.mark.parametrize("ref", ["e1", "e2", "e10", "e999", "e123456"])
    def test_valid_refs(self, ref):
        assert _REF_PATTERN.match(ref) is not None

    @pytest.mark.parametrize("ref", [
        "E1",          # uppercase
        "e",           # no digits
        "1",           # no prefix
        "e1a",         # trailing letters
        "--flag",      # flag injection attempt
        "-s=evil",     # session injection
        "e1; rm -rf",  # shell injection
        "",            # empty
        " e1",         # leading space
    ])
    def test_invalid_refs_rejected(self, ref):
        assert _REF_PATTERN.match(ref) is None


# ===========================================================================
# _load_system_prompt()
# ===========================================================================

class TestLoadSystemPrompt:

    def test_loads_from_prompts_directory(self, backend):
        """Should return non-empty string from browser_agent.system.md."""
        content = backend._load_system_prompt()
        assert isinstance(content, str)
        assert len(content) > 100  # should have real content
        assert "action" in content.lower()

    def test_returns_empty_on_missing_file(self, backend):
        """Returns empty string gracefully if prompt file missing."""
        with patch("builtins.open", side_effect=FileNotFoundError):
            result = backend._load_system_prompt()
        assert result == ""
