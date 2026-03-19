### browser_agent (playwright-cli):

> **Important:** For all browser and web tasks, load and use the **playwright-cli** skill via `code_execution_tool` (terminal runtime) — do NOT call `browser_agent` directly.

The `playwright-cli` skill is available in your skill list. Load it first:
```
skills_tool:load playwright-cli
```
Then use `playwright-cli` commands via `code_execution_tool` terminal to interact with the browser.

**Use playwright-cli for:**
- Navigating websites and web pages
- Clicking, filling forms, submitting
- Extracting content, data scraping
- Taking screenshots
- Login and authenticated sessions
- Any task involving a real browser

**Example workflow:**
```bash
playwright-cli open https://example.com
playwright-cli snapshot
playwright-cli click e3
playwright-cli close
```
