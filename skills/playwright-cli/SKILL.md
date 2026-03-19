---
name: playwright-cli
description: Use for any task that involves working with a website or web page — including browsing, navigating, reading content, extracting data, filling forms, clicking buttons, logging in, scraping, checking page structure, testing web apps, taking screenshots, or any other browser-based interaction.
allowed-tools: Bash(playwright-cli:*)
---

# Browser Automation with playwright-cli

## Quick start

```bash
# open new browser
playwright-cli open
# navigate to a page
playwright-cli goto https://playwright.dev
# interact with the page using refs from the snapshot
playwright-cli click e15
playwright-cli type "page.click"
playwright-cli press Enter
# take a screenshot (rarely used, as snapshot is more common)
playwright-cli screenshot
# close the browser
playwright-cli close
```

## Commands

### Core

```bash
playwright-cli open
# open and navigate right away
playwright-cli open https://example.com/
playwright-cli goto https://playwright.dev
playwright-cli type "search query"
playwright-cli click e3
playwright-cli dblclick e7
playwright-cli fill e5 "user@example.com"
playwright-cli drag e2 e8
playwright-cli hover e4
playwright-cli select e9 "option-value"
playwright-cli upload ./document.pdf
playwright-cli check e12
playwright-cli uncheck e12
playwright-cli snapshot
playwright-cli snapshot --filename=after-click.yaml
playwright-cli eval "document.title"
playwright-cli eval "el => el.textContent" e5
playwright-cli dialog-accept
playwright-cli dialog-accept "confirmation text"
playwright-cli dialog-dismiss
playwright-cli resize 1920 1080
playwright-cli close
```

### Navigation

```bash
playwright-cli go-back
playwright-cli go-forward
playwright-cli reload
```

### Keyboard

```bash
playwright-cli press Enter
playwright-cli press ArrowDown
playwright-cli keydown Shift
playwright-cli keyup Shift
```

### Mouse

```bash
playwright-cli mousemove 150 300
playwright-cli mousedown
playwright-cli mousedown right
playwright-cli mouseup
playwright-cli mouseup right
playwright-cli mousewheel 0 100
```

### Save as

```bash
playwright-cli screenshot
playwright-cli screenshot e5
playwright-cli screenshot --filename=page.png
playwright-cli pdf --filename=page.pdf
```

### Tabs

```bash
playwright-cli tab-list
playwright-cli tab-new
playwright-cli tab-new https://example.com/page
playwright-cli tab-close
playwright-cli tab-close 2
playwright-cli tab-select 0
```

### Storage

```bash
playwright-cli state-save
playwright-cli state-save auth.json
playwright-cli state-load auth.json

# Cookies
playwright-cli cookie-list
playwright-cli cookie-list --domain=example.com
playwright-cli cookie-get session_id
playwright-cli cookie-set session_id abc123
playwright-cli cookie-set session_id abc123 --domain=example.com --httpOnly --secure
playwright-cli cookie-delete session_id
playwright-cli cookie-clear

# LocalStorage
playwright-cli localstorage-list
playwright-cli localstorage-get theme
playwright-cli localstorage-set theme dark
playwright-cli localstorage-delete theme
playwright-cli localstorage-clear

# SessionStorage
playwright-cli sessionstorage-list
playwright-cli sessionstorage-get step
playwright-cli sessionstorage-set step 3
playwright-cli sessionstorage-delete step
playwright-cli sessionstorage-clear
```

### Network

```bash
playwright-cli route "**/*.jpg" --status=404
playwright-cli route "https://api.example.com/**" --body='{"mock": true}'
playwright-cli route-list
playwright-cli unroute "**/*.jpg"
playwright-cli unroute
```

### DevTools

```bash
playwright-cli console
playwright-cli console warning
playwright-cli network
playwright-cli run-code "async page => await page.context().grantPermissions(['geolocation'])"
playwright-cli tracing-start
playwright-cli tracing-stop
playwright-cli video-start
playwright-cli video-stop video.webm
```

## Open parameters
```bash
# Use specific browser when creating session
playwright-cli open --browser=chrome
playwright-cli open --browser=firefox
playwright-cli open --browser=webkit
playwright-cli open --browser=msedge
# Connect to browser via extension
playwright-cli open --extension

# Use persistent profile (by default profile is in-memory)
playwright-cli open --persistent
# Use persistent profile with custom directory
playwright-cli open --profile=/path/to/profile

# Start with config file
playwright-cli open --config=my-config.json

# Close the browser
playwright-cli close
# Delete user data for the default session
playwright-cli delete-data
```

## Snapshots

After each command, playwright-cli provides a snapshot of the current browser state.

```bash
> playwright-cli goto https://example.com
### Page
- Page URL: https://example.com/
- Page Title: Example Domain
### Snapshot
[Snapshot](.playwright-cli/page-2026-02-14T19-22-42-679Z.yml)
```

You can also take a snapshot on demand using `playwright-cli snapshot` command.

If `--filename` is not provided, a new snapshot file is created with a timestamp. Default to automatic file naming, use `--filename=` when artifact is a part of the workflow result.

## Browser Sessions

```bash
# create new browser session named "mysession" with persistent profile
playwright-cli -s=mysession open example.com --persistent
# same with manually specified profile directory (use when requested explicitly)
playwright-cli -s=mysession open example.com --profile=/path/to/profile
playwright-cli -s=mysession click e6
playwright-cli -s=mysession close  # stop a named browser
playwright-cli -s=mysession delete-data  # delete user data for persistent session

playwright-cli list
# Close all browsers
playwright-cli close-all
# Forcefully kill all browser processes
playwright-cli kill-all
```

## Local installation

In some cases user might want to install playwright-cli locally. If running globally available `playwright-cli` binary fails, use `npx playwright-cli` to run the commands. For example:

```bash
npx playwright-cli open https://example.com
npx playwright-cli click e1
```

## Example: Form submission

```bash
playwright-cli open https://example.com/form
playwright-cli snapshot

playwright-cli fill e1 "user@example.com"
playwright-cli fill e2 "password123"
playwright-cli click e3
playwright-cli snapshot
playwright-cli close
```

## Example: Multi-tab workflow

```bash
playwright-cli open https://example.com
playwright-cli tab-new https://example.com/other
playwright-cli tab-list
playwright-cli tab-select 0
playwright-cli snapshot
playwright-cli close
```

## Example: Debugging with DevTools

```bash
playwright-cli open https://example.com
playwright-cli click e4
playwright-cli fill e7 "test"
playwright-cli console
playwright-cli network
playwright-cli close
```

```bash
playwright-cli open https://example.com
playwright-cli tracing-start
playwright-cli click e4
playwright-cli fill e7 "test"
playwright-cli tracing-stop
playwright-cli close
```

## Data Extraction

For extracting structured data from pages, prefer **snapshot** (gives full DOM) or **eval** for specific queries.

```bash
# Get page title
playwright-cli eval "document.title"

# Get text content of an element by ref
playwright-cli eval "el => el.textContent" e5

# Get an attribute value
playwright-cli eval "el => el.getAttribute('href')" e3

# Get all links on the page
playwright-cli eval "JSON.stringify(Array.from(document.querySelectorAll('a')).map(a=>({text:a.textContent.trim(),href:a.href})))"

# Get all meta tags
playwright-cli eval "JSON.stringify(Array.from(document.querySelectorAll('meta')).map(m=>({name:m.name||m.property,content:m.content})).filter(m=>m.name))"

# Get JSON-LD schema data (structured data)
playwright-cli eval "JSON.stringify(Array.from(document.querySelectorAll('script[type=\"application/ld+json\"]')).map(s=>JSON.parse(s.textContent)))"

# Get all headings
playwright-cli eval "JSON.stringify(Array.from(document.querySelectorAll('h1,h2,h3')).map(h=>({tag:h.tagName,text:h.textContent.trim()})))"

# Get input values
playwright-cli eval "JSON.stringify(Array.from(document.querySelectorAll('input,select,textarea')).map(i=>({name:i.name,value:i.value,type:i.type})))"
```

### When to use eval vs run-code

| Situation | Approach |
|---|---|
| Simple query (title, one selector, one attribute) | `playwright-cli eval "..."` |
| Complex JS (wait, interact, multi-step logic) | `playwright-cli run-code "async page => { ... }"` |
| eval failing due to shell escaping in terminal | Python subprocess with `playwright-cli eval` |

### run-code — inline JS string

`run-code` takes an **inline JavaScript string** with signature `async page => { ... }`. Do NOT pass a file path.

```bash
# Grant permissions and set geolocation
playwright-cli run-code "async page => {
  await page.context().grantPermissions(['geolocation']);
  await page.context().setGeolocation({ latitude: 37.7749, longitude: -122.4194 });
}"

# Extract JSON-LD schema data
playwright-cli run-code "async page => {
  await page.waitForLoadState('networkidle');
  const schemas = await page.evaluate(() =>
    Array.from(document.querySelectorAll('script[type=\"application/ld+json\"]'))
      .map(s => { try { return JSON.parse(s.textContent); } catch(e) { return {error: e.message}; } })
  );
  console.log(JSON.stringify(schemas, null, 2));
}"

# Wait for a specific element before extracting
playwright-cli run-code "async page => {
  await page.waitForSelector('.product-price', { timeout: 5000 });
  const prices = await page.evaluate(() =>
    Array.from(document.querySelectorAll('.product-price')).map(el => el.textContent.trim())
  );
  console.log(JSON.stringify(prices));
}"
```

> ⚠️ **Common mistake**: `playwright-cli run-code /tmp/script.js` will fail with `SyntaxError: Invalid regular expression flags` because the path is parsed as JavaScript. Always pass an inline string.

### Python subprocess — for complex multi-page tasks

When `run-code` inline strings get unwieldy with escaping, use Python subprocess:

~~~python
import subprocess, json

cwd = '/path/to/working/dir'  # playwright-cli reads session from cwd

def pcli(*args):
    r = subprocess.run(['playwright-cli'] + list(args), capture_output=True, text=True, cwd=cwd)
    return r.stdout, r.stderr

def peval(js):
    out, _ = pcli('eval', js)
    lines = out.split('\n')
    result_lines = []
    in_result = False
    for line in lines:
        if '### Result' in line:
            in_result = True
            continue
        if in_result and line.strip().startswith('###'):
            break
        if in_result:
            result_lines.append(line)
    raw = '\n'.join(result_lines).strip()
    try:
        return json.loads(json.loads(raw))  # double-parse: result is JSON-encoded string
    except:
        try: return json.loads(raw)
        except: return raw

# Open browser ONCE — reuse session for all pages
pcli('open', 'https://example.com')
schemas = peval('JSON.stringify(Array.from(document.querySelectorAll(\'script[type="application/ld+json"]\')).map(function(s){try{return JSON.parse(s.textContent)}catch(e){return{error:e.message}}}))')
print(json.dumps(schemas, indent=2))

# Navigate to another page — use goto, NOT open (avoids bot detection)
pcli('goto', 'https://example.com/products')
pcli('close')
~~~

> ⚠️ **Bot detection**: Some sites (Cloudflare WAF etc.) block headless browsers. Open the browser **once** and use `goto` for navigation between pages. Re-opening with `open` per page triggers bot detection. If you get 403 responses, the site has bot protection — try `curl` for static content instead.

## Specific tasks

* **Request mocking** [references/request-mocking.md](references/request-mocking.md)
* **Running Playwright code** [references/running-code.md](references/running-code.md)
* **Browser session management** [references/session-management.md](references/session-management.md)
* **Storage state (cookies, localStorage)** [references/storage-state.md](references/storage-state.md)
* **Test generation** [references/test-generation.md](references/test-generation.md)
* **Tracing** [references/tracing.md](references/tracing.md)
* **Video recording** [references/video-recording.md](references/video-recording.md)
