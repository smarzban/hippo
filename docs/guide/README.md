# Hippo Documentation

Hippo is an **agentic organizational knowledge base**. You feed it your team's
documents (Markdown, plain text, HTML, or Word/`.docx`), and people ask it
questions in plain language. It answers **only** from the documents you've
indexed — every claim is backed by a `[path > section]` citation — and it
respects who is allowed to see what (three-tier role-based access control).

It runs self-hosted from a single SQLite file, scales from one person to a whole
org, and is reachable through a web chat UI, an MCP server (Claude Code / Desktop),
and a Slack bot.

---

## Where to start

| You are… | Start here |
|---|---|
| **Trying Hippo for the first time** | [Quick Start](quickstart.md) — a running instance in ~5 minutes |
| **Installing it for a team** | [Install guide](install/README.md) — pick Docker, local, or production |
| **An everyday user** (asking questions, adding docs) | [User guide](users/README.md) |
| **An admin or owner** (managing folders, users, config) | [Admin tasks](users/admin-tasks.md) · [Owner tasks](users/owner-tasks.md) |
| **An engineer** (understanding or extending the code) | [Technical docs](technical/README.md) |

## The three documentation sets

- **[install/](install/README.md)** — every way to install and configure Hippo,
  from a 5-minute local trial to a hardened multi-user deployment. Includes the
  full environment-variable reference, the four authentication modes, and a
  production hardening checklist.

- **[users/](users/README.md)** — how to *use* every feature: asking questions
  and reading citations, adding documents, organizing folders, managing your
  profile and access tokens, and the admin/owner tasks. Written for people, not
  programmers.

- **[technical/](technical/README.md)** — how Hippo *works* and *why* it was
  built that way: the architecture, the RAG pipeline, the storage layer, the API,
  the RBAC model, the agent, the integrations, and the security invariants. For
  engineers reading, maintaining, or extending the code.

## What makes Hippo different

- **Grounded answers only.** The agent is constrained to answer from retrieved
  documents and to cite them. It is told never to improvise; document text is
  fed to the model inside an untrusted-content boundary so a malicious document
  cannot hijack the agent. See [How answers are grounded](users/asking-questions.md)
  and the [Agent internals](technical/agent.md).

- **Access control that actually holds.** Documents live in folders; each folder
  has a tier (`user` / `admin` / `owner`); retrieval is filtered by the caller's
  role in the database layer — chat, MCP, and Slack all go through the same
  filter. See [Folders & access](users/documents-and-folders.md) and the
  [RBAC model](technical/auth-and-rbac.md).

- **Self-hosted and simple.** The whole "brain" is one SQLite file. No external
  vector database, no managed service. See the [Architecture](technical/architecture.md).

---

*These docs live in `docs/guide/` and are designed to also power Hippo's in-app
help. Each page is a self-contained section so it can be surfaced individually.*
