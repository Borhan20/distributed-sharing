#!/bin/sh
# Check that pool can be used from here: the pool command, the sign-in, and Docker.
# Usage: scripts/check.sh
# Uses $POOL_CMD when set (e.g. POOL_CMD="uv run --project /path/to/pool pool"), else `pool` on the PATH.
# Exit 0: ready. Exit 1: something is missing; each missing piece is printed with what to do.
# Never prompts.

set -u
set -f  # POOL_CMD is split into words below; never expand * or ? in it

missing=0
say() { printf '%s\n' "$*"; }

if [ -n "${POOL_CMD:-}" ]; then
    pool_cmd=$POOL_CMD
elif command -v pool >/dev/null 2>&1; then
    pool_cmd=pool
else
    say "MISSING pool command: not on the PATH and POOL_CMD is not set."
    say "  From a checkout of the pool repository: export POOL_CMD=\"uv run --project /path/to/pool pool\""
    pool_cmd=""
    missing=1
fi

if [ -n "$pool_cmd" ]; then
    # shellcheck disable=SC2086  # POOL_CMD may hold several words on purpose
    if me=$($pool_cmd whoami 2>&1); then
        say "OK   signed in: $me"
        # shellcheck disable=SC2086
        if groups=$($pool_cmd groups list 2>&1); then
            say "OK   groups:"
            printf '%s\n' "$groups" | sed 's/^/       /'
        fi
    else
        say "MISSING sign-in: $me"
        say "  Ask the user to run: pool login --server URL --token TOKEN --group GROUP"
        say "  (on a local test server: pool login --server http://127.0.0.1:8000 --dev-user NAME --group GROUP)"
        missing=1
    fi
fi

if command -v docker >/dev/null 2>&1; then
    if v=$(docker version --format '{{.Server.Version}}' 2>&1); then
        say "OK   docker $v"
    else
        say "MISSING docker daemon: $v"
        say "  Needed to build a Dockerfile and for pool run --local. Running an existing image does not need it."
        missing=1
    fi
else
    say "MISSING docker: not installed."
    say "  Needed to build a Dockerfile and for pool run --local. Running an existing image does not need it."
    missing=1
fi

exit $missing
