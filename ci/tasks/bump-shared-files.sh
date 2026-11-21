#!/bin/bash

set -eu

pushd repo
export ref=$(git rev-parse HEAD)
popd

pushd source-repo

mkdir -p ci

echo "    --> Generating vendir.yml based on ref ${ref}"
sed "s/ref:.*/ref: ${ref}/g" ../repo/vendir.tmpl.yml > ./ci/vendir.yml

echo $FEATURES | jq -c '.[]' | while read feat_str; do
  feat=$(echo $feat_str | tr -d '"')
  echo "    --> Adding feature ${feat} to vendir.yml"
  # removes the features we need from excludePaths in vendir yaml
  sed -i "/\b\($feat-*\)\b/d" ./ci/vendir.yml
done

pushd ci

# vendir fetches concourse-shared over HTTPS, which needs credentials once the
# repository is private. Mint a short-lived GitHub App installation token and hand
# it to git as an extra header through GIT_CONFIG_* on the vendir command only:
# nothing is written to disk, the committed ci/vendir.yml stays credential-free,
# and git does not print extra headers.
shared_urls=$(sed -n 's/^ *url: *//p' vendir.yml | sort -u)
if [[ $(printf '%s\n' "$shared_urls" | wc -l) -ne 1 || ! $shared_urls =~ ^https://github\.com/ ]]; then
  echo "Error: expected one github.com HTTPS url in vendir.yml, found: ${shared_urls}" >&2
  exit 1
fi
gh_token=$(ghtoken generate -b "${GH_APP_PRIVATE_KEY}" -i "${GH_APP_ID}" | jq -r '.token')
if [[ -z $gh_token || $gh_token == null ]]; then
  echo "Error: could not mint a GitHub App token for the vendir fetch" >&2
  exit 1
fi
auth_header="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$gh_token" | base64 | tr -d '\n')"
unset gh_token

# Run vendir sync and capture stderr
# Only ignore errors about empty directories, fail on other errors
# Could well be that we don't have any files to sync
stderr_file=$(mktemp)
echo "    --> Running vendir sync"
if ! GIT_CONFIG_COUNT=1 \
  GIT_CONFIG_KEY_0="http.${shared_urls}.extraheader" \
  GIT_CONFIG_VALUE_0="$auth_header" \
  vendir sync 2>"$stderr_file"; then
  stderr_content=$(cat "$stderr_file")
  if echo "$stderr_content" | grep -q "Expected to find at least one file within directory"; then
    echo "    --> Warning: vendir sync found empty directories (ignoring)" >&2
    cat "$stderr_file" >&2
  else
    echo "Error: vendir sync failed with unexpected error:" >&2
    cat "$stderr_file" >&2
    rm -f "$stderr_file"
    exit 1
  fi
fi
rm -f "$stderr_file"
unset auth_header

pushd vendor/tasks

# Those two changes are very counterintuitive and are therefore commented for now.
# Can be removed when we're sure they don't exist for a good reason.
# mv nodejs-helpers.sh helpers.sh || true
# mv rust-helpers.sh helpers.sh || true

popd

popd

echo "    --> Copying files to .github/workflows"
pushd .github/workflows

cp -r vendor/* .

rename -f 's/^nodejs-//' *
rename -f 's/^rust-//' *
rename -f 's/^docker-//' *
rename -f 's/^chart-//' *
rename -f 's/^tofu-//' *

popd

echo "    --> Copying dependabot config"
# Copy, do not move: ci/vendor must stay an exact vendir snapshot of ci/vendir.yml.
shopt -s nullglob
dependabot_configs=(ci/vendor/config/*-dependabot.yml)
shopt -u nullglob
if (( ${#dependabot_configs[@]} > 1 )); then
  echo "Error: expected at most one dependabot config, found: ${dependabot_configs[*]}" >&2
  exit 1
fi
if (( ${#dependabot_configs[@]} == 1 )); then
  cp "${dependabot_configs[0]}" .github/dependabot.yml
fi

if [[ ! -f ./typos.toml ]]; then
  cat <<EOF > typos.toml
[files]
extend-exclude = ["CHANGELOG.md"]
EOF
fi

echo "    --> Process bin files if directory exists"
if [ -d "bin/vendor" ]; then
  # Copy, do not move or rename in place: bin/vendor must stay an exact vendir
  # snapshot of ci/vendir.yml, like ci/vendor and .github/workflows/vendor.
  echo "    --> Copying bin files to root bin directory without feature prefixes"
  shopt -s nullglob
  for vendored in bin/vendor/*; do
    name=${vendored##*/}
    for prefix in nodejs- rust- docker- chart- tofu-; do
      name=${name#"$prefix"}
    done
    if [ -d "$vendored" ]; then
      # Merge into an existing directory instead of nesting a copy inside it.
      mkdir -p "bin/$name"
      cp -R "$vendored/." "bin/$name/"
    else
      cp "$vendored" "bin/$name"
    fi
  done
  shopt -u nullglob
fi

echo "    --> Committing changes"
if [[ -z $(git config --global user.email) ]]; then
  git config --global user.email "202112752+blinkbitcoinbot@users.noreply.github.com"
fi
if [[ -z $(git config --global user.name) ]]; then
  git config --global user.name "CI blinkbitcoinbot"
fi

(
  cd $(git rev-parse --show-toplevel)
  git add -A
  git status
  git commit -m "ci(shared): bump vendored ci files"
)
