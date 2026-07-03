#!/usr/bin/env bash
# Deploy the A0 Playwright CLI plugin to the agent-zero docker container.
# Usage: ./deploy.sh [--restart]

set -euo pipefail

CONTAINER="agent-zero"
DEST="/a0/usr/plugins/playwright_cli"

echo "→ Copying plugin files to $CONTAINER:$DEST ..."
docker -H ssh://docker.lan exec "$CONTAINER" mkdir -p "$DEST"
docker -H ssh://docker.lan cp . "$CONTAINER:$DEST"

echo "→ Clearing stale .pyc cache ..."
docker -H ssh://docker.lan exec "$CONTAINER" find "$DEST" -name "*.pyc" -delete

echo "→ Running execute.py inside container ..."
docker -H ssh://docker.lan exec "$CONTAINER" /opt/venv/bin/python "$DEST/execute.py"

if [[ "${1:-}" == "--restart" ]]; then
  echo "→ Restarting agent-zero ..."
  docker -H ssh://docker.lan restart "$CONTAINER"
fi

echo "✓ Done. Plugin installed at $DEST"
