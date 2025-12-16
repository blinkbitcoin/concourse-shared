#!/usr/bin/env bash

#! Auto synced from Shared CI Resources repository
#! Don't change this file, instead change it in github.com/blinkbitcoin/concourse-shared

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get the base branch (default to main)
BASE_BRANCH="${1:-main}"

echo -e "${YELLOW}Finding directories with changed files compared to ${BASE_BRANCH}...${NC}"

# Get all changed files compared to the base branch
CHANGED_FILES=$(git diff --name-only "${BASE_BRANCH}"...HEAD)

if [ -z "$CHANGED_FILES" ]; then
    echo -e "${YELLOW}No changed files found compared to ${BASE_BRANCH}${NC}"
    exit 0
fi

echo -e "${YELLOW}Changed files:${NC}"
echo "$CHANGED_FILES"
echo ""

# Extract unique directories containing .tf files
TOFU_DIRS=$(echo "$CHANGED_FILES" | grep '\.tf$' | xargs -n1 dirname | sort -u)

if [ -z "$TOFU_DIRS" ]; then
    echo -e "${YELLOW}No Terraform/OpenTofu files changed${NC}"
    exit 0
fi

echo -e "${YELLOW}Directories with changed .tf files:${NC}"
echo "$TOFU_DIRS"
echo ""

# Track results
FAILED_DIRS=()
SUCCESS_COUNT=0
TOTAL_COUNT=0

# Process each directory
while IFS= read -r dir; do
    if [ ! -d "$dir" ]; then
        echo -e "${YELLOW}Skipping non-existent directory: ${dir}${NC}"
        continue
    fi

    TOTAL_COUNT=$((TOTAL_COUNT + 1))
    echo -e "${YELLOW}========================================${NC}"
    echo -e "${YELLOW}Processing: ${dir}${NC}"
    echo -e "${YELLOW}========================================${NC}"
    
    # Change to the directory
    cd "$dir"

    # Clean up any existing .terraform directory to avoid backend conflicts
    if [ -d ".terraform" ]; then
        echo -e "${YELLOW}Cleaning existing .terraform directory${NC}"
        rm -rf .terraform
    fi

    # Run tofu init -backend=false with -reconfigure to ignore existing backend config
    echo -e "${YELLOW}Running: tofu init -backend=false -reconfigure${NC}"
    if tofu init -backend=false -reconfigure 2>&1; then
        echo -e "${GREEN}✓ tofu init succeeded${NC}"
    else
        echo -e "${RED}✗ tofu init failed in ${dir}${NC}"
        FAILED_DIRS+=("${dir} (init failed)")
        cd - > /dev/null
        continue
    fi
    
    # Run prepare script if it exists
    REPO_ROOT=$(git rev-parse --show-toplevel)
    PREPARE_SCRIPT="${REPO_ROOT}/bin/prepare-for-validate.sh"
    if [ -f "$PREPARE_SCRIPT" ]; then
        echo -e "${YELLOW}Running prepare script: ${PREPARE_SCRIPT}${NC}"
        if "$PREPARE_SCRIPT"; then
            echo -e "${GREEN}✓ prepare script succeeded${NC}"
        else
            echo -e "${RED}✗ prepare script failed in ${dir}${NC}"
            FAILED_DIRS+=("${dir} (prepare failed)")
            cd - > /dev/null
            continue
        fi
    fi
    
    # Run tofu validate
    echo -e "${YELLOW}Running: tofu validate${NC}"
    if tofu validate; then
        echo -e "${GREEN}✓ tofu validate succeeded${NC}"
        SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
    else
        echo -e "${RED}✗ tofu validate failed in ${dir}${NC}"
        FAILED_DIRS+=("${dir} (validate failed)")
    fi
    
    # Return to original directory
    cd - > /dev/null
    echo ""
done <<< "$TOFU_DIRS"

# Print summary
echo -e "${YELLOW}========================================${NC}"
echo -e "${YELLOW}SUMMARY${NC}"
echo -e "${YELLOW}========================================${NC}"
echo -e "Total directories processed: ${TOTAL_COUNT}"
echo -e "${GREEN}Successful: ${SUCCESS_COUNT}${NC}"
echo -e "${RED}Failed: ${#FAILED_DIRS[@]}${NC}"

if [ ${#FAILED_DIRS[@]} -gt 0 ]; then
    echo -e "\n${RED}Failed directories:${NC}"
    for failed_dir in "${FAILED_DIRS[@]}"; do
        echo -e "${RED}  - ${failed_dir}${NC}"
    done
    exit 1
else
    echo -e "\n${GREEN}All validations passed!${NC}"
    exit 0
fi
