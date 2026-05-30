# PocketDoctor

> A personal AI health agent that learns your body over time using wearable data, photos, and conversation.

## Overview

PocketDoctor is an OpenClaw-based AI health bot that continuously ingests data from a WHOOP wearable, chat history, and photographs to build a comprehensive, evolving picture of the user's health. Instead of generic advice, it delivers personalized insights grounded in the user's own biometric history — sleep quality, recovery scores, HRV, strain, and workout data — all synthesized into a living memory system that improves the longer it runs. The agent is designed to act as a data-driven health strategist accessible through Telegram.

## Tech Stack

- **OpenClaw** — agent runtime and orchestration framework
- **OpenAI GPT** — primary reasoning and response generation
- **Google Gemini** — vector embeddings and hybrid memory search
- **WHOOP API** — wearable health data ingestion (sleep, recovery, HRV, strain, workouts)
- **Telegram** — user-facing chat interface
- **Python** — WHOOP sync bridge (`whoop_sync.py`)
- **Node.js / npm** — skill and plugin management
- **SQLite** — local session and memory storage
- **Cron** — scheduled memory dreaming and data promotion

## Getting Started

### Prerequisites

- [OpenClaw](https://openclaw.dev) installed and configured
- Python 3.10+
- Node.js 18+
- A WHOOP account with API access
- OpenAI API key
- Google Gemini API key
- Telegram bot token

### Installation

```bash
git clone https://github.com/dylanlee3024/PocketDoctor.git
cd PocketDoctor

# Copy the example config and fill in your credentials
cp openclaw.json.example openclaw.json

# Install Python dependencies for WHOOP sync
pip install -r workspace/integrations/whoop/requirements.txt

# Install OpenClaw skills/plugins
openclaw install
```

### Required Configuration

Copy `openclaw.json.example` to `openclaw.json` and supply the following:

| Key | Description |
|-----|-------------|
| `env.GEMINI_API_KEY` | Google Gemini API key for embeddings and web search |
| `plugins.google.webSearch.apiKey` | Same Gemini key used for web search |
| `gateway.auth.token` | Local gateway auth token (generate a random string) |
| `channels.telegram.allowFrom` | Your Telegram user ID |

For WHOOP sync, copy `workspace/integrations/whoop/whoop.env.example` to `workspace/integrations/whoop/whoop.env` and add your WHOOP OAuth credentials.

Your OpenAI API key is stored separately in the OpenClaw agent auth profile — run `openclaw configure` to set it up interactively.

> ⚠️ Note: Configuration files containing API keys, OAuth tokens, device identity keys, and personal health profile data have been excluded from this repository for security. See above for what you'll need to supply before running the agent.

## Engineering Notes

### Challenges

The core difficulty was making the agent genuinely useful without burning through API credits on every message. Each prompt needs enough context to give personalized advice, but naively injecting all available health history would send gigabytes of data per request. The solution was a layered memory architecture — short-term recall, daily logs, and promoted long-term memory — so only the most relevant context gets loaded per session. Getting the hybrid retrieval (vector + text weights, temporal decay, MMR reranking) tuned to pull the right records without flooding the context window took significant iteration.

Accurate context injection was the other major challenge: deciding what the agent *needs to know right now* versus what should stay in cold storage, and making that boundary dynamic based on query type and recency.

### Breakthroughs

The biggest unlock was implementing the **dreaming system** — a nightly cron job that runs while the user sleeps and promotes the most recurring, high-confidence observations from short-term recall into long-term memory. This mirrors how human memory consolidates during sleep and means the agent's knowledge base compounds over time without manual curation. Insights that appear across multiple sessions get weighted up and eventually promoted to `MEMORY.md`, while one-off noise fades out.

The second breakthrough was structuring health data across distinct timeframes: raw WHOOP JSONL by month, daily markdown summaries, a rolling `latest.md` snapshot, and a `biometrics.md` for durable trends. This hierarchy lets the agent answer "how did I sleep last night?" and "what's my HRV baseline over the past month?" with the same retrieval pipeline, just hitting different layers.

## Screenshots

<!-- screenshots -->

## License

MIT
