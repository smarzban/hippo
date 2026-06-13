# Hippo Slack Integration — Design (2026-06-13)

Roadmap item **7**. A read-only Q&A bot: ask Hippo questions in Slack, get answers from the
indexed docs with the same `[path > section]` citations and the same role-filtering as the
web chat. Brainstormed and approved with Saeed on 2026-06-13.

Deployment target unchanged: GKE behind **IAP**, own HTTPS domain. The Slack bot is a
**new front door onto the existing agent** — no agent, retrieval, or auth-model changes;
it reuses `build_agent()` + `HubDeps(store, role)` and the Storage-layer role filtering
verbatim.

---

## 1. Scope

**In (v1):** ask questions via **DM** or **@mention** in a channel; threaded follow-ups;
role-filtered answers; citations; placeholder→update UX.

**Out (YAGNI, easy to add later):** ingesting/uploading docs via Slack, slash commands,
admin/config commands, interactive Block Kit components, home tab, channel allowlists,
per-user rate limiting beyond the existing tool-call budget.

---

## 2. Connection & process model

A standalone **`hippo slack`** CLI command (mirrors `hippo serve` / `hippo mcp`) running
**Slack Bolt for Python in Socket Mode**.

**Why Socket Mode (not HTTP Events API).** Hippo runs behind GCP IAP. Slack's inbound
HTTP webhooks cannot authenticate through IAP, so an Events-API endpoint would have to be
IAP-exempted and re-secured with Slack request-signing — a second public attack surface.
Socket Mode instead opens an **outbound** WebSocket from the pod to Slack: nothing inbound
crosses IAP, no public endpoint, and **no signing secret** is needed. Outbound WebSocket is
exactly what a locked-down pod can do.

**Why a separate process.** Socket Mode is a long-lived asyncio WebSocket loop; the bot
builds its **own** `Storage` + agent on its **own** sqlite connection (the one-Storage-
per-connection rule — see CLAUDE.md). Running it inside `hippo serve` would either share a
connection across two subsystems or add a second `Storage` on one connection; a separate
process sidesteps both and matches the existing CLI shape. In the pod it is a second
container alongside `serve` (a deploy-manifest note for roadmap item 10).

**Secrets / config (env, `HIPPO_` prefix):**

| Var | Default | Notes |
|-----|---------|-------|
| `HIPPO_SLACK_ENABLED` | `false` | `hippo slack` refuses to start unless true (guards against half-configured deploys) |
| `HIPPO_SLACK_BOT_TOKEN` | _(unset)_ | `xoxb-…` bot token (chat:write, users:read.email, history scopes) |
| `HIPPO_SLACK_APP_TOKEN` | _(unset)_ | `xapp-…` app-level token with `connections:write`, for Socket Mode |

Both tokens required when enabled; `hippo slack` fails fast with a clear message otherwise.

**Slack app manifest (documented in README):** Socket Mode on; bot scopes
`app_mentions:read`, `chat:write`, `im:history`, `im:read`, `im:write`, `users:read`,
`users:read.email`, `channels:history`, `groups:history`; event subscriptions
`app_mention` and `message.im`.

Rejected alternatives:
- *HTTP Events API* — needs an IAP exemption + request-signing; second public surface. No
  upside behind IAP.
- *In-process with `hippo serve`* — connection-sharing hazard; couples a WebSocket loop to
  the HTTP server lifecycle.

---

## 3. Identity → role

On each inbound message, fetch the Slack user's email via `users.info`
(`profile.email`, scope `users:read.email`) and run it through the **same** resolution the
web and MCP paths use: domain check → `ensure_user` (first-timers default to `developer`)
→ admin-email bootstrap. To keep one source of truth, today's `_email_to_role` closure in
`api.py` is **extracted into a shared `auth.resolve_role(store, settings, email) -> str`**
(raises `AuthError` on domain failure); `api.py` and the Slack bot both call it.

- Out-of-domain email, or no email on the Slack profile → polite refusal ("You don't have
  access to Hippo — sign in with your work email."). Never silently answer.
- Bot/app messages and Hippo's own messages are ignored (no self-reply loops).

The bot authenticates to Hippo **directly** (it owns the `Storage`), so no bearer token is
involved on the bot→Hippo path; the Slack email *is* the identity.

Rejected: a single service token / fixed role for all Slack traffic — would discard
per-user role filtering, the whole point of the access model.

---

## 4. Access policy — split by surface

Retrieval is role-filtered by the **asker**, but a channel has an audience, so "answer as
the asker" can leak manager-only docs to everyone watching. Therefore:

- **DM** → the asker's **full role**. Private surface; you see everything you're allowed to.
- **Channel @mention** → forced **`everyone`-access only**, regardless of who asks. Public
  surface, public knowledge. A manager who wants the manager-doc answer asks in a DM.

Implemented by choosing the role passed to the agent based on surface: DM uses
`resolve_role(...)`; channel uses the developer-equivalent (`everyone`) view even for
managers/admins. Fails closed.

Rejected: *answer-as-asker everywhere* (leaks); *answer in-channel but redirect
manager-content to an ephemeral/DM reply* (Slack's event model makes reliable ephemeral
replies to `app_mention` fiddly; not worth the complexity for v1).

---

## 5. Surfaces, threading & conversation memory

**DMs are flat.** A DM is already a private 1:1 channel; messages flow sequentially, no
@mention needed (the `message.im` event only fires for messages sent to the bot). The
recent DM message sequence is the conversation context.

**Channels are threaded.** Hippo responds only to `app_mention` (so it sees only messages
explicitly addressed to it — minimal scopes, privacy-respecting). It replies **in a thread**
hanging off the mention; follow-ups happen in that thread and must **re-@mention** Hippo.
The thread is the conversation boundary.

**Thread-aware memory.** For both surfaces, prior turns are reconstructed and passed to the
agent as `message_history`:
- Channel: `conversations.replies(channel, thread_ts)` → the thread's messages.
- DM: recent messages in the IM (a bounded window).
Each prior turn maps to a pydantic-ai `ModelRequest`(user question) / `ModelResponse`(Hippo's
answer) pair; the current message is the new prompt. Hippo's own past messages are
identified by bot user id. The split-by-surface access rule still holds per surface (a
channel thread reconstructs as `everyone`-scoped; a DM as full-role).

A bounded history window (most recent N turns) caps token cost on Ollama Cloud.

---

## 6. UX & formatting

- **Placeholder → update.** On receipt, immediately post "_Searching the knowledge base…_"
  (in-thread for channels), capture its `ts`, run the agent, then `chat.update` that message
  with the answer. Slow Ollama-Cloud answers still show instant activity.
- **Citations** stay as `[path > section]` text (docs are not web-addressable), lightly
  formatted for Slack `mrkdwn`.
- **Errors.** Agent exceptions and usage-limit hits become a friendly message
  ("Sorry — I hit an error answering that."), never a stack trace. Logged server-side.
- The agent's prompt-injection framing, cite-everything system prompt, and tool-call budget
  apply unchanged — a Slack question is trusted *as a question*, document content remains
  the untrusted-data boundary already enforced in the agent.

---

## 7. Module layout & testability

`src/hippo/slack_bot.py`:
- **Pure, unit-testable functions** (no Slack, no network):
  - `surface_role(resolved_role, *, is_dm) -> str` — the split-by-surface rule.
  - `build_history(turns) -> list[ModelMessage]` — thread/DM turns → pydantic-ai history.
  - `format_answer(text) -> str` — answer/citation → Slack mrkdwn.
  - `answer_question(agent, store, *, question, role, history) -> str` — run the agent to a
    final string (reused by all surfaces).
- **`build_slack_app(store, agent, settings, *, client=None)`** — wires Bolt handlers
  (`app_mention`, `message.im`) onto an injected Slack client; the handlers are thin
  adapters: resolve identity → pick surface role → reconstruct history → `answer_question`
  → post/update. A **fake client** in tests records `postMessage`/`update`/`users_info`
  /`conversations_replies` payloads so the whole handler path is asserted with **no network**.
- The Socket-Mode runner (`AsyncSocketModeHandler(...).start()`) lives in the `hippo slack`
  CLI command and is **not** unit-tested — same posture as the `hippo mcp` stdio runner.

**Tests (`tests/test_slack_bot.py`)**, zero-network, `ALLOW_MODEL_REQUESTS=False`,
`FakeEmbedder` + `TestModel`/`FunctionModel`:
- `surface_role`: DM→full role; channel→`everyone` even for manager/admin.
- Identity: out-of-domain email → refusal; first-timer → developer; admin bootstrap.
- `build_history`: alternating turns → correct `ModelRequest`/`ModelResponse` sequence;
  bounded window.
- Access enforcement end-to-end against an rbac store: a manager's **DM** sees a
  manager-source doc; the same manager's **channel @mention** does not.
- Handler path via the fake client: placeholder posted then updated; thread replies use
  `thread_ts`; DM replies are flat; refusal path posts the refusal.
- `format_answer`: citations render as readable mrkdwn.

No SQL is added outside `storage.py`; the bot calls existing `Storage` methods with the
chosen role.

---

## 8. Out-of-scope confirmations

- Works in any channel it is invited to (no allowlist in v1).
- No rate limiting beyond the per-question tool-call budget.
- No streaming (Slack has no token streaming; placeholder→update is the idiom).
