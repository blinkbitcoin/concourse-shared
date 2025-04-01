#!/bin/bash

set -eu

export GH_TOKEN="$(ghtoken generate -b "${GH_APP_PRIVATE_KEY}" -i "${GH_APP_ID}" | jq -r '.token')"

pushd source-repo

# For forked repos (many are forked from GaloyMoney) we need to explicitly specify the REPO_NAME below
# So let's calculate here

REPO_NAME=$(basename $(git config --get remote.origin.url) .git)
REPO_OWNER=$(git config --get remote.origin.url | sed -n 's/.*github.com[:/]\([^/]*\).*/\1/p')
FULL_REPO="${REPO_OWNER}/${REPO_NAME}"

cat <<EOF >> ../body.md
# Bump Shared Tasks

This PR syncs in this repository, shared CI tasks from [concourse-shared](https://github.com/blinkbitcoin/concourse-shared).
EOF

gh pr close ${PR_BRANCH} --repo=${FULL_REPO} || true
gh pr create \
  --title "ci(shared): bump vendored ci files" \
  --body-file ../body.md \
  --base ${BRANCH} \
  --head ${PR_BRANCH} \
  --label blinkbitcoinbot \
  --repo=${FULL_REPO}
