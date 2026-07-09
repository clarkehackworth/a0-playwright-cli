#!/usr/bin/env python3
"""Dump the plugin's effective config so the agent can build launch args from it.

The AI drives `playwright-cli` from a terminal. When it needs a remote/proxied
Chrome it must construct the Chrome command line itself — and the values
(proxy, cert handling, PwnFox tagging, headed) live in the plugin config, not
in the agent's head. This prints that config as JSON so a shell/agent can read
individual keys.

Usage:
    python config.py                     # print all config as JSON
    python config.py browser_proxy_server  # print one value (raw, no quotes)
    python config.py --check             # self-check

Effective config = default_config.yaml + config.json overrides + env, exactly
as get_plugin_config() resolves it — when run inside Agent Zero. Outside A0
(dev), it falls back to default_config.yaml plus `a0_playwright_cli__<key>` env
overrides and notes that on stderr.
"""
import json
import os
import sys

_PLUGIN_ROOT = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_NAME = "a0_playwright_cli"


def _via_a0() -> dict | None:
    """Resolve config through Agent Zero's real config system, if reachable."""
    d = _PLUGIN_ROOT
    for _ in range(8):  # walk up looking for the A0 root (has helpers/plugins.py)
        if os.path.exists(os.path.join(d, "helpers", "plugins.py")):
            if d not in sys.path:
                sys.path.insert(0, d)
            try:
                from helpers.plugins import get_plugin_config  # type: ignore
                return get_plugin_config(_PLUGIN_NAME) or {}
            except Exception:
                return None
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def _fallback() -> dict:
    """default_config.yaml + `a0_playwright_cli__<key>` env overrides."""
    import yaml
    with open(os.path.join(_PLUGIN_ROOT, "default_config.yaml")) as f:
        cfg = yaml.safe_load(f) or {}
    for key in list(cfg):
        env = os.environ.get(f"{_PLUGIN_NAME}__{key}")
        if env is not None:
            cfg[key] = env
    print("config.py: A0 not found — showing default_config.yaml + env only",
          file=sys.stderr)
    return cfg


def effective_config() -> dict:
    cfg = _via_a0()
    return cfg if cfg is not None else _fallback()


def _check() -> None:
    cfg = _fallback()
    for k in ("browser_proxy_server", "browser_ignore_cert_errors",
              "browser_cdp_endpoint", "browser_pwnfox_headers", "browser_headed"):
        assert k in cfg, f"missing expected key: {k}"
    os.environ[f"{_PLUGIN_NAME}__browser_proxy_server"] = "http://x:1"
    assert _fallback()["browser_proxy_server"] == "http://x:1", "env override not applied"
    print("ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        _check()
    elif len(sys.argv) > 1:
        val = effective_config().get(sys.argv[1], "")
        print("" if val is None else val)
    else:
        print(json.dumps(effective_config(), indent=2))
