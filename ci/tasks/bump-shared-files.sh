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
vendir sync

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

popd

mv ci/vendor/config/*-dependabot.yml .github/dependabot.yml || true

if [[ ! -f ./typos.toml ]]; then
  cat <<EOF > typos.toml
[files]
extend-exclude = ["CHANGELOG.md"]
EOF
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
