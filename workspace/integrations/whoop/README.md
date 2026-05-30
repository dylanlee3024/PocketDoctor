# WHOOP Integration

This integration polls the WHOOP Developer API and writes both raw data and readable summaries into the health workspace so OpenClaw can use them as memory.

## What it writes

- `health/whoop/latest.md`: current WHOOP snapshot and 7-day trend
- `health/whoop/daily/YYYY-MM-DD.md`: per-day recovery, sleep, cycle, and workout summaries
- `health/whoop/profile.json`: WHOOP profile payload
- `health/whoop/body_measurement.json`: WHOOP body measurement payload
- `health/whoop/raw/*/*.jsonl`: append-safe raw API history

Because `health/` is already indexed by memory search, the assistant can retrieve WHOOP data during normal chats and weekly reviews.

## One-time setup

1. Create a WHOOP Developer app and add the redirect URI you want to use.
2. Copy `whoop.env.example` to `~/.config/whoop-sync.env` and fill in your client ID, client secret, and redirect URI.
3. Generate the auth URL:

   `python3 /home/bot0/.openclaw/workspace/integrations/whoop/whoop_sync.py auth-url`

4. Approve the WHOOP app in your browser.
5. Copy the full redirect URL from the browser and exchange it:

   `python3 /home/bot0/.openclaw/workspace/integrations/whoop/whoop_sync.py exchange --callback-url 'https://.../?code=...&state=...'`

6. Run a first sync:

   `python3 /home/bot0/.openclaw/workspace/integrations/whoop/whoop_sync.py sync`

## Postman fallback

If the callback URL flow is annoying, you can authorize the WHOOP app in Postman, copy the refresh token, and import it locally:

`python3 /home/bot0/.openclaw/workspace/integrations/whoop/whoop_sync.py import-refresh-token --refresh-token '<refresh-token>'`

## Automation

Systemd user files are installed at:

- `~/.config/systemd/user/whoop-sync.service`
- `~/.config/systemd/user/whoop-sync.timer`

The timer runs hourly after boot and safely stays idle until both the env file and token file exist.
