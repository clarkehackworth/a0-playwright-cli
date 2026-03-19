"""browser_agent Tool — Playwright CLI browser automation for Agent Zero.

Proxies all browser tasks to PlaywrightCliBackend, which runs playwright-cli
shell commands, takes YAML DOM snapshots, and feeds structured element refs
(e1/e2/...) back to the Agent Zero LLM loop.
"""
import os
import sys

# ---------------------------------------------------------------------------
# Plugin root on sys.path — required so helpers.* inside this plugin resolve
# ---------------------------------------------------------------------------
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

# Also ensure /a0 is on sys.path for python.helpers.* imports
_A0_ROOT = os.path.abspath(os.path.join(_PLUGIN_ROOT, "..", "..", ".."))
if _A0_ROOT not in sys.path:
    sys.path.insert(0, _A0_ROOT)

import asyncio
import time

from helpers.tool import Tool, Response
from helpers import files, persist_chat
from helpers.print_style import PrintStyle
from helpers.secrets import get_secrets_manager


class BrowserAgent(Tool):

    async def execute(self, message="", reset="", **kwargs):
        self.guid = self.agent.context.generate_id()
        reset = str(reset).lower().strip() == "true"
        await self.prepare_state(reset=reset)
        message = get_secrets_manager(self.agent.context).mask_values(
            message, placeholder="<secret>{key}</secret>"
        )
        task = self.state.start_task(message) if self.state else None

        timeout_seconds = 300  # 5 minute timeout
        start_time = time.time()

        fail_counter = 0
        while not task.is_ready() if task else False:
            if time.time() - start_time > timeout_seconds:
                PrintStyle().warning(
                    self._mask(f"Browser agent task timeout after {timeout_seconds}s, forcing completion")
                )
                break

            await self.agent.handle_intervention()
            await asyncio.sleep(1)
            try:
                if task and task.is_ready():
                    break
                try:
                    update = await asyncio.wait_for(self.get_update(), timeout=10)
                    fail_counter = 0
                except asyncio.TimeoutError:
                    fail_counter += 1
                    PrintStyle().warning(
                        self._mask(f"browser_agent.get_update timed out ({fail_counter}/3)")
                    )
                    if fail_counter >= 3:
                        PrintStyle().warning(
                            self._mask("3 consecutive timeouts — breaking loop")
                        )
                        break
                    continue
                update_log = update.get("log", [])
                self.update_progress("\n".join(update_log))
                screenshot = update.get("screenshot", None)
                if screenshot:
                    self.log.update(screenshot=screenshot)
            except Exception as e:
                PrintStyle().error(self._mask(f"Error getting update: {str(e)}"))

        if task and not task.is_ready():
            PrintStyle().warning(self._mask("Task timed out — killing"))
            self.state.kill_task() if self.state else None
            return Response(
                message=self._mask("Browser agent task timed out, no output provided."),
                break_loop=False,
            )

        # final progress update
        if self.state:
            log_final = self.state.get_log() if hasattr(self.state, "get_log") else []
            self.update_progress("\n".join(log_final))

        try:
            result = await task.result() if task else None
        except Exception as e:
            PrintStyle().error(self._mask(f"Error getting task result: {str(e)}"))
            answer_text = self._mask(f"Browser agent task failed: {str(e)}")
            self.log.update(answer=answer_text)
            return Response(message=answer_text, break_loop=False)

        # Parse result from PlaywrightCliBackend
        # PlaywrightCliResult.final_result() always returns a string
        if result:
            answer_text = result.final_result() if hasattr(result, 'final_result') else str(result)
        else:
            answer_text = "Task completed but no result returned."

        answer_text = self._mask(answer_text)
        self.log.update(answer=answer_text)

        if (
            self.log.kvps
            and "screenshot" in self.log.kvps
            and self.log.kvps["screenshot"]
        ):
            path = self.log.kvps["screenshot"].split("//", 1)[-1].split("&", 1)[0]
            answer_text += f"\n\nScreenshot: {path}"

        return Response(message=answer_text, break_loop=False)

    def get_log_object(self):
        return self.agent.context.log.log(
            type="browser",
            heading=f"icon://captive_portal {self.agent.agent_name}: Calling Browser Agent",
            content="",
            kvps=self.args,
        )

    async def get_update(self):
        await self.prepare_state()
        result = {}
        agent = self.agent

        if self.state:
            try:
                async def _get_update():
                    result["log"] = self.state.get_log() if hasattr(self.state, "get_log") else []
                    path = files.get_abs_path(
                        persist_chat.get_chat_folder_path(agent.context.id),
                        "browser", "screenshots", f"{self.guid}.png",
                    )
                    files.make_dirs(path)
                    if hasattr(self.state, "get_screenshot"):
                        screenshot_path = await self.state.get_screenshot(path)
                        if screenshot_path:
                            result["screenshot"] = f"img://{screenshot_path}&t={str(time.time())}"

                if self.state and self.state.task and not self.state.task.is_ready():
                    await self.state.task.execute_inside(_get_update)
            except Exception:
                pass

        return result

    async def prepare_state(self, reset=False):
        """Initialize PlaywrightCliBackend — the sole backend since v2.1.0."""
        state_key = "_browser_agent_state_playwright_cli"
        self.state = self.agent.get_data(state_key)

        if reset and self.state:
            self.state.kill_task()
        if not self.state or reset:
            import importlib.util as _ilu
            _mod_name = "playwright_cli_backend"
            if _mod_name not in sys.modules:
                _spec = _ilu.spec_from_file_location(
                    _mod_name,
                    os.path.join(_PLUGIN_ROOT, "helpers", "playwright_cli_backend.py")
                )
                _mod = _ilu.module_from_spec(_spec)
                sys.modules[_mod_name] = _mod
                _spec.loader.exec_module(_mod)
            else:
                _mod = sys.modules[_mod_name]
            self.state = _mod.PlaywrightCliBackend(self.agent)
        self.agent.set_data(state_key, self.state)

    def update_progress(self, text):
        text = self._mask(text)
        short = text.split("\n")[-1]
        if len(short) > 50:
            short = short[:50] + "..."
        self.log.update(progress=text)
        self.agent.context.log.set_progress(f"Browser: {short}")

    def _mask(self, text: str) -> str:
        try:
            return get_secrets_manager(self.agent.context).mask_values(text or "")
        except Exception:
            return text or ""
