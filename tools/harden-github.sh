#!/usr/bin/env bash
# Hardens the GitHub repository (free features for public repos).
#   - files: Dependabot, dependency review, OpenSSF Scorecard, CODEOWNERS, PR template,
#            least-privilege token for the tests workflow, CodeQL on main + dev
#   - branch "dev" created from main
#   - settings: secret scanning + push protection, Dependabot alerts + security updates,
#               private vulnerability reporting, read-only default GITHUB_TOKEN,
#               approval required for workflows from outside contributors, delete merged branches
#   - rulesets: no direct push / force push / deletion on main and dev (PR + green checks),
#               release tags v* can be created but never moved or deleted
# Run from the repository root, on an up-to-date main, with `gh auth login` done:
#   tools/harden-github.sh
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
OWNER="${REPO%%/*}"
say(){ printf '\n\033[1;35m▸ %s\033[0m\n' "$*"; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(git branch --show-current)" = main ] || die "checkout main first"
git diff --quiet && git diff --cached --quiet || die "commit or stash your changes first"
git pull --ff-only origin main

# ------------------------------------------------------------------ files
say "Security files"
mkdir -p .github/workflows
cat > .github/dependabot.yml <<'EOF'
# Weekly dependency updates, opened as PRs against dev.
version: 2
updates:
  - package-ecosystem: github-actions
    directory: /
    target-branch: dev
    schedule: { interval: weekly, day: monday }
    groups:
      actions: { patterns: ["*"] }
  - package-ecosystem: pip
    directories: ["/", "/tests"]
    target-branch: dev
    schedule: { interval: weekly, day: monday }
    groups:
      python: { patterns: ["*"] }
  - package-ecosystem: gradle
    directory: /android
    target-branch: dev
    schedule: { interval: weekly, day: monday }
    groups:
      android: { patterns: ["*"] }
EOF

cat > .github/workflows/dependency-review.yml <<'EOF'
name: dependency-review

# Blocks a PR that adds a dependency with a known high/critical vulnerability.
on:
  pull_request:

permissions:
  contents: read
  pull-requests: write

jobs:
  dependency-review:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v5
      - uses: actions/dependency-review-action@v4
        with:
          fail-on-severity: high
          comment-summary-in-pr: on-failure
EOF

cat > .github/workflows/scorecard.yml <<'EOF'
name: scorecard

# OpenSSF Scorecard: supply-chain security checks, results in Security > Code scanning
# and the public badge at https://scorecard.dev/viewer/?uri=github.com/OWNER/REPO
on:
  push:
    branches: [main]
  schedule:
    - cron: "17 5 * * 1"
  workflow_dispatch:

permissions: read-all

jobs:
  analysis:
    runs-on: ubuntu-24.04
    permissions:
      security-events: write
      id-token: write
    steps:
      - uses: actions/checkout@v5
        with:
          persist-credentials: false
      - uses: ossf/scorecard-action@v2.4.3
        with:
          results_file: results.sarif
          results_format: sarif
          publish_results: true
      - uses: github/codeql-action/upload-sarif@v4
        with:
          sarif_file: results.sarif
EOF

printf '* @%s\n' "$OWNER" > .github/CODEOWNERS

cat > .github/pull_request_template.md <<'EOF'
## What

## Why

## Checks
- [ ] Tests pass (`python -m pytest -q`)
- [ ] No secret, personal data or local config committed
- [ ] VERSION bumped if this is released
EOF

# least privilege for the tests workflow (it only reads the code)
grep -q '^permissions:' .github/workflows/tests.yml || \
  python3 - <<'EOF'
p = ".github/workflows/tests.yml"
s = open(p).read()
s = s.replace("\njobs:\n", "\npermissions:\n  contents: read\n\njobs:\n", 1)
open(p, "w").write(s)
EOF
# CodeQL also on PRs to dev
sed -i.bak 's/branches: \[ "main" \]/branches: [ "main", "dev" ]/' .github/workflows/codeql.yml && rm -f .github/workflows/codeql.yml.bak

git add .github SECURITY.md tools/harden-github.sh
if ! git diff --cached --quiet; then
  git commit -m "Add Dependabot, dependency review, Scorecard and repository policies"
  git push origin main
fi

# ------------------------------------------------------------------ dev branch
say "Branch dev"
if git ls-remote --exit-code --heads origin dev >/dev/null; then
  echo "dev already exists"
else
  git push origin main:refs/heads/dev
fi

# ------------------------------------------------------------------ settings
say "Repository settings"
gh api -X PATCH "repos/$REPO" --silent --input - <<'EOF'
{
  "delete_branch_on_merge": true,
  "allow_auto_merge": true,
  "allow_merge_commit": true,
  "allow_squash_merge": true,
  "allow_rebase_merge": false,
  "squash_merge_commit_title": "PR_TITLE",
  "squash_merge_commit_message": "PR_BODY",
  "allow_update_branch": true,
  "has_wiki": false,
  "security_and_analysis": {
    "secret_scanning": { "status": "enabled" },
    "secret_scanning_push_protection": { "status": "enabled" }
  }
}
EOF
gh api -X PUT "repos/$REPO/vulnerability-alerts" --silent && echo "Dependabot alerts: on"
gh api -X PUT "repos/$REPO/automated-security-fixes" --silent && echo "Dependabot security updates: on"
gh api -X PUT "repos/$REPO/private-vulnerability-reporting" --silent && echo "Private vulnerability reporting: on"
gh api -X PUT "repos/$REPO/actions/permissions/workflow" --silent \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false && echo "GITHUB_TOKEN: read-only by default"
gh api -X PUT "repos/$REPO/actions/permissions/fork-pr-contributor-approval" --silent \
  -f approval_policy=all_external_contributors 2>/dev/null && echo "Outside contributors: workflows need approval" || true

# ------------------------------------------------------------------ rulesets
upsert_ruleset(){ # name json
  local id
  id="$(gh api "repos/$REPO/rulesets" -q ".[] | select(.name==\"$1\") | .id" || true)"
  if [ -n "$id" ]; then gh api -X PUT "repos/$REPO/rulesets/$id" --silent --input - <<<"$2"; echo "updated: $1"
  else gh api -X POST "repos/$REPO/rulesets" --silent --input - <<<"$2"; echo "created: $1"; fi
}
say "Rulesets"
upsert_ruleset "protect main and dev" '{
  "name": "protect main and dev",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [],
  "conditions": { "ref_name": { "include": ["refs/heads/main", "refs/heads/dev"], "exclude": [] } },
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    { "type": "pull_request", "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": true,
        "allowed_merge_methods": ["merge", "squash"] } },
    { "type": "required_status_checks", "parameters": {
        "strict_required_status_checks_policy": false,
        "do_not_enforce_on_create": true,
        "required_status_checks": [
          { "context": "test" },
          { "context": "dependency-review" },
          { "context": "Analyze (python)" },
          { "context": "Analyze (java-kotlin)" },
          { "context": "Analyze (javascript-typescript)" },
          { "context": "Analyze (actions)" } ] } }
  ]
}'
upsert_ruleset "protect release tags" '{
  "name": "protect release tags",
  "target": "tag",
  "enforcement": "active",
  "bypass_actors": [],
  "conditions": { "ref_name": { "include": ["refs/tags/v*"], "exclude": [] } },
  "rules": [ { "type": "deletion" }, { "type": "update" }, { "type": "non_fast_forward" } ]
}'

say "Done"
cat <<EOF
Workflow from now on:
  git switch dev && git pull
  git switch -c feat/my-change      # work, commit
  git push -u origin feat/my-change && gh pr create --base dev --fill
  gh pr merge --squash --auto       # merges itself when the checks are green
Release:
  gh pr create --base main --head dev --title "Release x.y.z" && gh pr merge --merge --auto
  git switch main && git pull && git tag vX.Y.Z && git push origin vX.Y.Z
Check: https://github.com/$REPO/settings/rules  and  https://github.com/$REPO/security
EOF
