### browser_agent (playwright-cli):

Use `browser_agent` for any task involving a real browser: navigating sites,
clicking, filling forms, logging in, scraping content, taking screenshots.
Give it a natural-language `message` describing the goal; it runs its own
snapshot → act → repeat loop and returns the result. Set `reset="true"` to
start a fresh session, or omit it to continue the existing one.

```
browser_agent  message="open https://example.com, log in as test@x.com, and read the dashboard total"
```

**Parallel windows:** to drive several independent browser windows at once,
pass a `window` argument (any short name). Each distinct name gets its own
persistent window and session; reuse the same name to continue in that window.
```
browser_agent  window="research"  message="open https://news.example.com and summarize"
browser_agent  window="checkout"  message="open https://shop.example.com and add item X to cart"
```
Note: separate windows are truly independent only when the plugin launches a
dedicated Chrome per window (`browser_launch_chrome`). In `browser_cdp_endpoint`
mode every window attaches to the same Chrome and shares its tabs.

**Advanced — driving playwright-cli by hand:** for fine-grained control (custom
Chrome flags, an intercepting proxy for pentesting, PwnFox tagging) you can load
the **playwright-cli** skill and run its commands directly via `code_execution_tool`:
```
skills_tool:load playwright-cli
```
The plugin's proxy/cert/PwnFox settings live in its config — never hardcode them.
Read them with `python <plugin_root>/config.py`, launch Chrome with the matching
flags (`--proxy-server`, `--ignore-certificate-errors`, `--remote-debugging-port`),
then `playwright-cli attach --cdp=`. The skill has the full recipe under
"Remote / proxied browser".
