"""Playwright binary discovery helper for the browser plugin.

Locates the full Chromium executable used by playwright-cli.
Searches ~/.cache/ms-playwright first (playwright-cli install target),
then /a0/tmp/playwright (Agent Zero core install target).

Binary preference order: chrome > chrome.exe > headless_shell
(playwright-cli requires full Chrome, not headless_shell)

Used by PlaywrightCliBackend.get_browsers_path() to derive
PLAYWRIGHT_BROWSERS_PATH = 3x dirname from the binary.

If no binary is found, raises FileNotFoundError.
Run: playwright-cli install  (or use the plugin Initialize button)
"""
import glob
import os


def ensure_playwright_binary() -> str:
    """Locate the full Chromium executable for playwright-cli.

    Search order:
      1. ~/.cache/ms-playwright/ — playwright-cli install target (full Chrome)
      2. /a0/tmp/playwright/     — Agent Zero core install target (headless_shell)

    Binary preference: chrome first, headless_shell last.
    playwright-cli requires the full Chrome binary — headless_shell will fail.

    Returns:
        Absolute path to the Chromium executable.

    Raises:
        FileNotFoundError: if no Chromium binary can be located.
    """
    search_roots = [
        os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright"),
        "/a0/tmp/playwright",
    ]

    # chrome before headless_shell — playwright-cli needs full Chrome
    binary_names = ["chrome", "chrome.exe", "headless_shell"]

    for root in search_roots:
        if not os.path.isdir(root):
            continue
        for name in binary_names:
            pattern = os.path.join(root, "**", name)
            matches = sorted(glob.glob(pattern, recursive=True), reverse=True)
            for match in matches:
                if os.path.isfile(match) and os.access(match, os.X_OK):
                    return match

    raise FileNotFoundError(
        "Playwright Chromium binary not found. "
        "Run: playwright-cli install  (or use the plugin Initialize button)"
    )
