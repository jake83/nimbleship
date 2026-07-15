#!/usr/bin/env bash
# GitHub state backing CLAUDE.md's auto-merge convention (dev loop step 4),
# captured here to stay reviewable and reproducible. Idempotent.
set -euo pipefail
REPO="${1:-jake83/nimbleship}"

gh api -X PATCH "repos/$REPO" \
  -F allow_auto_merge=true -F delete_branch_on_merge=true >/dev/null

# Protect main: require only the ci.yml jobs (NOT the reviewer/refuter jobs,
# which usage-limit and would block every merge). enforce_admins keeps ci an
# inviolable gate for everyone - no one merges red ci - while a triaged logic
# PR still merges on ci-green, since the AI jobs are not required checks.
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
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null
}
JSON

echo "Applied auto-merge + branch protection to $REPO."
