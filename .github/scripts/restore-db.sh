#!/usr/bin/env bash
# Restore data/tradealert.db from the orphan `data` branch (if it exists yet).
# Run from the repository root with the checkout's credentials. Safe on the very first run.
set -euo pipefail
BRANCH="${DATA_BRANCH:-data}"
mkdir -p data
if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  git fetch --quiet --depth=1 origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
  git show "refs/remotes/origin/$BRANCH:tradealert.db" > data/tradealert.db
  echo "Restored data/tradealert.db from branch '$BRANCH' ($(wc -c < data/tradealert.db) bytes)."
else
  echo "No '$BRANCH' branch yet: starting with an empty database (first run)."
fi
