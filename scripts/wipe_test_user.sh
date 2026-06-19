#!/usr/bin/env bash
# Wipe one or more test users from staging — DB + Redis — and clear the
# log monitor's issues feed for a fresh QA cycle.
#
# Usage:
#   ./scripts/wipe_test_user.sh +919497191690 +918287611995
#   ./scripts/wipe_test_user.sh --pattern '+91%'
#   ./scripts/wipe_test_user.sh --dry-run +919497191690
#
# Runs:
#   1. python -m scripts.cleanup_test_users <args> inside the workflow
#      container (uses the same DSN + Redis URL the agent already does)
#   2. POST /monitor/clear so the next QA cycle starts from a zero
#      captured-issues baseline
#
# Requires: SSH key at staging_server_access/jathish_madad_agent_staging.

set -euo pipefail

SSH_KEY="staging_server_access/jathish_madad_agent_staging"
SSH_HOSTS="staging_server_access/known_hosts"
SSH_USER="jathish"
SSH_HOST="34.18.50.97"

if [[ $# -eq 0 ]]; then
    cat <<'EOF'
wipe_test_user.sh — wipe test user(s) from staging.

Usage:
  ./scripts/wipe_test_user.sh +919497191690 +918287611995
  ./scripts/wipe_test_user.sh --pattern '+91%'
  ./scripts/wipe_test_user.sh --dry-run +919497191690

Args are forwarded verbatim to scripts/cleanup_test_users.py — see
that script's --help for details.

After the wipe, POST /monitor/clear so the next QA cycle starts from
zero captured issues.
EOF
    exit 0
fi

CLEAN_ARGS=("$@")

# Auto-add --yes when stdin isn't a TTY (so CI / one-shot bash usage
# doesn't hang on the confirmation prompt). Doesn't apply to --dry-run.
IS_DRY=0
for arg in "$@"; do
    if [[ "$arg" == "--dry-run" ]]; then
        IS_DRY=1
    fi
done
if [[ $IS_DRY -eq 0 ]] && [[ ! -t 0 ]]; then
    CLEAN_ARGS+=("--yes")
fi

echo "Running cleanup inside the staging workflow container..."
ssh -i "$SSH_KEY" -o UserKnownHostsFile="$SSH_HOSTS" \
    "${SSH_USER}@${SSH_HOST}" \
    "cd ~/madad_ai_agent && docker compose -f docker/docker-compose.yml --env-file .env exec -T workflow \
        python -m scripts.cleanup_test_users ${CLEAN_ARGS[*]@Q}"

if [[ $IS_DRY -eq 1 ]]; then
    echo
    echo "(dry run — skipping monitor clear)"
    exit 0
fi

echo
echo "Clearing the log monitor issues feed..."
ssh -i "$SSH_KEY" -o UserKnownHostsFile="$SSH_HOSTS" \
    "${SSH_USER}@${SSH_HOST}" \
    'cd ~/madad_ai_agent && source .env && \
     curl -s -X POST -H "Authorization: Bearer $ADMIN_API_TOKEN" \
        http://localhost:8090/monitor/clear' \
    && echo

echo
echo "Done. Staging is ready for the next QA cycle."
