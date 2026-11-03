#!/usr/bin/env bash
# Stop what scripts/dev-up.sh started: the agent from this checkout and the dev server.
# All data (database, files, this computer's enrollment) is kept, so dev-up.sh resumes where you left off.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT="$ROOT/agent/bin/pool-agent"

if pgrep -f "^$AGENT run" >/dev/null 2>&1; then
  pkill -TERM -f "^$AGENT run" || true
  for _ in $(seq 1 20); do pgrep -f "^$AGENT run" >/dev/null 2>&1 || break; sleep 0.5; done
  echo "agent stopped"
else
  echo "agent was not running"
fi

docker compose -f "$ROOT/deploy/compose.yaml" --profile storage down
cat <<'EOF'

Server stopped. Your data is kept; start again with scripts/dev-up.sh.
To delete everything instead (database, stored files), run this yourself; it cannot be undone:
  docker compose -f deploy/compose.yaml --profile storage down -v
EOF
