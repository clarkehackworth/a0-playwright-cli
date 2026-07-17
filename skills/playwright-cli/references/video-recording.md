# Video Recording

Capture browser automation sessions as video for debugging, documentation, or verification. Produces WebM (VP8/VP9 codec).

Recording attaches to the already-open browser via CDP screencast — no restart or fresh session needed. Pages already open when you call `video-start` are captured from that point forward (not retroactively).

## Basic Recording

The filename is passed to `video-start`, not `video-stop`. The `.webm` is only
flushed to disk on `video-stop` — if the browser/session closes while a recording
is still open, the file is discarded. `video-stop` does **not** close the browser,
so no restart is needed to finalize a recording.

```bash
# Start recording (filename goes here)
playwright-cli video-start demo.webm

# Perform actions
playwright-cli open https://example.com
playwright-cli snapshot
playwright-cli click e1
playwright-cli fill e2 "test input"

# Stop and flush the .webm (takes no filename)
playwright-cli video-stop
```

## Best Practices

### 1. Use Descriptive Filenames

```bash
# Include context in filename (on video-start)
playwright-cli video-start recordings/login-flow-2024-01-15.webm
playwright-cli video-start recordings/checkout-test-run-42.webm
```

## Tracing vs Video

| Feature | Video | Tracing |
|---------|-------|---------|
| Output | WebM file | Trace file (viewable in Trace Viewer) |
| Shows | Visual recording | DOM snapshots, network, console, actions |
| Use case | Demos, documentation | Debugging, analysis |
| Size | Larger | Smaller |

## Limitations

- Recording adds slight overhead to automation
- Large recordings can consume significant disk space
