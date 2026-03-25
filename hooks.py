"""Agent Zero plugin lifecycle hooks for a0_playwright_cli.

The ``install`` hook is called automatically by Agent Zero when the plugin
is enabled or updated.  It installs playwright-cli and Chromium so the
plugin works out-of-the-box without the user having to click Initialize.

The same logic is also available via the Initialize button on the plugin
page (initialize.py) and can be run standalone::

    python initialize.py
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent


def install(**kwargs) -> None:
    """Install playwright-cli and Chromium when the plugin is enabled/updated.

    Delegates to ``initialize.initialize()`` which performs four steps:
    1. Install playwright-cli via npm.
    2. Install Chromium via ``playwright-cli install``.
    3. Write ``~/.playwright/cli.config.json`` pointing at the binary.
    4. Create ``/opt/google/chrome/chrome`` wrapper script for Docker.

    Any failure is logged and re-raised so Agent Zero can surface it to
    the user rather than silently leaving the plugin in a broken state.
    """
    # Clear cached modules so a plugin update picks up fresh code
    _clear_plugin_modules()

    # Load initialize module from plugin root (avoids sys.path pollution)
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location(
        "playwright_cli_initialize",
        str(_PLUGIN_ROOT / "initialize.py"),
    )
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)

    success = mod.initialize(plugin_dir=str(_PLUGIN_ROOT))
    if not success:
        raise RuntimeError(
            "Playwright CLI plugin initialization failed — "
            "check the output above and ensure Node.js is installed."
        )


def _clear_plugin_modules() -> None:
    """Remove cached plugin modules from sys.modules so updates take effect."""
    prefixes = (
        "playwright_cli_backend",
        "playwright_helper",
        "playwright_cli_initialize",
    )
    for name in list(sys.modules):
        if any(name == p or name.startswith(p + ".") for p in prefixes):
            sys.modules.pop(name, None)
    importlib.invalidate_caches()
