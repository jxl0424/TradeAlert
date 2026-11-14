#!/usr/bin/env bash
# Save data/tradealert.db as a single parentless commit force-pushed to the `data` branch,
# so the branch never accumulates binary history. Never touches the working tree, the
# checked-out branch or the default branch. Run from the repository root.
set -euo pipefail
BRANCH="${DATA_BRANCH:-data}"
DB="${DB_FILE:-data/tradealert.db}"
if [ ! -s "$DB" ]; then
  echo "No database at $DB, nothing to save."
  exit 0
fi
# Fold the WAL into the main file so the single file is complete.
python - "$DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
con.execute("PRAGMA journal_mode=DELETE")
con.close()
PY
export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-github-actions[bot]}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-41898282+github-actions[bot]@users.noreply.github.com}"
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"
blob=$(git hash-object -w "$DB")
tree=$(printf '100644 blob %s\ttradealert.db\n' "$blob" | git mktree)
commit=$(git commit-tree "$tree" -m "TradeAlert database snapshot $(date -u +%Y-%m-%dT%H:%MZ)")
git push --force origin "$commit:refs/heads/$BRANCH"
echo "Saved $(wc -c < "$DB") bytes to branch '$BRANCH' as $commit."
