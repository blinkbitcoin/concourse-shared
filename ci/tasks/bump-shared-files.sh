#!/bin/bash

set -eu

pushd repo
export ref=$(git rev-parse HEAD)
popd

pushd source-repo

mkdir -p ci
sed "s/ref:.*/ref: ${ref}/g" ../repo/vendir.tmpl.yml > ./ci/vendir.yml

echo $FEATURES | jq -c '.[]' | while read feat_str; do
  feat=$(echo $feat_str | tr -d '"')

  # removes the features we need from excludePaths in vendir yaml
  sed -i "/\b\($feat-*\)\b/d" ./ci/vendir.yml
done

pushd ci

# Run vendir sync and capture stderr
# Only ignore errors about empty directories, fail on other errors
# Could well be that we don't have any files to sync
stderr_file=$(mktemp)
if ! vendir sync 2>"$stderr_file"; then
  stderr_content=$(cat "$stderr_file")
  if echo "$stderr_content" | grep -q "Expected to find at least one file within directory"; then
    echo "Warning: vendir sync found empty directories (ignoring)" >&2
    cat "$stderr_file" >&2
  else
    echo "Error: vendir sync failed with unexpected error:" >&2
    cat "$stderr_file" >&2
    rm -f "$stderr_file"
    exit 1
  fi
fi
rm -f "$stderr_file"

pushd vendor/tasks

# Those two changes are very counterintuitive and are therefore commented for now.
# Can be removed when we're sure they don't exist for a good reason.
# mv nodejs-helpers.sh helpers.sh || true
# mv rust-helpers.sh helpers.sh || true

popd

popd

pushd .github/workflows

cp -r vendor/* .

rename -f 's/^nodejs-//' *
rename -f 's/^rust-//' *
rename -f 's/^docker-//' *
rename -f 's/^chart-//' *
rename -f 's/^tofu-//' *

popd

mv ci/vendor/config/*-dependabot.yml .github/dependabot.yml || true

if [[ ! -f ./typos.toml ]]; then
  cat <<EOF > typos.toml
[files]
extend-exclude = ["CHANGELOG.md"]
EOF
fi

# Process bin files if directory exists
if [ -d "bin/vendor" ]; then
  pushd bin/vendor

  rename -f 's/^nodejs-//' *
  rename -f 's/^rust-//' *
  rename -f 's/^docker-//' *
  rename -f 's/^chart-//' *
  rename -f 's/^tofu-//' *

  popd

  # Copy bin files to root bin directory
  mkdir -p bin
  cp -r bin/vendor/* bin/ || true
fi

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
