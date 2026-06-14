# Using Hippo from Slack

Ask Hippo questions without leaving Slack. The bot answers from your indexed
docs, with the same grounding as the web chat — and with **access tuned to the
surface** you ask from.

## How to ask

- **DM the app** — a private, flowing conversation. Follow-ups just work.
- **`@Hippo <question>` in a channel** — Hippo replies in the channel. To
  continue, reply **in the thread** and `@Hippo` again.

## Access rules (important)

Answers are role-filtered, and the surface changes *which* role applies:

- **In a DM**, Hippo uses **your full access** (your role's tier).
- **In a channel @mention**, Hippo only ever surfaces **`user`-tier** documents —
  admin/owner-tier content never appears in a channel, even if you're an admin or
  owner. Keep sensitive questions to DMs.

This split keeps higher-tier content out of shared channels by construction.

## What you get

A grounded answer with its sources, just like the web UI. The bot is **read-only
Q&A** — it doesn't ingest documents or change anything.

## For operators: running the bot

The Slack bot is a **separate process** that connects out over **Socket Mode** (an
outbound WebSocket), so it needs no public endpoint and works behind IAP.

Create a Slack app with Socket Mode enabled, these bot scopes —
`app_mentions:read, chat:write, im:history, im:read, im:write, users:read,
users:read.email, channels:history, groups:history` — and event subscriptions
`app_mention` + `message.im`. Then:

```bash
export HIPPO_SLACK_ENABLED=true
export HIPPO_SLACK_BOT_TOKEN=xoxb-…     # Bot User OAuth Token
export HIPPO_SLACK_APP_TOKEN=xapp-…     # App-Level Token (connections:write)
uv run hippo slack
```

Run it as its own process/container alongside `hippo serve` (it keeps its own
connection to the database). It refuses to start unless `HIPPO_SLACK_ENABLED=true`.

For the implementation (surface-split access, thread-aware history), see the
[Integrations internals](../technical/integrations.md).
