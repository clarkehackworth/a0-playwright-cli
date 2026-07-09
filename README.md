# A0 Playwright CLI

![Version](https://img.shields.io/badge/version-1.2.0-blue) ![License](https://img.shields.io/badge/license-MIT-green) ![Agent Zero](https://img.shields.io/badge/Agent%20Zero-plugin-orange)

Microsoft Playwright CLI browser automation plugin for [Agent Zero](https://github.com/frdel/agent-zero). Gives every agent a `browser_agent` tool to navigate, interact with, and extract data from any website using structured DOM snapshots with stable element references.

---

## Features

- 🎭 **Playwright CLI backend** — structured YAML DOM snapshots with stable element refs (`e1`, `e2`, ...)
- 🤖 **Uses Agent Zero Browser Model** — no separate LLM config needed, inherits your Settings → Agent → Browser Model
- 🔧 **Auto-skill injection** — the full Playwright CLI skill is injected into the agent system prompt automatically
- 📋 **30 browser actions** — navigation, interaction, keyboard & mouse, scroll, eval/JS, drag, dialogs, tabs, viewport, and more
- 🖥️ **Remote browser control** — attach to a running Chrome over CDP, or launch a dedicated headed Chrome per context; remote browsers stay open across tasks and are reused
- 🔒 **Security validated** — URL allowlist (http/https only), element ref pattern validation
- 📱 **Mobile/device emulation** — emulate any device viewport
- 🕸️ **Network mocking** — intercept and mock HTTP requests
- 🎬 **DevTools tracing & video** — record sessions for debugging
- 🚀 **One-click initialization** — installs playwright-cli and Chromium automatically

---

## Installation

### 1. Copy the plugin

```bash
cp -r playwright_cli /path/to/agent-zero/usr/plugins/
```

### 2. Enable in Agent Zero

Go to **Settings → Plugins → Playwright CLI** and toggle it on.

### 3. Initialize (automatic)

Click the **Initialize** button on the plugin page. This will:
- Install `playwright-cli` via npm (`npm install -g @playwright/cli@latest`)
- Install Chromium binaries (`playwright-cli install`)
- Write `~/.playwright/cli.config.json` pointing to the discovered Chromium binary

### Manual install (fallback)

If initialization fails:
```bash
npm install -g @playwright/cli@latest
playwright-cli install
```

### Deploy to a container

To push a working copy into a running `agent-zero` Docker container (over `ssh://docker.lan`):
```bash
./deploy.sh            # copy files + run initializer
./deploy.sh --restart  # also restart the container
```

---

## Configuration

This plugin **inherits the Browser Model** from Agent Zero's built-in settings.

Go to **Settings → Agent → Browser Model** to configure:

| Setting | Description |
|---------|--------------|
| Provider | LLM provider for browser decisions (e.g. openrouter, openai) |
| Model | Model name (e.g. anthropic/claude-sonnet-4-5) |
| Vision | Enable vision for screenshot-based decisions |
| Rate limits | Optional request/token rate limiting |

> No plugin-specific config page needed — all browser model settings live in the standard Agent Zero settings.

### Visible Chrome windows / attaching to a running Chrome

Two plugin config options (in `default_config.yaml` / plugin settings) control where the browser runs:

| Option | Effect |
|--------|--------|
| `browser_headed: true` | Each browser task opens a visible Chrome window (requires a display; not for Docker) |
| `browser_launch_chrome: true` | Launch a dedicated Chrome — own profile, own CDP port — and attach to it. Concurrent agent contexts each get their own window. |
| `browser_cdp_endpoint` | Attach to an already-running Chrome instead of launching one. Start Chrome with `google-chrome --remote-debugging-port=9222`, then set `http://127.0.0.1:9222`. |

Precedence: `browser_cdp_endpoint` > `browser_launch_chrome` > `browser_headed`.

Both remote modes are **persistent**: the browser window stays open between tasks and the session is reused — the next task continues in the same window (and inherits its logins/cookies). Close the window yourself, or run `playwright-cli close-all`, to reset.

### Pentesting through Burp Suite

These options route Chrome through an intercepting proxy for the AI to drive alongside a Burp MCP server. `browser_proxy_server`/`browser_ignore_cert_errors` apply to `browser_launch_chrome: true` mode; `browser_pwnfox_headers` works in every mode (enabling it forces a dedicated Chrome launch when neither `browser_launch_chrome` nor `browser_cdp_endpoint` is set, since plain playwright `open` exposes no CDP port to tag through):

| Option | Effect |
|--------|--------|
| `browser_proxy_server` | e.g. `http://127.0.0.1:8080` — Burp's proxy listener |
| `browser_ignore_cert_errors: true` | Skip TLS warnings from Burp's CA (or install Burp's CA as trusted instead) |
| `browser_pwnfox_headers: true` | Tag every request — including tabs/popups opened later — with `X-PwnFox-Color` ([PwnFox](https://github.com/yeswehack/PwnFox) convention) so Burp's proxy history can be filtered/highlighted per window |
| `browser_pwnfox_color` | Fix the color (`blue`/`turquoise`/`green`/`yellow`/`orange`/`red`/`pink`/`purple`); leave empty to auto-assign one per `window` name, so concurrent windows get distinct colors automatically |

Attaching to an already-running Chrome (`browser_cdp_endpoint`) works too — just launch it yourself with `--proxy-server=...` (and `--ignore-certificate-errors` if needed); PwnFox tagging attaches to that endpoint too.

#### Which field applies to which launch mode

The four Pentesting options do **not** all apply to every browser-launch path. There are four paths (see the table above plus the headless fallback), and each field's scope differs:

| Field | CDP attach (`browser_cdp_endpoint`) | Launch Chrome (`browser_launch_chrome`) | Plain `open` [--headed] | Headless Docker fallback |
|---|:---:|:---:|:---:|:---:|
| `browser_proxy_server` | ❌ set on your own Chrome cmdline | ✅ `--proxy-server` | ❌ | ❌ |
| `browser_ignore_cert_errors` | ❌ set it yourself | ✅ `--ignore-certificate-errors` | ❌ | ❌ |
| `browser_pwnfox_headers` | ✅ tagger attaches to endpoint | ✅ tagger attaches after launch | ❌ | ❌ |
| `browser_pwnfox_color` | ✅ (when headers on) | ✅ (when headers on) | ❌ | ❌ |

Two rules to internalize:

1. **Proxy + cert-errors are `browser_launch_chrome`-only.** In CDP-attach mode the plugin never sets Chrome's launch flags (it's your process), so bake `--proxy-server` / `--ignore-certificate-errors` into your own launch command.
2. **PwnFox spans both remote modes and can rewrite the path.** Enabling `browser_pwnfox_headers` while both remote modes are off *forces* a dedicated Chrome launch — tagging needs a CDP port and plain `open` exposes none.

So **plain `open` and the headless fallback honor none of these fields.** Setting a proxy with both remote modes off does nothing.

#### Letting the AI build the launch command

The agent can drive a proxied/headed Chrome itself (via the **playwright-cli** skill) instead of relying on the config-driven paths above. It reads the effective config with the bundled helper and constructs the Chrome command line — so it honors whatever the operator set without hardcoding:

```bash
python <plugin_root>/config.py                    # full effective config as JSON
python <plugin_root>/config.py browser_proxy_server   # one value, for shell use
```

The skill's "Remote / proxied browser" section has the full launch-and-attach recipe.

---

## How It Works

```
Parent Agent
    │
    │  browser_agent tool call
    ▼
BrowserAgent (tools/browser_agent.py)
    │
    │  start_task(message)
    ▼
PlaywrightCliBackend (helpers/playwright_cli_backend.py)
    │
    ├─ open browser session via playwright-cli
    │
    └─ LOOP (up to 50 steps):
         │
         ├─ snapshot → YAML DOM with element refs (e1, e2, ...)
         │
         ├─ LLM decision (Browser Model)
         │    SystemMessage: browser_agent.system.md (action protocol)
         │    HumanMessage:  task + snapshot + action history
         │
         ├─ execute action (goto/click/fill/press/...)
         │
         └─ done? → return result to parent agent
```

---

## Available Actions

### Navigation
| Action | Description |
|--------|-------------|
| `goto` | Navigate to URL (http/https only) |
| `go-back` | Navigate back |
| `go-forward` | Navigate forward |
| `reload` | Reload page |
| `wait` | Wait N seconds for dynamic content (max 30) |

### Interaction
| Action | Description |
|--------|-------------|
| `click` | Click element by ref |
| `dblclick` | Double-click element |
| `fill` | Clear and fill input field |
| `type` | Type text at cursor |
| `press` | Press keyboard key (Enter, Tab, ArrowDown...) |
| `select` | Select dropdown option |
| `check` | Check checkbox |
| `uncheck` | Uncheck checkbox |
| `hover` | Hover over element |
| `drag` | Drag element (`ref`) onto target element (`target`) |
| `upload` | Upload file to input element |

### Keyboard & Mouse
| Action | Description |
|--------|-------------|
| `keydown` | Hold modifier key (Shift, Control, Alt, Meta) |
| `keyup` | Release held modifier key |
| `mousemove` | Move mouse to absolute x/y coordinates |
| `mousedown` | Press mouse button (default: left) |
| `mouseup` | Release mouse button (default: left) |
| `scroll` | Scroll page by dy pixels (positive = down) |

### Page State
| Action | Description |
|--------|-------------|
| `snapshot` | Force fresh DOM snapshot |
| `screenshot` | Take screenshot |
| `eval` | Evaluate JavaScript expression (optionally on element ref) |
| `run-code` | Run inline JS `async page => { ... }` |
| `resize` | Resize viewport (`value`: `"width height"`) |

### Dialogs
| Action | Description |
|--------|-------------|
| `dialog-accept` | Accept browser dialog (optional confirmation text) |
| `dialog-dismiss` | Dismiss browser dialog |

### Tabs
| Action | Description |
|--------|-------------|
| `tab-new` | Open new tab (optional URL) |
| `tab-close` | Close current tab |
| `tab-select` | Switch to tab by index (0-based) |
| `tab-list` | List all open tabs |

### Completion
| Action | Description |
|--------|-------------|
| `done` | Task complete — return full result |

---

## Usage

The `browser_agent` tool is available to all agents when the plugin is enabled:

```json
{
  "tool_name": "browser_agent",
  "tool_args": {
    "message": "Go to https://example.com and return the page title",
    "reset": "true"
  }
}
```

```json
{
  "tool_name": "browser_agent",
  "tool_args": {
    "message": "Considering open pages, click the Submit button and confirm the result. End task.",
    "reset": "false"
  }
}
```

- `reset: true` — spawn a fresh browser session
- `reset: false` — continue the existing session (start message with "Considering open pages...")

---

## Plugin Structure

```
playwright_cli/
├── plugin.yaml                          # Plugin manifest
├── initialize.py                        # Auto-installer for playwright-cli + Chromium
├── default_config.yaml                  # Config (browser model + remote browser modes)
├── deploy.sh                            # Push the plugin to the agent-zero container
├── tools/
│   └── browser_agent.py                 # browser_agent tool
├── helpers/
│   ├── playwright_cli_backend.py        # Core agentic browser loop
│   └── playwright.py                    # Chromium binary discovery
├── extensions/
│   └── python/
│       ├── agent_init/
│       │   └── _20_browser_plugin_config.py   # Plugin init hook
│       └── system_prompt/
│           └── _16_playwright_cli_skill_prompt.py  # Skill auto-injection
├── prompts/
│   ├── browser_agent.system.md          # Internal browser LLM instructions
│   └── agent.system.tool.browser.md    # Parent agent tool description
├── webui/
│   └── config.html                      # Settings info card
└── skills/
    └── playwright-cli/                  # Bundled Playwright CLI skill
        ├── SKILL.md
        └── references/
```

---

## Requirements

- **Node.js** (for `npm install -g @playwright/cli`)
- **Agent Zero** with plugin support
- Browser model configured in Agent Zero Settings (any LLM provider)

---

## License

MIT — Copyright (c) 2026 Emichi d.o.o. See [LICENSE](LICENSE) for details.

---

## Changelog

### Unreleased

#### Remote browser control
- `browser_headed` — open a visible Chrome window per task (needs a display)
- `browser_launch_chrome` — launch a dedicated Chrome (own profile + CDP port), attach to it, and keep it open across tasks
- `browser_cdp_endpoint` — attach to an already-running Chrome via CDP
- Remote modes (CDP / launched) are **persistent**: the browser stays open between tasks and the session is reused, so page and login state carry over. Precedence: `browser_cdp_endpoint` > `browser_launch_chrome` > `browser_headed`.
- Added `deploy.sh` to push the plugin to the `agent-zero` container.

### v1.2.0 — 2026-03-25

#### New Actions (+16)

Expanded `PlaywrightCliBackend._execute_action()` from 16 to 32 action branches:

| New Action | Description |
|-----------|-------------|
| `scroll` / `mousewheel` | Scroll page by dx/dy pixels |
| `eval` | Evaluate JavaScript expression, optionally against an element ref |
| `drag` | Drag source element (`ref`) to target element (`target`) |
| `tab-select` | Switch to tab by 0-based index |
| `tab-list` | List all open tabs |
| `keydown` | Hold modifier key (Shift, Control, Alt, Meta) |
| `keyup` | Release held modifier key |
| `dialog-accept` | Accept browser alert/confirm/prompt |
| `dialog-dismiss` | Dismiss browser dialog |
| `resize` | Resize viewport to given width × height |
| `wait` | Sleep N seconds for dynamic content (max 30s cap) |
| `mousemove` | Move mouse to absolute x/y page coordinates |
| `mousedown` | Press mouse button |
| `mouseup` | Release mouse button |
| `upload` | Upload file to a file input element |
| `run-code` | Execute inline JS string `async page => { ... }` |

#### Updated
- `browser_agent.system.md` — full action reference table with all 30 actions, grouped by category, with usage rules for scroll, drag, eval, resize, wait

### v1.1.0 — 2026-03-25

#### Bug Fixes
- **`get_log()` implemented** — `PlaywrightCliBackend` now exposes a `get_log()` method populated throughout task execution. Previously, the `hasattr` guard in `BrowserAgent` always returned `False`, leaving the Agent Zero progress log empty for every browser task.
- **`get_screenshot()` implemented** — `PlaywrightCliBackend` now exposes an async `get_screenshot(path)` method. Previously, screenshots were never captured or surfaced in the tool log despite the infrastructure being wired up.
- **`_truncate_snapshot()` crash fix** — The playwright-cli YAML snapshot format is a top-level list, not a dict. The previous implementation called `dict(snapshot)` on this list, raising `ValueError` and silently crashing every browser task after the first snapshot. Now handles both list (actual format) and dict (fallback) correctly.

#### New
- **`hooks.py`** — Plugin now auto-installs playwright-cli and Chromium when enabled or updated via Agent Zero's plugin lifecycle hook. No need to manually click Initialize.
- **`LICENSE`** — MIT license added with Apache 2.0 attribution for upstream playwright-cli (Microsoft Corporation).

#### Improvements
- `plugin.yaml` — removed non-standard `note` field; content merged into `description`.
- `webui/config.html` — removed redundant `<template x-if="true">` wrapper; now clean static HTML.

### v1.0.0 — 2026-03-19

- Initial release.
