# Cleanup test users

Two ways to wipe a test user from staging — pick what fits.

## Option A: One-shot from your local terminal

```bash
./scripts/wipe_test_user.sh +919497191690 +918287611995
./scripts/wipe_test_user.sh --pattern '+91%'
./scripts/wipe_test_user.sh --dry-run +919497191690
```

The wrapper SSHes to staging, runs the Python cleanup inside the
workflow container (so the DSN + Redis URL are already set), and
clears the log monitor's issues feed for a fresh QA cycle.

When stdin isn't a TTY (e.g. CI / piping), `--yes` is auto-added so the
confirmation prompt doesn't block.

## Option B: Directly from the VM

```bash
ssh jathish@34.18.50.97
cd ~/madad_ai_agent
docker compose -f docker/docker-compose.yml --env-file .env exec workflow \
    python -m scripts.cleanup_test_users +919497191690 +918287611995
```

## What gets wiped

| Layer | Tables / keys |
|---|---|
| Postgres `workflow` | `runs`, `run_audit` (joined via `run_id`) |
| Postgres `communication` | `messages`, `conversations` (matched by `data->>'identity'`) |
| Postgres `nudge` | `reminders`, `sequences` (matched by `data::text LIKE '%<identity>%'`) |
| Postgres `public` | `checkpoints`, `checkpoint_writes`, `checkpoint_blobs` (joined to workflow.runs via `thread_id` BEFORE the run row is dropped) |
| Redis | every key matching `*<identity>*` (sessions, webhook dedupe, campaign_start locks, invoice sigs) |

All deletes run inside a single Postgres transaction — partial failures
roll back cleanly.

## Flags

| Flag | What |
|---|---|
| `--dry-run` | Print counts that would be deleted; touch nothing. |
| `--pattern '+91%'` | SQL LIKE pattern matched against `data->>'identity'`. Combine with explicit identities — both are unioned. |
| `--yes` | Skip the confirmation prompt. |

## Safety

* Refuses to run with no identities AND no pattern (no accidental
  "wipe everything" by typo).
* Always prompts for confirmation unless `--yes` is passed.
* `--dry-run` shows exact row counts before you commit.

## Examples

```bash
# Wipe two specific numbers (used today's QA cycle)
./scripts/wipe_test_user.sh +919497191690 +918287611995

# Dry-run all Indian test numbers
./scripts/wipe_test_user.sh --pattern '+91%' --dry-run

# Wipe everything matching a pattern AND a specific identity
./scripts/wipe_test_user.sh --pattern '+97455%' +919497191690

# Wipe with no prompt (CI / scripts)
./scripts/wipe_test_user.sh --yes +97499999001
```
