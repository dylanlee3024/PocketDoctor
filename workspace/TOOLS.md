# TOOLS.md - Local Notes

Skills define how tools work. This file is for local setup notes that matter to this specific assistant.

## Current Runtime

- Workspace root: `/home/bot0/.openclaw/workspace`
- Health records live under `health/`
- Daily raw memory lives under `memory/`
- Inbound Telegram and other media land under `/home/bot0/.openclaw/media/inbound`
- That inbound media directory is indexed for memory search alongside the health workspace

## Channel Notes

- Primary live channel: Telegram
- Bot handle from last probe: `@grindtilltheendbot`
- Authorized owner user ID: `7150642959`

## Health Data Workflow

- Save durable health facts into the structured `health/*.md` files
- Save day-specific events into `health/logs/YYYY-MM-DD.md`
- Save any report or lab summary into `health/reports/`
- When media matters long-term, reference the actual saved path in the corresponding log entry
- WHOOP device sync writes auto-generated files under `health/whoop/`; use `latest.md` for current context and `daily/` for date-specific analysis

## Token Pressure Notes

- When replies start feeling slow or context feels bloated, use `/status`, `/context list`, and `/usage tokens` in Telegram to inspect the active session.
- Prefer memory retrieval and structured health files over rereading large daily logs unless the question truly depends on raw detail.
- Avoid repeatedly sending large multi-image batches in one turn unless comparison across the full set is necessary.
- Favor concise practical answers by default; go deep only when Dill explicitly asks for depth.
