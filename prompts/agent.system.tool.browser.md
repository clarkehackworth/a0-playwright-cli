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

**Always act via `browser_agent`.** To browse — including CDP-attach, proxied,
or pentest sessions — call `browser_agent`. It reads the plugin config (CDP
endpoint, proxy, cert, PwnFox) and attaches automatically; you do not launch or
configure Chrome yourself. **Never** load the `playwright-cli` skill to carry out
a browsing task — the skill is a passive reference, so loading it does nothing and
leaves the task unstarted.

**Advanced (rare, operator-only):** the `playwright-cli` skill exists solely for a
human operator debugging launch flags by hand. Do not load it in response to a
user's browse request — use `browser_agent`.
