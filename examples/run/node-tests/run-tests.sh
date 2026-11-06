#!/bin/sh
# Run the tests. With parts, each part runs its own share of the test files (Node's --test-shard).
# The report goes to /out/junit.xml (JUnit format, read by most CI tools); the exit code is the result.
part=$(( ${POOL_TASK_INDEX:-0} + 1 ))
parts=${POOL_TASK_COUNT:-1}
exec node --test --test-shard="$part/$parts" \
  --test-reporter=spec --test-reporter-destination=stdout \
  --test-reporter=junit --test-reporter-destination=/out/junit.xml \
  test/*.test.js
