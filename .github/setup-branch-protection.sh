#!/usr/bin/env bash
# Repo config backing the auto-merge convention (CLAUDE.md, dev loop step 4).
# This state lives out-of-band in GitHub; captured here so it is reviewable and
# reproducible. Idempotent - safe to re-run.
set -euo pipefail
REPO="${1:-jake83/nimbleship}"

# Allow GitHub auto-merge, and delete branches once merged.
gh api -X PATCH "repos/$REPO" \
  -F allow_auto_merge=true -F delete_branch_on_merge=true >/dev/null

# Protect main: require only the ci.yml jobs (NOT the reviewer/refuter jobs,
# which usage-limit and would block every merge), with admin bypass so a
# triaged `--admin` logic merge still goes through.
gh api -X PUT "repos/$REPO/branches/main/protection" --input - >/dev/null <<'JSON'
{
  "required_status_checks": {
    "strict": false,
    "contexts": [
      "API (lint, types, tests)",
      "Web (lint, types, tests, build)",
      "Container images build"
    ]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null
}
JSON

echo "Applied auto-merge + branch protection to $REPO."
