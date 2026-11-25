#!/bin/bash

set -eu

CI_ROOT=$(pwd)
ORG_NAME="blinkbitcoin"

cat <<EOF > ${CI_ROOT}/gcloud-creds.json
${GOOGLE_CREDENTIALS}
EOF

gcloud auth activate-service-account --key-file ${CI_ROOT}/gcloud-creds.json

# GitHub App installation tokens expire after one hour, and cloning the whole
# org can take longer than that, so mint a fresh token before every API call.
refresh_gh_token() {
  local token
  token="$(gh-token generate -b "${GH_APP_PRIVATE_KEY}" -i "${GH_APP_ID}" | jq -r '.token')"
  if [[ -z "${token}" || "${token}" == "null" ]]; then
    echo "    --> failed to generate a GitHub App installation token" >&2
    return 1
  fi
  export GH_TOKEN="${token}"
}

refresh_gh_token
# no API keys up to 4,000 private repos.
repos="$(gh repo list ${ORG_NAME} --limit 4000 --json nameWithOwner --jq '.[].nameWithOwner')"
repo_count=$(grep -c . <<< "${repos}" || true)
if [[ "${repo_count}" -eq 0 ]]; then
  echo "    --> no repos listed for ${ORG_NAME}, refusing to upload an empty backup" >&2
  exit 1
fi
echo "    --> cloning ${repo_count} repos from ${ORG_NAME}"
while read -r repo; do
  refresh_gh_token
  echo "    --> cloning ${repo}"
  gh repo clone "$repo" "$repo" < /dev/null
done <<< "${repos}"

# compress the repo in a tarball
timestamp=$(date +%Y%m%d%H%M%S)
backup_name="${ORG_NAME}-${timestamp}.tar.gz"
tar -czf "${backup_name}" ${ORG_NAME}

# uploading
backup_size=$(du -h "${backup_name}" | awk '{print $1}')
echo "    --> Uploading ${backup_name} with size ${backup_size}"
gcloud storage cp "${backup_name}" gs://${GOOGLE_BUCKET_NAME}/blinkbitcoin_gh_org_backup/
