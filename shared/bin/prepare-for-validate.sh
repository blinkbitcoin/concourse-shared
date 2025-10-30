#!/usr/bin/env bash


set -e

echo "    --> Preparing environment for Terraform/OpenTofu validation..."

# Change to repository root
pushd "$(git rev-parse --show-toplevel)" > /dev/null

# Create missing secret files for validation
mkdir -p reproduce/secrets/{blink/{staging,bbw},shared,stablesats,testflight,staging}

# Touch all the missing files mentioned in validation errors
touch reproduce/secrets/blink/staging/honeycomb-api-key
touch reproduce/secrets/staging/lana-bank-ci-bq-sa-base64
touch reproduce/secrets/blink/staging/lnd1-pass
touch reproduce/secrets/blink/bbw/lnd1-pass
touch reproduce/secrets/shared/npm-token
touch reproduce/secrets/shared/npm-token-blink
touch reproduce/secrets/stablesats/okex-api-key
touch reproduce/secrets/stablesats/okex-passphrase
touch reproduce/secrets/stablesats/okex-secret-key
touch reproduce/secrets/testflight/okex-api-key
touch reproduce/secrets/testflight/okex-passphrase
touch reproduce/secrets/testflight/okex-secret-key
touch reproduce/secrets/staging/pinecone-sync-openai-api-key
touch reproduce/secrets/staging/pinecone-sync-pinecone-api-key
touch reproduce/secrets/staging/pinecone-sync-airtable-access-token
touch reproduce/secrets/testflight/prelude-api-key
touch reproduce/secrets/staging/scaleway-access-key

# Additional files from provision directory errors
touch reproduce/secrets/blink/staging/honeycomb-api-key
touch reproduce/secrets/stablesats/galoy-auth-code
touch reproduce/secrets/testflight/twilio-account-sid
touch reproduce/secrets/testflight/twilio-auth-token
touch reproduce/secrets/staging/scaleway-secret-key
touch reproduce/secrets/shared/mobile-mattermost-api-url
touch reproduce/secrets/shared/mobile-rollouts-mattermost-api-url
touch reproduce/secrets/blink/staging/smoketest-kubeconfig.base64
mkdir -p reproduce/secrets/cepler && touch reproduce/secrets/cepler/crates-api-token
mkdir -p reproduce/secrets/galoy-reporting && touch reproduce/secrets/galoy-reporting/mattermost-api-url

popd > /dev/null

echo "    --> Environment preparation completed."
