#!/bin/bash

#! Auto synced from Shared CI Resources repository
#! Don't change this file, instead change it in github.com/blinkbitcoin/concourse-shared

set -eu

REPO_ROOT=${REPO_ROOT:-./}
LEVEL=${LEVEL:-high}
TASKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

pushd ${REPO_ROOT}

exec node "${TASKS_DIR}/audit-advisories.js" --level "${LEVEL}"
