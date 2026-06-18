# Madad log monitor

A small diagnostic sidecar that tails docker logs for every `madad_*`
container, filters lines through a configurable rule set, and writes
matches to a host-mounted issues log file plus an admin HTTP API.

## What it does

- Streams `docker logs -f` for the watched containers (default: workflow,
  celery-worker, cms, communication, document, nudge, visibility)
- For each line, walks `rules.yml` — first matching rule wins
- Appends matches to `/data/issues.log` (host-mounted) and a ring buffer
- Exposes a tiny FastAPI admin API for tail / clear / stats / live stream
- Stubs a notification webhook (`NOTIFY_WEBHOOK_URL`) — defer-ready for
  Slack / Discord / Pushover wiring without code changes

## On the staging VM

Already wired into `docker/docker-compose.yml` as the `log_monitor`
service. It boots with the rest of the stack:

```bash
ssh jathish@34.18.50.97
cd ~/madad_ai_agent
docker compose -f docker/docker-compose.yml --env-file .env up -d log_monitor
docker compose -f docker/docker-compose.yml --env-file .env logs -f log_monitor
```

The issues log lives at `~/madad-monitor/issues.log` on the VM (the
default — override via `LOG_MONITOR_HOST_DIR` in `.env`).

## Tailing from your local machine

The simplest path is SSH + `tail -f` straight against the host-mounted
file:

```bash
ssh -i staging_server_access/jathish_madad_agent_staging jathish@34.18.50.97 \
    "tail -f ~/madad-monitor/issues.log"
```

Or via the admin API (token from `.env`, loopback-bound so use a tunnel
or hop into the box):

```bash
# Last 200 matched issues
curl -s -H "Authorization: Bearer $ADMIN_API_TOKEN" \
    http://localhost:8090/monitor/tail?n=200 | jq .

# Counts by rule / container / severity
curl -s -H "Authorization: Bearer $ADMIN_API_TOKEN" \
    http://localhost:8090/monitor/stats | jq .

# Truncate the issues log when you're done reviewing
curl -X POST -H "Authorization: Bearer $ADMIN_API_TOKEN" \
    http://localhost:8090/monitor/clear

# Live stream (SSE)
curl -N -H "Authorization: Bearer $ADMIN_API_TOKEN" \
    http://localhost:8090/monitor/stream
```

## Editing rules

`rules.yml` is baked into the image at build time; to iterate on rules
without rebuilding, mount the file:

```yaml
# Add this to the log_monitor compose service:
volumes:
  - ../tools/log_monitor/rules.yml:/app/rules.yml:ro
```

…or just rebuild after edits:

```bash
docker compose -f docker/docker-compose.yml --env-file .env up -d --build log_monitor
```

## Adding a notification channel (future)

When you decide on Slack / Discord / Pushover:

1. Get a webhook URL from the channel.
2. Set `LOG_MONITOR_NOTIFY_WEBHOOK` in `.env` on the VM.
3. `docker compose -f docker/docker-compose.yml --env-file .env up -d log_monitor`

The monitor will POST a JSON body matching the Slack incoming-webhook
shape (`text`, `username`, `icon_emoji`, `raw`) on every matched issue.
Most webhook tools accept this shape directly.

## Layout

```
tools/log_monitor/
├── main.py         # FastAPI app + tailer + rule engine
├── rules.yml       # default filter patterns
├── Dockerfile
├── requirements.txt
└── README.md
```

## Caveats

- Reads `docker.sock` (mounted RO). On a hardened host the monitor needs
  to be in the `docker` group equivalent — for our staging VM this is
  fine.
- Default rotation: the issues log rotates at 50 MB. If you fall behind
  on review, older snapshots accumulate as `issues.<timestamp>.log`
  alongside the live file — clean up periodically.
- This is a UAT diagnostic — for prod, layer Promtail + Loki on top once
  the agent settles down.
