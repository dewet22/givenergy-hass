#!/usr/bin/env bash
# Apply (or update) the baseline branch-protection ruleset for this repo.
#
# Requires: gh CLI authenticated with admin rights on the repo. Set REPO to
# point at a different repo, or rely on the default.
#
# Idempotent: looks up an existing ruleset by name and PUTs to update it if
# found, POSTs a new one otherwise.

set -euo pipefail

REPO="${REPO:-dewet22/givenergy-hass}"
RULESET_NAME="release branches baseline"

USER_ID=$(gh api /user --jq .id)

# The ruleset itself: block force-push and deletion on main/v1.0, require the
# two validate.yml jobs to pass on PRs. The repo owner is bypass-listed so
# deliberate maintenance operations (e.g. release-branch resets) still work.
read -r -d '' BODY <<EOF || true
{
  "name": "${RULESET_NAME}",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": {
      "include": ["refs/heads/main", "refs/heads/v1.0"],
      "exclude": []
    }
  },
  "rules": [
    {"type": "non_fast_forward"},
    {"type": "deletion"},
    {
      "type": "required_status_checks",
      "parameters": {
        "required_status_checks": [
          {"context": "HACS validation"},
          {"context": "hassfest"}
        ],
        "strict_required_status_checks_policy": false
      }
    }
  ],
  "bypass_actors": [
    {"actor_id": ${USER_ID}, "actor_type": "User", "bypass_mode": "always"}
  ]
}
EOF

existing_id=$(
  gh api "/repos/${REPO}/rulesets" \
    --jq ".[] | select(.name == \"${RULESET_NAME}\") | .id" \
  || true
)

if [ -n "${existing_id}" ]; then
  echo "Updating existing ruleset #${existing_id} on ${REPO}…"
  printf '%s' "${BODY}" | gh api -X PUT "/repos/${REPO}/rulesets/${existing_id}" --input -
else
  echo "Creating new ruleset on ${REPO}…"
  printf '%s' "${BODY}" | gh api -X POST "/repos/${REPO}/rulesets" --input -
fi

echo
echo "Current rulesets on ${REPO}:"
gh api "/repos/${REPO}/rulesets" \
  --jq '.[] | "  #\(.id)  \(.name)  [\(.enforcement)]"'
