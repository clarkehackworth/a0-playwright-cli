"""Shared fixtures for a0_playwright_cli test suite.

Provides:
  - mock_agent: lightweight stub satisfying PlaywrightCliBackend constructor
  - backend: fresh PlaywrightCliBackend instance per test
  - browser_env: PLAYWRIGHT_BROWSERS_PATH resolved at session scope
  - session_id: unique per-test playwright-cli session name
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Make plugin importable without installing
# ---------------------------------------------------------------------------
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_browsers_path() -> str:
    """Locate PLAYWRIGHT_BROWSERS_PATH — same logic as PlaywrightCliBackend."""
    search_roots = [
        os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright"),
        "/a0/tmp/playwright",
    ]
    binary_names = ["chrome", "chrome.exe", "headless_shell"]
    for root in search_roots:
        if not os.path.isdir(root):
            continue
        for name in binary_names:
            pattern = os.path.join(root, "**", name)
            matches = sorted(glob.glob(pattern, recursive=True), reverse=True)
            for match in matches:
                if os.path.isfile(match) and os.access(match, os.X_OK):
                    binary = match
                    return os.path.dirname(os.path.dirname(os.path.dirname(binary)))
    return ""


def run_cli(*args, browsers_path: str = "") -> subprocess.CompletedProcess:
    """Run playwright-cli synchronously, return CompletedProcess."""
    env = {**os.environ}
    if browsers_path:
        env["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path
    return subprocess.run(
        ["playwright-cli"] + list(args),
        capture_output=True,
        text=True,
        timeout=30,
        cwd="/tmp",
        env=env,
    )


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def browsers_path() -> str:
    """Resolve PLAYWRIGHT_BROWSERS_PATH once per test session."""
    path = _find_browsers_path()
    if not path:
        pytest.skip("Playwright Chromium binary not found — run playwright-cli install")
    return path


# ---------------------------------------------------------------------------
# Function-scoped fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_agent():
    """Minimal mock agent satisfying PlaywrightCliBackend requirements."""
    agent = MagicMock()
    agent.context.id = uuid.uuid4().hex
    return agent


@pytest.fixture
def backend(mock_agent):
    """Fresh PlaywrightCliBackend instance for each test."""
    from helpers.playwright_cli_backend import PlaywrightCliBackend
    return PlaywrightCliBackend(mock_agent)


@pytest.fixture
def session_id(browsers_path) -> str:
    """Unique playwright-cli session name; auto-closes after test."""
    sid = f"test-{uuid.uuid4().hex[:8]}"
    yield sid
    # Best-effort cleanup
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": browsers_path}
    subprocess.run(
        ["playwright-cli", f"-s={sid}", "close"],
        env=env,
        capture_output=True,
        timeout=10,
        cwd="/tmp",
    )


@pytest.fixture(autouse=True)
def clean_playwright_cli_artifacts():
    """Remove .playwright-cli/ artifacts from plugin root after each test."""
    yield
    artifact_dir = PLUGIN_ROOT / ".playwright-cli"
    if artifact_dir.exists():
        import shutil
        shutil.rmtree(artifact_dir, ignore_errors=True)
