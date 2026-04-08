"""Plugin execute script — called when the user clicks 'Initialize' in the plugin list.

Installs playwright-cli (npm) and Chromium browser binaries so the plugin
works out of the box without manual setup.

Delegates all logic to initialize.initialize().
Can also be run standalone: python execute.py
"""
import logging
import sys
from pathlib import Path

_PLUGIN_ROOT = str(Path(__file__).resolve().parent)


def main() -> int:
    """Entry point for the Initialize button. Returns 0 on success, 1 on failure."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location(
        "playwright_cli_initialize",
        str(Path(_PLUGIN_ROOT) / "initialize.py"),
    )
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)

    success = mod.initialize(plugin_dir=_PLUGIN_ROOT)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
