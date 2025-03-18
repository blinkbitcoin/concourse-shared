#!/bin/bash

set -eu

tar_out="$(pwd)/bundled-deps"

pushd deps
# Install dependencies
echo "    --> pnpm install"
pnpm install --shamefully-hoist

# Get git reference for versioning
echo "    --> git log"
git log --pretty=format:'%h' -n 1 > gitref

# Create the output filename
output_file="${tar_out}/bundled-deps-v$(cat ../deps-version/number)-$(cat gitref).tgz"

# Use --dereference to convert hard links to regular files
echo "    --> tar ..."
tar --dereference -zcf "$output_file" \
    --exclude='.git' \
    --exclude='.github' \
    --exclude='ci' \
    .

echo "    --> Done!"
popd
