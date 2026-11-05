#!/bin/sh
# Wait for a pool job to end, without attaching to it (so an interrupt can never cancel the job).
# Usage: scripts/wait.sh JOB-ID [TIMEOUT-SECONDS]     (default timeout 480, so it fits in one tool call)
# Uses $POOL_CMD when set, else `pool` on the PATH.
# Exit 0: the job ended (succeeded, failed or cancelled); the last `pool status` is printed.
# Exit 2: still running at the timeout; call it again.
# Exit 1: error (unknown job, not signed in, ...).
# Never prompts.

set -u
set -f  # POOL_CMD is split into words below; never expand * or ? in it

if [ $# -lt 1 ] || [ $# -gt 2 ]; then
    echo "usage: $0 JOB-ID [TIMEOUT-SECONDS]" >&2
    exit 1
fi
job=$1
timeout=${2:-480}
case $timeout in
    ''|*[!0-9]*) echo "TIMEOUT-SECONDS must be a whole number, got: $timeout" >&2; exit 1 ;;
esac
pool_cmd=${POOL_CMD:-pool}
interval=10
start=$(date +%s)
last=""

while :; do
    # shellcheck disable=SC2086  # POOL_CMD may hold several words on purpose
    if ! out=$($pool_cmd status "$job" 2>&1); then
        printf '%s\n' "$out" >&2
        exit 1
    fi
    # The first line reads: NAME  state=STATE  tasks=pending=1,running=2,...
    state=$(printf '%s\n' "$out" | head -n 1 | sed -n 's/.*state=\([a-z]*\).*/\1/p')
    tasks=$(printf '%s\n' "$out" | head -n 1 | sed -n 's/.*tasks=\([^ ]*\).*/\1/p')
    case $state in
        succeeded|failed|cancelled)
            printf '%s\n' "$out"
            echo "job $job ended: $state"
            exit 0 ;;
        '')
            printf '%s\n' "$out" >&2
            echo "could not read the job's state" >&2
            exit 1 ;;
    esac
    if [ "$state $tasks" != "$last" ]; then
        echo "$(date +%H:%M:%S) $state $tasks"
        last="$state $tasks"
    fi
    now=$(date +%s)
    if [ $((now - start)) -ge "$timeout" ]; then
        printf '%s\n' "$out"
        echo "job $job is still $state after ${timeout}s: run this script again to keep waiting"
        exit 2
    fi
    sleep "$interval"
done
