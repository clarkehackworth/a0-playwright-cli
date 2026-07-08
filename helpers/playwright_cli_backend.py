"""PlaywrightCliBackend — Microsoft Playwright CLI browser automation backend.

Provides structured DOM snapshots with stable element refs (e1/e2...), mobile emulation,
network mocking, and DevTools tracing via playwright-cli shell commands.

Reuses existing Playwright browser binaries from the ms-playwright cache.
Binary path: 3x dirname from ensure_playwright_binary() = PLAYWRIGHT_BROWSERS_PATH.

Session ID: f"a0-{context_id_hex[:16]}" — 16 hex chars, negligible collision probability.

Public API (used by BrowserAgent tool):
  start_task(task) -> PlaywrightCliTask   (with .is_ready(), .is_alive(), .result(), .kill(), .execute_inside())
  kill_task()                              (sync, cancels asyncio task + closes CLI session)
  task: PlaywrightCliTask | None

LLM decision format:
  {"action": "goto|click|fill|type|snapshot|done",
   "ref": "e1|e2|...",
   "value": "<url or text or final answer>",
   "reasoning": "<why>",
   "done": false}

Snapshot: saved to /tmp/pw-snap-<session_id>.yml via --filename flag, parsed with pyyaml.
Elements truncated to top SNAPSHOT_MAX_ELEMENTS before serialization (dict-level, not string-slice).
Total snapshot JSON capped at SNAPSHOT_MAX_BYTES to prevent LLM context overflow.

Security:
  - goto: only http:// and https:// URLs accepted (blocks file://, javascript:, chrome:// etc.)
  - click/fill ref: must match ^e\\d+$ pattern (blocks flag injection via --arg style refs)
  - Task string embedded in prompt — inherent prompt injection risk acknowledged.
    Parent agent secrets are masked before reaching this class. No additional boundary
    enforcement can be guaranteed; operators should restrict task content to trusted inputs.
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import time
import logging
from typing import Optional

log = logging.getLogger(__name__)

# Absolute imports via importlib — file loaded via importlib, relative imports forbidden
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import importlib.util as _ilu

# Compiled patterns (module-level, not per-call)
_REF_PATTERN = re.compile(r'^e\d+$')
_URL_ALLOWED_SCHEMES = ('http://', 'https://')


def _load_module(name: str, relpath: str):
    """Load module by absolute path. Checks sys.modules first to prevent duplicate instances."""
    if name in sys.modules:
        return sys.modules[name]
    spec = _ilu.spec_from_file_location(name, os.path.join(_PLUGIN_ROOT, relpath))
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod  # Register before exec_module to prevent recursive double-load
    spec.loader.exec_module(mod)
    return mod



# ---------------------------------------------------------------------------
# Minimal CDP WebSocket client (stdlib only — no playwright/websockets dependency).
# Just enough RFC6455 to send commands and read frames on a single connection;
# used only to keep a Network.setExtraHTTPHeaders override alive (see
# PlaywrightCliBackend._pwnfox_tag_task).
# ---------------------------------------------------------------------------

async def _cdp_ws_connect(ws_url: str):
    import base64
    from urllib.parse import urlparse
    u = urlparse(ws_url)
    reader, writer = await asyncio.open_connection(u.hostname, u.port or 80)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {u.path or '/'} HTTP/1.1\r\n"
        f"Host: {u.hostname}:{u.port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    writer.write(req.encode())
    await writer.drain()
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = await reader.read(4096)
        if not chunk:
            break
        resp += chunk
    if b" 101 " not in resp.split(b"\r\n", 1)[0]:
        raise RuntimeError(f"CDP websocket handshake failed: {resp[:200]!r}")
    return reader, writer


def _ws_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """Encode a single masked client->server text frame (client frames must be masked)."""
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    length = len(payload)
    if length <= 125:
        header = bytes([0x80 | opcode, 0x80 | length])
    elif length <= 65535:
        header = bytes([0x80 | opcode, 0x80 | 126]) + length.to_bytes(2, "big")
    else:
        header = bytes([0x80 | opcode, 0x80 | 127]) + length.to_bytes(8, "big")
    return header + mask + masked


async def _ws_read_frame(reader) -> bytes:
    """Read one server->client frame (unmasked, not fragmented — sufficient for CDP)."""
    first2 = await reader.readexactly(2)
    length = first2[1] & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    return await reader.readexactly(length)


async def _cdp_send(
    writer, msg_id: int, method: str, params: Optional[dict] = None, session_id: Optional[str] = None
) -> None:
    msg = {"id": msg_id, "method": method, "params": params or {}}
    if session_id:
        msg["sessionId"] = session_id  # flattened-protocol routing to an attached target
    writer.write(_ws_frame(json.dumps(msg).encode()))
    await writer.drain()


# ---------------------------------------------------------------------------
# Result wrapper — implements the interface BrowserAgent.execute() expects
# Result wrapper returned by PlaywrightCliTask.result()
# ---------------------------------------------------------------------------

class PlaywrightCliResult:
    """Result object returned by PlaywrightCliTask.result().
    Implements the interface expected by BrowserAgent.execute() after the wait loop.
    """

    def __init__(self, result_text: str):
        self._result_text = result_text or ""

    def is_done(self) -> bool:
        return True

    def final_result(self) -> str:
        return self._result_text

    def urls(self) -> list:
        return []


# ---------------------------------------------------------------------------
# Task wrapper — implements the interface BrowserAgent.execute() expects
# Task wrapper returned by PlaywrightCliBackend.start_task()
# ---------------------------------------------------------------------------

class PlaywrightCliTask:
    """Wraps an asyncio.Task and exposes the async task interface
    used by BrowserAgent to poll progress and retrieve results.

    API:
      .is_ready()           → True when task is done
      .is_alive()           → True while task is running
      .result()             → awaitable returning PlaywrightCliResult
      .kill()               → cancel the task
      .execute_inside(fn)   → run coroutine fn in current async context
    """

    def __init__(self, async_task: asyncio.Task, backend: "PlaywrightCliBackend"):
        self._async_task = async_task
        self._backend = backend

    def is_ready(self) -> bool:
        """True when asyncio task has completed (success, failure, or cancelled)."""
        return self._async_task.done()

    def is_alive(self) -> bool:
        """True while asyncio task is still running."""
        return not self._async_task.done()

    async def result(self) -> PlaywrightCliResult:
        """Await completion and return result. Safe to call after is_ready() is True."""
        if not self._async_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._async_task), timeout=30)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        return PlaywrightCliResult(self._backend._result or "")

    def kill(self, terminate_thread: bool = False) -> None:
        """Cancel the asyncio task."""
        if not self._async_task.done():
            self._async_task.cancel()

    async def execute_inside(self, coro_fn) -> None:
        """Execute a coroutine inside this task's context.
        For PlaywrightCliBackend: run directly in current async context.
        No-op if task is already done.
        """
        if not self._async_task.done():
            try:
                await coro_fn()
            except Exception as e:
                log.debug("PlaywrightCliTask.execute_inside: %s", e)


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

class PlaywrightCliBackend:
    """Browser automation backend using Microsoft Playwright CLI.

    Public API used by BrowserAgent tool:
      start_task(task) -> PlaywrightCliTask
      kill_task()
      task: PlaywrightCliTask | None
    """

    MAX_STEPS = 50
    SNAPSHOT_MAX_ELEMENTS = 50  # Truncate at dict level, never slice JSON string
    SNAPSHOT_MAX_BYTES = 16000  # Cap total snapshot JSON to prevent LLM context overflow
    TASK_TIMEOUT = 300  # seconds

    def __init__(self, agent, window: str = "default"):
        self.agent = agent
        # Window name → separate playwright-cli session + browser window, so one
        # context can drive several independent windows. Sanitized to a safe slug.
        self.window = re.sub(r"[^a-z0-9_-]", "", str(window or "default").lower())[:24] or "default"
        # State interface compatibility attributes
        self.task: Optional[PlaywrightCliTask] = None
        # Internal state
        self._async_task: Optional[asyncio.Task] = None
        self._result: Optional[str] = None
        self._log_lines: list = []  # Progress log consumed by BrowserAgent via get_log()
        self._chrome_proc = None  # Per-task Chrome process (browser_launch_chrome mode)
        self._chrome_profile = None  # Its unique --user-data-dir, removed on stop
        self._pwnfox_task = None  # Background CDP connection tagging requests (see _pwnfox_tag_task)
        self._video_recording = False  # True between video-start and video-stop
        self._video_path = None  # Path passed to video-start (video-stop takes no filename)
        self._last_artifact = None  # Absolute path of the last saved video/screenshot
        self._last_tabs = None  # tab-list markdown from the last tab action, surfaced to the LLM
        self._cdp = ""  # CDP http endpoint for this task, when known (attach / per-task launch)
        self._pinned_target = None  # CDP targetId of the tab the agent is working on (stable across index shifts + navigation)

    # ── Session helpers ──────────────────────────────────────────────────────

    def get_session_id(self) -> str:
        """Session ID for playwright-cli -s flag.
        Uses 16 hex chars (vs 8 in original) to reduce collision probability
        in concurrent multi-agent deployments.
        """
        raw = self.agent.context.id.replace('-', '')
        return f"a0-{raw[:16]}-{self.window}"

    def get_browsers_path(self) -> str:
        """Return PLAYWRIGHT_BROWSERS_PATH by traversing 3x dirname from the binary.

        binary:          ~/.cache/ms-playwright/chromium-1148/chrome-linux/chrome
        dirname x1:      ~/.cache/ms-playwright/chromium-1148/chrome-linux
        dirname x2:      ~/.cache/ms-playwright/chromium-1148
        dirname x3:      ~/.cache/ms-playwright   ← correct PLAYWRIGHT_BROWSERS_PATH

        2x dirname is WRONG — gives the version-specific dir, causing
        'Executable doesn't exist' errors in playwright-cli.
        """
        pw_helper = _load_module("playwright_helper", "helpers/playwright.py")
        binary_path = pw_helper.ensure_playwright_binary()
        return os.path.dirname(os.path.dirname(os.path.dirname(binary_path)))

    def _make_env(self) -> dict:
        """Build subprocess environment with PLAYWRIGHT_BROWSERS_PATH set.

        playwright-cli uses PLAYWRIGHT_BROWSERS_PATH to find Chromium.
        We derive it via get_browsers_path() = 3x dirname from the chrome binary,
        which resolves to ~/.cache/ms-playwright — the correct directory.

        helpers/playwright.py searches ~/.cache/ms-playwright FIRST and prefers
        'chrome' over 'headless_shell', ensuring the full Chrome binary is found.
        """
        return {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": self.get_browsers_path()}

    def _plugin_cfg(self) -> dict:
        """Return plugin config dict (empty on any error)."""
        try:
            from helpers.plugins import get_plugin_config
            return get_plugin_config("a0_playwright_cli", agent=self.agent) or {}
        except Exception as e:
            log.debug("PlaywrightCliBackend._plugin_cfg: %s", e)
            return {}

    def _get_cdp_endpoint(self) -> str:
        """Configured CDP endpoint of an externally-running Chrome, or ''."""
        ep = str(self._plugin_cfg().get("browser_cdp_endpoint") or "").strip()
        if ep and not ep.startswith(("http://", "https://", "ws://", "wss://")):
            log.warning("PlaywrightCliBackend: ignoring invalid CDP endpoint '%s'", ep[:100])
            return ""
        return ep

    def _launch_per_task(self) -> bool:
        """True when config asks for a dedicated Chrome per task (attached via CDP)."""
        return str(self._plugin_cfg().get("browser_launch_chrome") or "").strip().lower() in (
            "true", "1", "yes", "on",
        )

    @staticmethod
    def _find_chrome_binary() -> str:
        """Locate a Chrome/Chromium binary: system Chrome first, playwright's as fallback."""
        import shutil
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            path = shutil.which(name)
            if path:
                return path
        pw_helper = _load_module("playwright_helper", "helpers/playwright.py")
        return pw_helper.ensure_playwright_binary()

    async def _launch_chrome(self, sid: str, url: str) -> str:
        """Launch a dedicated Chrome with its own profile and CDP port.

        Returns the CDP endpoint URL once /json/version responds.
        The process handle is kept in self._chrome_proc for cleanup.
        """
        import socket
        import urllib.request
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        # ponytail: free-port probe races with Chrome's bind; collisions are
        # vanishingly rare — surface as a task error, retry logic if it ever bites
        binary = self._find_chrome_binary()
        # Unique profile per launch. A fixed per-sid path collides with the still-open
        # Chrome from a previous task (persistent mode keeps it alive): the second
        # process finds the SingletonLock, prints "Opening in existing browser session",
        # forwards to the old instance and exits — so the new CDP port never opens and
        # the launch hangs/flaps. A fresh dir has no lock to collide with.
        self._chrome_profile = tempfile.mkdtemp(prefix=f"a0-chrome-{sid}-")
        args = [
            binary,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self._chrome_profile}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        proxy = str(self._plugin_cfg().get("browser_proxy_server") or "").strip()
        if proxy:
            args.append(f"--proxy-server={proxy}")
        if self._cfg_flag("browser_ignore_cert_errors"):
            args.append("--ignore-certificate-errors")
        args.append(url)
        self._chrome_proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        endpoint = f"http://127.0.0.1:{port}"
        for _ in range(50):  # up to ~15s for Chrome to open the CDP port
            if self._chrome_proc.returncode is not None:
                raise RuntimeError(
                    f"Chrome exited during startup (code {self._chrome_proc.returncode})"
                )
            try:
                await asyncio.to_thread(
                    urllib.request.urlopen, f"{endpoint}/json/version", timeout=1
                )
                log.info("PlaywrightCliBackend: launched Chrome (pid %s) at %s",
                         self._chrome_proc.pid, endpoint)
                if self._cfg_flag("browser_pwnfox_headers"):
                    self._pwnfox_task = asyncio.create_task(
                        self._pwnfox_tag_task(endpoint, self._pwnfox_color())
                    )
                return endpoint
            except Exception:
                await asyncio.sleep(0.3)
        self._stop_chrome()
        raise RuntimeError(f"Chrome CDP endpoint not ready at {endpoint} after 15s")

    def _cfg_flag(self, key: str) -> bool:
        return str(self._plugin_cfg().get(key) or "").strip().lower() in ("true", "1", "yes", "on")

    _PWNFOX_COLORS = ("blue", "turquoise", "green", "yellow", "orange", "red", "pink", "purple")

    def _pwnfox_color(self) -> str:
        """Header value for X-PwnFox-Color (github.com/yeswehack/PwnFox convention)."""
        cfg = str(self._plugin_cfg().get("browser_pwnfox_color") or "").strip().lower()
        if cfg in self._PWNFOX_COLORS:
            return cfg
        # ponytail: no fixed color configured — derive a stable one from the window
        # name so concurrent windows land on different colors in Burp automatically.
        import hashlib
        idx = int(hashlib.md5(self.window.encode()).hexdigest(), 16) % len(self._PWNFOX_COLORS)
        return self._PWNFOX_COLORS[idx]

    async def _pwnfox_tag_task(self, endpoint: str, color: str) -> None:
        """Keep a browser-level CDP connection open, auto-attaching to every existing
        and future target (tabs, popups, iframes) and tagging each with X-PwnFox-Color
        so Burp's proxy history can be filtered/highlighted per window — including
        tabs opened later via window.open/tab-new, not just the one Chrome opens at
        launch. Uses a hand-rolled WS client (stdlib only, no new dependency).
        """
        import urllib.request
        try:
            body = await asyncio.to_thread(
                lambda: urllib.request.urlopen(f"{endpoint}/json/version", timeout=2).read()
            )
            browser_ws = json.loads(body)["webSocketDebuggerUrl"]
            reader, writer = await _cdp_ws_connect(browser_ws)
            try:
                await _cdp_send(
                    writer, 1, "Target.setAutoAttach",
                    {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
                )
                log.info("PlaywrightCliBackend: tagging requests with X-PwnFox-Color=%s", color)
                next_id = 2
                while True:
                    frame = await _ws_read_frame(reader)
                    try:
                        msg = json.loads(frame)
                    except Exception:
                        continue
                    if msg.get("method") != "Target.attachedToTarget":
                        continue
                    params = msg.get("params", {})
                    session_id = params.get("sessionId")
                    target_type = params.get("targetInfo", {}).get("type")
                    if not session_id or target_type not in ("page", "background_page"):
                        continue
                    await _cdp_send(writer, next_id, "Network.enable", {}, session_id)
                    next_id += 1
                    await _cdp_send(
                        writer, next_id, "Network.setExtraHTTPHeaders",
                        {"headers": {"X-PwnFox-Color": color}}, session_id,
                    )
                    next_id += 1
            finally:
                writer.close()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("PlaywrightCliBackend: PwnFox header tagging failed: %s", e)

    def _stop_chrome(self) -> None:
        """Terminate the per-task Chrome, if we launched one. Best-effort."""
        task = self._pwnfox_task
        self._pwnfox_task = None
        if task and not task.done():
            task.cancel()
        proc = self._chrome_proc
        self._chrome_proc = None
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        profile = self._chrome_profile
        self._chrome_profile = None
        if profile:
            import shutil
            shutil.rmtree(profile, ignore_errors=True)

    # ── Stable tab-identity pinning ───────────────────────────────────────────
    # The daemon addresses tabs only by positional index, which shifts when the
    # user closes a tab, and by _currentTab, which silently jumps to a neighbour
    # if the user closes the agent's active tab. To keep the agent glued to *its*
    # tab we pin the CDP targetId (a GUID Chrome assigns per tab, constant for the
    # tab's whole lifetime, unaffected by index shifts or navigation) and re-map
    # it to the live index each step. Only works when we have an http CDP endpoint
    # (per-task launch / http attach); a no-op otherwise.
    # ponytail: matches targetId<->tab-list by URL, so two tabs on the SAME url are
    # ambiguous (first match wins). Expose targetId in tab-list upstream if it bites.

    _TAB_LINE_RE = re.compile(r"^- (\d+):(\s*\(current\))?\s*\[.*?\]\((.*?)\)", re.M)

    async def _cdp_page_targets(self) -> list:
        """List of {'id','url'} for every page target, via {endpoint}/json. [] if unavailable."""
        if not self._cdp.startswith(("http://", "https://")):
            return []  # ws:// or unknown endpoint — pinning disabled
        import urllib.request
        try:
            body = await asyncio.to_thread(
                lambda: urllib.request.urlopen(f"{self._cdp}/json", timeout=2).read()
            )
            return [
                {"id": t.get("id"), "url": t.get("url", "")}
                for t in json.loads(body)
                if t.get("type") == "page" and t.get("id")
            ]
        except Exception as e:
            log.debug("PlaywrightCliBackend: /json target list failed: %s", e)
            return []

    async def _pin_current_tab(self, sid: str) -> None:
        """Record the currently-active tab's targetId as the agent's pinned tab."""
        targets = await self._cdp_page_targets()
        if not targets:
            self._pinned_target = None
            return
        try:
            out = await self._run_cmd([f"-s={sid}", "tab-list"])
        except Exception:
            return
        cur_url = next(
            (m.group(3) for m in self._TAB_LINE_RE.finditer(out or "") if m.group(2)), None
        )
        if cur_url is None:
            return
        match = next((t for t in targets if t["url"] == cur_url), None)
        if match:
            self._pinned_target = match["id"]
            log.debug("PlaywrightCliBackend: pinned agent tab %s (%s)", match["id"], cur_url)

    async def _ensure_pinned_tab(self, sid: str) -> str:
        """Re-select the pinned tab if the user's tab activity moved the daemon off it.

        Returns a short note for the agent's history when something notable happened
        (its tab was closed, or was restored from under a user switch), else "".
        """
        if not self._pinned_target:
            return ""
        targets = await self._cdp_page_targets()
        if not targets:
            return ""
        pinned = next((t for t in targets if t["id"] == self._pinned_target), None)
        if pinned is None:
            # The agent's working tab was closed (by the user, or a script). Re-pin to
            # whatever the daemon fell back to so the agent keeps going, and tell it.
            self._pinned_target = None
            await self._pin_current_tab(sid)
            log.info("PlaywrightCliBackend: agent's pinned tab was closed; re-pinned to current")
            return "Your working tab was closed; now operating on the current tab. Re-check with tab-list."
        try:
            out = await self._run_cmd([f"-s={sid}", "tab-list"])
        except Exception:
            return ""
        rows = [(int(m.group(1)), bool(m.group(2)), m.group(3))
                for m in self._TAB_LINE_RE.finditer(out or "")]
        cur = next((r for r in rows if r[1]), None)
        if cur and cur[2] == pinned["url"]:
            return ""  # already on the pinned tab
        idx = next((r[0] for r in rows if r[2] == pinned["url"]), None)
        if idx is None:
            return ""  # can't locate it in the daemon's list — leave the agent be
        await self._run_cmd([f"-s={sid}", "tab-select", str(idx)])
        log.info("PlaywrightCliBackend: restored agent tab (index %s) after user tab switch", idx)
        return ""

    def _is_persistent(self) -> bool:
        """Remote-browser modes keep the browser open across tasks."""
        return bool(self._get_cdp_endpoint()) or self._launch_per_task()

    async def _session_alive(self, sid: str) -> bool:
        """True if playwright-cli's daemon still holds a session named sid."""
        try:
            out = await self._run_cmd(["list"])
            # Match the exact list entry ("- <sid>:"), not a loose substring, so a
            # window named "check" doesn't match another window's "checkout" session.
            return re.search(rf"(?m)^\s*-\s*{re.escape(sid)}\s*:", out or "") is not None
        except Exception:
            return False

    def _is_headed(self) -> bool:
        """True when config asks for a visible Chrome window."""
        return str(self._plugin_cfg().get("browser_headed") or "").strip().lower() in (
            "true", "1", "yes", "on",
        )

    def _build_llm(self):
        """Resolve the LLM to use for browser task decisions.

        Priority order:
          1. Plugin config (browser_provider + browser_model set in plugin settings UI)
          2. agent.get_chat_model() — always-available fallback

        Returns a LangChain-compatible chat model with ainvoke() support.
        """
        try:
            import models
            cfg = self._plugin_cfg()
            provider = (cfg.get("browser_provider") or "").strip()
            model_name = (cfg.get("browser_model") or "").strip()
            if provider and model_name:
                api_key = (cfg.get("browser_api_key") or "").strip()
                api_base = (cfg.get("browser_api_base") or "").strip()
                mc = models.ModelConfig(
                    type=models.ModelType.CHAT,
                    provider=provider,
                    name=model_name,
                    api_key=api_key,
                    api_base=api_base,
                )
                provider_name, kwargs = models._merge_provider_defaults(
                    "chat", provider, mc.build_kwargs()
                )
                llm = models._get_litellm_chat(
                    models.LiteLLMChatWrapper,
                    model_name,
                    provider_name,
                    mc,
                    **kwargs,
                )
                log.debug(
                    "PlaywrightCliBackend: using plugin-configured browser model %s/%s",
                    provider, model_name,
                )
                return llm
        except Exception as e:
            log.warning("PlaywrightCliBackend._build_llm: plugin config error: %s", e)

        # Fallback: use the agent's chat model
        return self.agent.get_chat_model()

    @staticmethod
    def validate_binary() -> bool:
        """Check that playwright-cli binary is available. Returns True if found."""
        import shutil
        return shutil.which("playwright-cli") is not None

    # ── CLI runner ────────────────────────────────────────────────────────────

    async def _run_cmd(self, args: list) -> str:
        """Run playwright-cli command, return stdout.
        Raises RuntimeError on non-zero exit. stderr captured up to 2000 chars.
        """
        cmd = ["playwright-cli"] + args
        log.debug("PlaywrightCliBackend: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._make_env(),
            cwd="/tmp",  # playwright-cli writes .playwright-cli/ artifacts to cwd — use /tmp to keep project clean
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            # Capture 2000 chars of stderr — actionable detail is often at end
            stderr_text = stderr.decode(errors='replace')
            err_excerpt = stderr_text[-2000:] if len(stderr_text) > 2000 else stderr_text
            raise RuntimeError(
                f"playwright-cli error (exit {proc.returncode}): {err_excerpt}"
            )
        return stdout.decode()

    # ── Public lifecycle API ──────────────────────────────────────────────────

    @staticmethod
    def _ensure_chrome_wrapper() -> None:
        """Ensure /opt/google/chrome/chrome wrapper script exists.

        playwright-cli always looks for 'chrome' at /opt/google/chrome/chrome.
        In Docker there is no sandbox and no display, so we create a wrapper
        script that injects --no-sandbox and --headless=new automatically.

        Called from start_task() on every invocation — safe to call repeatedly,
        skips creation if wrapper already exists and is executable.
        """
        wrapper_path = "/opt/google/chrome/chrome"
        if os.path.isfile(wrapper_path) and os.access(wrapper_path, os.X_OK):
            return  # already exists
        try:
            pw_helper = _load_module("playwright_helper", "helpers/playwright.py")
            chrome_binary = pw_helper.ensure_playwright_binary()
        except Exception as e:
            log.warning("_ensure_chrome_wrapper: could not find Chrome binary: %s", e)
            return
        try:
            os.makedirs("/opt/google/chrome", exist_ok=True)
            wrapper_content = (
                "#!/bin/bash\n"
                f'exec "{chrome_binary}" --no-sandbox --disable-setuid-sandbox --headless=new "$@"\n'
            )
            with open(wrapper_path, "w") as f:
                f.write(wrapper_content)
            os.chmod(wrapper_path, 0o755)
            log.info("_ensure_chrome_wrapper: created %s -> %s", wrapper_path, chrome_binary)
        except Exception as e:
            log.warning("_ensure_chrome_wrapper: failed to create wrapper: %s", e)

    def start_task(self, task: str) -> PlaywrightCliTask:
        """Schedule _run_task as an asyncio task. Must be called from async context.
        Returns PlaywrightCliTask wrapping the asyncio task.
        """
        # Ensure Chrome wrapper exists at /opt/google/chrome/chrome (restart-proof).
        # Skip for headed/CDP modes — the wrapper hardcodes --headless=new and only
        # exists for headless Docker environments.
        if not self._get_cdp_endpoint() and not self._is_headed():
            self._ensure_chrome_wrapper()
        # Pre-flight check: binary must exist
        if not self.validate_binary():
            raise RuntimeError(
                "playwright-cli binary not found on PATH. "
                "Install with: npm install -g @playwright/cli@latest"
            )
        loop = asyncio.get_running_loop()
        self._async_task = loop.create_task(self._run_task(task))
        self.task = PlaywrightCliTask(self._async_task, self)
        return self.task

    def kill_task(self) -> None:
        """Cancel running asyncio task and close CLI session (non-blocking)."""
        if self._async_task and not self._async_task.done():
            self._async_task.cancel()
        if self.task:
            self.task.kill()
        if self._is_persistent():
            return  # remote browser stays open; only the task was cancelled
        self._stop_chrome()  # kill per-task Chrome if one was launched
        # Close browser session — use to_thread to avoid blocking event loop
        sid = self.get_session_id()
        env = self._make_env()
        import subprocess
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                asyncio.to_thread(
                    subprocess.run,
                    ["playwright-cli", f"-s={sid}", "close"],
                    env=env,
                    capture_output=True,
                    timeout=10,
                )
            )
        except RuntimeError:
            # No running loop (e.g. called from __del__) — run synchronously
            try:
                subprocess.run(
                    ["playwright-cli", f"-s={sid}", "close"],
                    env=env,
                    capture_output=True,
                    timeout=10,
                )
            except Exception:
                pass

    async def get_response(self) -> str:
        """Await task completion and return result string.
        Note: BrowserAgent.execute() uses the PlaywrightCliTask.result() path instead.
        This method is retained as a convenience for direct usage.
        """
        if self._async_task is None:
            return "Error: PlaywrightCliBackend task was never started."
        try:
            await asyncio.wait_for(self._async_task, timeout=self.TASK_TIMEOUT)
        except asyncio.TimeoutError:
            self.kill_task()
            return f"Error: Playwright CLI task timed out after {self.TASK_TIMEOUT} seconds."
        except asyncio.CancelledError:
            return "Error: Playwright CLI task was cancelled."
        except Exception as e:
            return f"Error: Playwright CLI task failed: {e}"
        return self._result or "Task completed with no result."

    def get_log(self) -> list:
        """Return a snapshot of current progress log lines.

        Called by BrowserAgent.execute() via hasattr guard at lines 90 and 137
        to surface live progress updates to the Agent Zero UI.
        Returns a copy so callers cannot mutate internal state.
        """
        return list(self._log_lines)

    async def get_screenshot(self, path: str) -> "str | None":
        """Take a screenshot and save it to the given path.

        Called by BrowserAgent.get_update() via hasattr guard at line 143
        to capture and surface browser screenshots in the Agent Zero UI log.

        Args:
            path: Absolute path where the PNG screenshot should be saved.

        Returns:
            The path string on success, or None if the screenshot failed.
        """
        sid = self.get_session_id()
        try:
            await self._run_cmd([f"-s={sid}", "screenshot", f"--filename={path}"])
            if os.path.exists(path):
                log.debug("PlaywrightCliBackend.get_screenshot: saved to %s", path)
                return path
            log.warning("PlaywrightCliBackend.get_screenshot: file not found after command: %s", path)
            return None
        except Exception as e:
            log.warning("PlaywrightCliBackend.get_screenshot: failed: %s", e)
            return None

    def _artifact_path(self, subdir: str, ext: str) -> str:
        """Persistent output path for videos/annotated screenshots (never auto-deleted).

        Saves under the Agent Zero chat folder (same place BrowserAgent.get_update
        writes live screenshots) so files show up in the chat's file browser.
        Falls back to the system tempdir if the chat helpers aren't reachable
        (e.g. this backend used outside an Agent Zero context).
        """
        fname = f"{subdir}-{self.get_session_id()}-{time.strftime('%Y%m%d-%H%M%S')}.{ext}"
        try:
            from helpers import files as a0_files, persist_chat
            path = a0_files.get_abs_path(
                persist_chat.get_chat_folder_path(self.agent.context.id),
                "browser", subdir, fname,
            )
            a0_files.make_dirs(path)
            return path
        except Exception as e:
            log.debug("PlaywrightCliBackend._artifact_path: falling back to tempdir: %s", e)
            return os.path.join(tempfile.gettempdir(), fname)

    async def _resolve_bbox(self, sid: str, ann: dict) -> "tuple | None":
        """Return (x, y, width, height) in viewport px for an annotation.

        Prefers an element ref (looked up live via `eval` + getBoundingClientRect);
        falls back to explicit x/y fields (zero-size point) when no ref is given.
        """
        ref = ann.get("ref")
        if ref and _REF_PATTERN.match(str(ref)):
            try:
                # Plain object return (no JSON.stringify) — playwright-cli pretty-prints it
                # as real JSON under "### Result"; stringifying it ourselves would make the
                # CLI double-encode it (quoted, escaped) instead.
                out = await self._run_cmd([
                    f"-s={sid}", "eval",
                    "el => el.getBoundingClientRect()", str(ref),
                ])
                section = out.split("### Result", 1)[-1].split("### Ran Playwright code", 1)[0]
                match = re.search(r"\{.*\}", section, re.DOTALL)
                rect = json.loads(match.group(0)) if match else {}
                return (rect["x"], rect["y"], rect["width"], rect["height"])
            except Exception as e:
                log.warning("PlaywrightCliBackend: bbox lookup failed for %s: %s", ref, e)
                return None
        x, y = ann.get("x"), ann.get("y")
        if x is not None and y is not None:
            try:
                return (float(x), float(y), 0.0, 0.0)
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _draw_arrow(draw, x: float, y: float, w: float, h: float, color: tuple) -> None:
        """Draw an arrow pointing at the (x,y,w,h) box's center, tail offset diagonally."""
        import math
        cx, cy = x + w / 2, y + h / 2
        start = (cx - 70, cy - 70) if cx > 90 and cy > 90 else (cx + 70, cy + 70)
        end = (cx, cy)
        draw.line([start, end], fill=color, width=4)
        angle = math.atan2(end[1] - start[1], end[0] - start[0])
        for a in (angle - 0.4, angle + 0.4):
            draw.line(
                [end, (end[0] - 16 * math.cos(a), end[1] - 16 * math.sin(a))],
                fill=color, width=4,
            )

    async def _annotate_screenshot(self, sid: str, decision: dict) -> None:
        """Take a screenshot and draw boxes/arrows/text labels over it for repro walkthroughs.

        decision["annotations"]: list of
          {"ref": "e5", "type": "box"|"arrow"|"text", "text": "Click here"}
        (or "x"/"y" viewport px instead of "ref" for a free-floating point).
        Result is saved persistently (unlike the plain `screenshot` action) so the
        final answer can point the user at it.
        """
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            log.warning("PlaywrightCliBackend: Pillow not installed, cannot annotate screenshot")
            self._log_lines.append("  ⚠ annotate failed: Pillow not installed")
            return

        annotations = decision.get("annotations") or []
        if not annotations:
            log.warning("PlaywrightCliBackend: annotate action requires an 'annotations' list")
            return

        raw_path = os.path.join(tempfile.gettempdir(), f"pw-annotate-raw-{sid}.png")
        await self._run_cmd([f"-s={sid}", "screenshot", f"--filename={raw_path}"])
        if not os.path.exists(raw_path):
            return

        img = Image.open(raw_path).convert("RGB")
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", 16)
        except Exception:
            font = ImageFont.load_default()
        color = (235, 30, 30)

        for ann in annotations[:20]:  # cap — this is a walkthrough aid, not a report generator
            box = await self._resolve_bbox(sid, ann)
            if box is None:
                continue
            x, y, w, h = box
            kind = str(ann.get("type") or "box").lower()
            text = str(ann.get("text") or "")
            if kind == "arrow":
                self._draw_arrow(draw, x, y, w, h, color)
            elif kind != "text":  # "box" (default)
                draw.rectangle([x, y, x + w, y + h], outline=color, width=3)
            if text:
                label_xy = (x, max(0, y - 22))
                label_w = draw.textlength(text, font=font) + 8
                draw.rectangle(
                    [label_xy[0], label_xy[1], label_xy[0] + label_w, label_xy[1] + 20],
                    fill=color,
                )
                draw.text((label_xy[0] + 4, label_xy[1]), text, fill=(255, 255, 255), font=font)

        try:
            os.unlink(raw_path)
        except Exception:
            pass
        out_path = self._artifact_path("screenshots", "png")
        img.save(out_path)
        self._last_artifact = out_path
        self._log_lines.append(f"  ✓ annotated screenshot saved → {out_path}")

    # ── Snapshot helpers ──────────────────────────────────────────────────────

    async def _get_snapshot(self, sid: str) -> dict:
        """Take a structured snapshot, save to /tmp/pw-snap-<sid>.yml, parse YAML."""
        snap_path = os.path.join(tempfile.gettempdir(), f"pw-snap-{sid}.yml")
        try:
            await self._run_cmd([f"-s={sid}", "snapshot", f"--filename={snap_path}"])
        except RuntimeError as e:
            log.warning("PlaywrightCliBackend: snapshot command failed: %s", e)
            return {}
        if not os.path.exists(snap_path):
            log.warning("PlaywrightCliBackend: snapshot file not created at %s", snap_path)
            return {}
        try:
            import yaml
            with open(snap_path) as f:
                data = yaml.safe_load(f) or {}
            os.unlink(snap_path)  # clean up temp file
            return data
        except ImportError:
            # pyyaml not available — return raw text for LLM to interpret
            with open(snap_path) as f:
                raw = f.read()
            os.unlink(snap_path)
            return {"raw_snapshot": raw[:4000]}  # cap raw text
        except Exception as e:
            log.warning("PlaywrightCliBackend: snapshot parse error: %s", e)
            try:
                os.unlink(snap_path)
            except Exception:
                pass
            return {}

    def _truncate_snapshot(self, snapshot) -> object:
        """Limit elements to SNAPSHOT_MAX_ELEMENTS before serialization.

        playwright-cli snapshot YAML format is a **top-level list** of ARIA tree nodes:
            - generic [ref=e2]:
              - heading "Example Domain" [level=1] [ref=e3]
              - paragraph [ref=e4]: ...

        PyYAML parses this as a Python list. The previous implementation called
        dict(snapshot) which raises ValueError on list input — crashing every
        browser task after the first snapshot.

        Handles both formats:
        - list  (playwright-cli actual format): truncate top-level items directly
        - dict  (fallback for future format changes): check known element-list keys
        """
        # playwright-cli actual format: top-level list of ARIA tree nodes
        if isinstance(snapshot, list):
            if len(snapshot) > self.SNAPSHOT_MAX_ELEMENTS:
                omitted = len(snapshot) - self.SNAPSHOT_MAX_ELEMENTS
                truncated = snapshot[: self.SNAPSHOT_MAX_ELEMENTS]
                truncated.append(f"... {omitted} elements omitted")
                return truncated
            return snapshot

        # Fallback: dict with named element-list keys
        if isinstance(snapshot, dict):
            result = dict(snapshot)
            for key in ("elements", "nodes", "items", "children", "tree"):
                elements = result.get(key)
                if isinstance(elements, list) and len(elements) > self.SNAPSHOT_MAX_ELEMENTS:
                    result[key] = elements[: self.SNAPSHOT_MAX_ELEMENTS]
                    result["_truncated"] = (
                        f"{len(elements) - self.SNAPSHOT_MAX_ELEMENTS} elements omitted"
                    )
                    break
            return result

        # Unknown type — return as-is, byte cap in _build_prompt will protect context
        return snapshot

    # ── Main execution loop ───────────────────────────────────────────────────

    async def _run_task(self, task: str) -> None:
        """Core agentic loop: snapshot → LLM decision → action → repeat."""
        sid = self.get_session_id()
        history: list = []
        self._log_lines = []  # Reset log lines for this task run

        # Open browser session — `open` initializes the session (required before any other command)
        # If task contains a URL, open directly to it; otherwise open a blank session
        url_match = re.search(r"https?://\S+", task)
        try:
            initial_url = url_match.group(0) if url_match else "about:blank"
            reused = False
            if self._is_persistent() and await self._session_alive(sid):
                # Session from a previous task is still attached — reuse it
                try:
                    if initial_url != "about:blank":
                        await self._run_cmd([f"-s={sid}", "goto", initial_url])
                    else:
                        await self._run_cmd([f"-s={sid}", "tab-list"])  # liveness probe
                    reused = True
                    self._log_lines.append(f"Reusing open browser → {initial_url}")
                except RuntimeError:
                    # Stale session (browser gone) — drop it and start fresh
                    try:
                        await self._run_cmd([f"-s={sid}", "close"])
                    except Exception:
                        pass
            cdp = self._get_cdp_endpoint()
            if reused:
                pass
            elif not cdp and self._launch_per_task():
                # Launch a dedicated Chrome and attach to it via CDP
                cdp = await self._launch_chrome(sid, initial_url)
                await self._run_cmd([f"-s={sid}", "attach", f"--cdp={cdp}"])
                self._log_lines.append(f"Launched Chrome at {cdp} → {initial_url}")
            elif cdp:
                # Attach to an externally-running Chrome
                await self._run_cmd([f"-s={sid}", "attach", f"--cdp={cdp}"])
                if initial_url != "about:blank":
                    await self._run_cmd([f"-s={sid}", "goto", initial_url])
                self._log_lines.append(f"Attached to Chrome at {cdp} → {initial_url}")
            else:
                open_args = [f"-s={sid}", "open", initial_url]
                if self._is_headed():
                    open_args.append("--headed")
                await self._run_cmd(open_args)
                self._log_lines.append(f"Browser opened → {initial_url}")
        except RuntimeError as e:
            self._stop_chrome()  # don't leak a launched Chrome if attach failed
            self._result = f"Error opening browser session: {e}"
            self._log_lines.append(f"Error opening browser: {str(e)[:120]}")
            return

        # Pin the agent's working tab by CDP targetId so user tab activity can't drift
        # the agent onto the wrong tab. No-op unless we have an http CDP endpoint.
        self._cdp = cdp if cdp.startswith(("http://", "https://")) else ""
        await self._pin_current_tab(sid)

        for step in range(self.MAX_STEPS):
            # Keep the agent on its pinned tab if the user closed/switched tabs underneath.
            pin_note = await self._ensure_pinned_tab(sid)
            if pin_note and history:
                history[-1]["_notice"] = pin_note

            # Get structured snapshot with element refs (e1, e2, ...)
            snapshot = await self._get_snapshot(sid)
            truncated = self._truncate_snapshot(snapshot)

            # Build LLM prompt with snapshot + history
            prompt = self._build_prompt(task, truncated, history)

            # Call browser LLM — resolves model via _build_llm() priority chain:
            # 1. Plugin config (Settings > Playwright CLI > Browser Model)
            # 2. agent.get_chat_model() — always-available fallback
            try:
                from langchain_core.messages import HumanMessage, SystemMessage
                llm = self._build_llm()
                system_text = self._load_system_prompt()
                messages = []
                if system_text:
                    messages.append(SystemMessage(content=system_text))
                messages.append(HumanMessage(content=prompt))
                response = await llm.ainvoke(messages)
                decision = self._parse_decision(response.content)
            except Exception as e:
                log.warning("PlaywrightCliBackend: LLM call failed at step %d: %s", step, e)
                self._result = f"LLM error at step {step}: {e}"
                self._log_lines.append(f"Step {step + 1}: LLM error — {str(e)[:120]}")
                return

            history.append(decision)
            log.debug(
                "PlaywrightCliBackend step %d: action=%s ref=%s",
                step,
                decision.get("action"),
                decision.get("ref", ""),
            )

            # Log step progress for BrowserAgent UI display via get_log()
            _action = decision.get("action", "unknown")
            _ref = decision.get("ref", "")
            _val = str(decision.get("value", ""))
            _reason = decision.get("reasoning", "")
            _ref_part = f" {_ref}" if _ref else ""
            _val_short = (_val[:60] + "\u2026") if len(_val) > 60 else _val
            _val_part = f": {_val_short}" if _val else ""
            self._log_lines.append(f"Step {step + 1}: {_action}{_ref_part}{_val_part}")
            if _reason:
                self._log_lines.append(f"  \u21b3 {_reason[:100]}")

            # Check completion
            if decision.get("done") or decision.get("action") == "done":
                self._result = f"Task complete.\n{decision.get('value', '')}"
                self._log_lines.append("\u2713 Task complete")
                break

            # Execute action
            try:
                self._last_artifact = None
                self._last_tabs = None
                await self._execute_action(sid, decision)
                if self._last_artifact:
                    # Surface saved video/annotated-screenshot path in next prompt's
                    # history so the LLM can cite it when it calls `done`.
                    history[-1]["_artifact"] = self._last_artifact
                if self._last_tabs:
                    # Surface the open-tab list (incl. user-opened tabs) so the LLM can
                    # see what exists and pick one via tab-select. history is JSON-dumped
                    # into the next prompt, so this field reaches the model automatically.
                    history[-1]["_tabs"] = self._last_tabs
                    # The agent intentionally changed tabs — re-pin so _ensure_pinned_tab
                    # tracks its new intended tab instead of dragging it back to the old one.
                    if decision.get("action") in ("tab-new", "tab-select", "tab-close"):
                        await self._pin_current_tab(sid)
            except RuntimeError as e:
                log.warning("PlaywrightCliBackend: action failed at step %d: %s", step, e)
                # Don't abort — let LLM adapt on next snapshot
                history[-1]["_error"] = str(e)
                self._log_lines.append(f"  \u26a0 action error: {str(e)[:100]}")
        else:
            self._result = "Max steps reached without completing task."
            self._log_lines.append(f"Max steps ({self.MAX_STEPS}) reached — task incomplete")

        # Clean up session — persistent (remote) browsers stay open for the next task
        if self._is_persistent():
            self._log_lines.append("Browser left open (persistent mode)")
        else:
            try:
                await self._run_cmd([f"-s={sid}", "close"])
            except Exception:
                pass  # Best-effort close

    # ── Decision parsing ──────────────────────────────────────────────────────

    def _parse_decision(self, content: str) -> dict:
        """Parse LLM JSON response. Falls back to regex extraction, then done."""
        # Try direct JSON parse
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass
        # Try extracting JSON object from prose
        match = re.search(r"\{[^{}]*\}", content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass
        # Fallback: treat entire response as final answer
        return {"action": "done", "value": content, "done": True}

    # ── Action executor ───────────────────────────────────────────────────────

    async def _execute_action(self, sid: str, decision: dict) -> None:
        """Dispatch action to playwright-cli.

        Security:
        - goto: URL scheme allowlist (http/https only) prevents file://, javascript:, etc.
        - click/fill ref: must match ^e\\d+$ pattern to prevent flag injection (--arg style).
        Raises RuntimeError on CLI failure.
        """
        action = decision.get("action", "")
        ref = decision.get("ref", "")
        value = decision.get("value", "")

        if action == "goto":
            # URL scheme validation — only allow http/https
            if not any(str(value).startswith(s) for s in _URL_ALLOWED_SCHEMES):
                log.warning(
                    "PlaywrightCliBackend: goto rejected non-http URL: %s",
                    str(value)[:100],
                )
                return
            await self._run_cmd([f"-s={sid}", "goto", value])

        elif action == "click":
            # Ref must match e\d+ to prevent flag injection
            if not ref or not _REF_PATTERN.match(str(ref)):
                log.warning("PlaywrightCliBackend: click rejected invalid ref '%s'", ref)
                return
            await self._run_cmd([f"-s={sid}", "click", ref])

        elif action == "fill":
            if not ref or not _REF_PATTERN.match(str(ref)):
                log.warning("PlaywrightCliBackend: fill rejected invalid ref '%s'", ref)
                return
            await self._run_cmd([f"-s={sid}", "fill", ref, value])

        elif action == "dblclick":
            if not ref or not _REF_PATTERN.match(str(ref)):
                log.warning("PlaywrightCliBackend: dblclick rejected invalid ref '%s'", ref)
                return
            await self._run_cmd([f"-s={sid}", "dblclick", ref])

        elif action == "type":
            await self._run_cmd([f"-s={sid}", "type", str(value)])

        elif action == "press":
            if not value:
                log.warning("PlaywrightCliBackend: press action missing value")
                return
            await self._run_cmd([f"-s={sid}", "press", str(value)])

        elif action == "select":
            if not ref or not _REF_PATTERN.match(str(ref)):
                log.warning("PlaywrightCliBackend: select rejected invalid ref '%s'", ref)
                return
            await self._run_cmd([f"-s={sid}", "select", ref, str(value)])

        elif action == "check":
            if not ref or not _REF_PATTERN.match(str(ref)):
                log.warning("PlaywrightCliBackend: check rejected invalid ref '%s'", ref)
                return
            await self._run_cmd([f"-s={sid}", "check", ref])

        elif action == "uncheck":
            if not ref or not _REF_PATTERN.match(str(ref)):
                log.warning("PlaywrightCliBackend: uncheck rejected invalid ref '%s'", ref)
                return
            await self._run_cmd([f"-s={sid}", "uncheck", ref])

        elif action == "hover":
            if not ref or not _REF_PATTERN.match(str(ref)):
                log.warning("PlaywrightCliBackend: hover rejected invalid ref '%s'", ref)
                return
            await self._run_cmd([f"-s={sid}", "hover", ref])

        elif action == "go-back":
            await self._run_cmd([f"-s={sid}", "go-back"])

        elif action == "go-forward":
            await self._run_cmd([f"-s={sid}", "go-forward"])

        elif action == "reload":
            await self._run_cmd([f"-s={sid}", "reload"])

        elif action == "snapshot":
            # Explicit snapshot request — loop will call _get_snapshot on next iteration
            pass

        elif action == "tab-new":
            if value and any(str(value).startswith(s) for s in _URL_ALLOWED_SCHEMES):
                self._last_tabs = await self._run_cmd([f"-s={sid}", "tab-new", str(value)])
            else:
                self._last_tabs = await self._run_cmd([f"-s={sid}", "tab-new"])

        elif action == "tab-close":
            self._last_tabs = await self._run_cmd([f"-s={sid}", "tab-close"])

        elif action == "screenshot":
            snap_path = os.path.join(tempfile.gettempdir(), f"pw-shot-{sid}.png")
            await self._run_cmd([f"-s={sid}", "screenshot", f"--filename={snap_path}"])
            log.info("PlaywrightCliBackend: screenshot saved to %s", snap_path)
            try:
                if os.path.exists(snap_path):
                    os.unlink(snap_path)
            except Exception:
                pass

        elif action == "scroll" or action == "mousewheel":
            # scroll: value = dy (pixels), ref = optional element to scroll within
            # e.g. {action: scroll, value: 300}  or  {action: scroll, ref: e5, value: 300}
            try:
                dx = int(decision.get("dx", 0))
                dy = int(decision.get("value") or decision.get("dy") or 100)
            except (TypeError, ValueError):
                dy = 100
                dx = 0
            await self._run_cmd([f"-s={sid}", "mousewheel", "--", str(dx), str(dy)])

        elif action == "eval":
            # eval: value = JS expression, ref = optional element ref
            expr = str(value) if value else "document.title"
            if ref and _REF_PATTERN.match(str(ref)):
                await self._run_cmd([f"-s={sid}", "eval", expr, ref])
            else:
                await self._run_cmd([f"-s={sid}", "eval", expr])

        elif action == "drag":
            # drag: ref = source element, value = target element ref
            target_ref = str(decision.get("target") or value or "")
            if not ref or not _REF_PATTERN.match(str(ref)):
                log.warning("PlaywrightCliBackend: drag rejected invalid source ref '%s'", ref)
                return
            if not target_ref or not _REF_PATTERN.match(target_ref):
                log.warning("PlaywrightCliBackend: drag rejected invalid target ref '%s'", target_ref)
                return
            await self._run_cmd([f"-s={sid}", "drag", ref, target_ref])

        elif action == "tab-select":
            # tab-select: value = tab index (0-based integer)
            try:
                idx = int(value)
            except (TypeError, ValueError):
                log.warning("PlaywrightCliBackend: tab-select requires integer value, got '%s'", value)
                return
            self._last_tabs = await self._run_cmd([f"-s={sid}", "tab-select", str(idx)])

        elif action == "tab-list":
            # tab-list: open-tab listing (index, current-marker, title, URL) — captured
            # into _last_tabs so the loop can feed it back to the LLM. This is the only
            # way the agent learns about tabs the *user* opened, since _get_snapshot only
            # captures the current tab. Format: "- 0: (current) [Title](url)"
            self._last_tabs = await self._run_cmd([f"-s={sid}", "tab-list"])

        elif action == "keydown":
            # keydown: value = key name (Shift, Control, Alt, Meta, etc.)
            if not value:
                log.warning("PlaywrightCliBackend: keydown action missing value")
                return
            await self._run_cmd([f"-s={sid}", "keydown", str(value)])

        elif action == "keyup":
            # keyup: value = key name
            if not value:
                log.warning("PlaywrightCliBackend: keyup action missing value")
                return
            await self._run_cmd([f"-s={sid}", "keyup", str(value)])

        elif action == "dialog-accept":
            # dialog-accept: value = optional confirmation text
            if value:
                await self._run_cmd([f"-s={sid}", "dialog-accept", str(value)])
            else:
                await self._run_cmd([f"-s={sid}", "dialog-accept"])

        elif action == "dialog-dismiss":
            await self._run_cmd([f"-s={sid}", "dialog-dismiss"])

        elif action == "resize":
            # resize: value = "width height" or use separate width/height keys
            try:
                width = int(decision.get("width") or str(value).split()[0])
                height = int(decision.get("height") or str(value).split()[1])
            except (TypeError, ValueError, IndexError):
                log.warning("PlaywrightCliBackend: resize requires width and height, got '%s'", value)
                return
            await self._run_cmd([f"-s={sid}", "resize", str(width), str(height)])

        elif action == "wait":
            # wait: value = seconds to sleep (max 30 to prevent stalling)
            try:
                seconds = min(float(value), 30.0)
            except (TypeError, ValueError):
                seconds = 2.0
            log.debug("PlaywrightCliBackend: wait %.1fs", seconds)
            await asyncio.sleep(seconds)

        elif action == "mousemove":
            # mousemove: value = "x y" or use separate x/y keys
            try:
                x = int(decision.get("x") or str(value).split()[0])
                y = int(decision.get("y") or str(value).split()[1])
            except (TypeError, ValueError, IndexError):
                log.warning("PlaywrightCliBackend: mousemove requires x and y, got '%s'", value)
                return
            await self._run_cmd([f"-s={sid}", "mousemove", str(x), str(y)])

        elif action == "mousedown":
            # mousedown: value = optional button (left/right/middle, default left)
            btn = str(value) if value in ("right", "middle") else None
            if btn:
                await self._run_cmd([f"-s={sid}", "mousedown", btn])
            else:
                await self._run_cmd([f"-s={sid}", "mousedown"])

        elif action == "mouseup":
            btn = str(value) if value in ("right", "middle") else None
            if btn:
                await self._run_cmd([f"-s={sid}", "mouseup", btn])
            else:
                await self._run_cmd([f"-s={sid}", "mouseup"])

        elif action == "upload":
            # upload: ref = file input element ref, value = file path
            if not ref or not _REF_PATTERN.match(str(ref)):
                log.warning("PlaywrightCliBackend: upload rejected invalid ref '%s'", ref)
                return
            if not value:
                log.warning("PlaywrightCliBackend: upload requires a file path in value")
                return
            await self._run_cmd([f"-s={sid}", "upload", ref, str(value)])

        elif action == "run-code":
            # run-code: value = inline JS string with signature async page => { ... }
            # WARNING: inherits prompt injection risk from task string.
            if not value:
                log.warning("PlaywrightCliBackend: run-code requires JS expression in value")
                return
            await self._run_cmd([f"-s={sid}", "run-code", str(value)])

        elif action == "video-start":
            # playwright-cli takes the filename on video-start, not video-stop.
            out_path = self._artifact_path("videos", "webm")
            await self._run_cmd([f"-s={sid}", "video-start", out_path])
            self._video_recording = True
            self._video_path = out_path
            self._log_lines.append("  ● recording started")

        elif action == "video-stop":
            await self._run_cmd([f"-s={sid}", "video-stop"])
            self._video_recording = False
            out_path = self._video_path
            self._video_path = None
            if out_path and os.path.exists(out_path):
                self._last_artifact = out_path
                self._log_lines.append(f"  ■ recording saved → {out_path}")

        elif action == "annotate":
            await self._annotate_screenshot(sid, decision)

        else:
            log.warning("PlaywrightCliBackend: unknown action '%s' — skipping", action)

    # ── Prompt builder ────────────────────────────────────────────────────────

    def _load_system_prompt(self) -> str:
        """Load browser_agent.system.md from plugin prompts directory.

        Returns empty string if file not found (fallback: human-only message).
        Loaded fresh on each call — no caching — so hot-reload and agent-profile
        overrides work without restarting Agent Zero.
        """
        path = os.path.join(_PLUGIN_ROOT, "prompts", "browser_agent.system.md")
        try:
            return open(path, encoding="utf-8").read()
        except Exception as e:
            log.warning(
                "PlaywrightCliBackend: could not load system prompt from '%s': %s", path, e
            )
            return ""

    def _build_prompt(self, task: str, snapshot: dict, history: list) -> str:
        """Build human-turn LLM message: task + current snapshot + recent action history.

        System instructions are loaded separately from browser_agent.system.md
        and passed as a SystemMessage. This method carries only situational context.

        Security note: task string comes from the parent agent after secrets masking.
        It is embedded directly in the prompt — inherent prompt injection relay risk.
        Operators should restrict task content to trusted inputs.
        """
        # Safe serialization — snapshot already truncated at dict level
        snap_json = json.dumps(snapshot, indent=2)
        # Cap total snapshot bytes to prevent LLM context overflow
        # (deeply nested structures or long attribute values can exceed cap after element truncation)
        if len(snap_json) > self.SNAPSHOT_MAX_BYTES:
            snap_json = snap_json[: self.SNAPSHOT_MAX_BYTES] + "\n... (snapshot truncated at byte limit)"
        # Only last 5 history entries to avoid prompt bloat
        hist_json = json.dumps(history[-5:], indent=2)
        return (
            f"## Current Task\n{task}\n\n"
            f"## Page Snapshot\n"
            f"(Use element refs e1, e2, ... as targets for click/fill/hover/etc.)\n"
            f"{snap_json}\n\n"
            f"## Action History (last 5 steps)\n"
            f"{hist_json}\n\n"
            "Respond with a single JSON object — no prose, no markdown fences."
        )
