# User Guide

How to use Hippo day to day. These pages are written for everyone — no
programming needed. If you're setting Hippo up, see the
[Install guide](../install/README.md) instead; if you want to know how it works
under the hood, see the [Technical docs](../technical/README.md).

## What Hippo does

Hippo answers your questions using **only** your team's indexed documents, and
shows you exactly which document and section each part of the answer came from.
It won't make things up — if it can't find support in the docs, it tells you.

## Everyone

- **[Signing in](signing-in.md)** — how to get in, depending on how your instance
  is set up.
- **[Asking questions](asking-questions.md)** — the chat, how answers are
  grounded, reading citations, and the "no sources" warning.
- **[Documents & folders](documents-and-folders.md)** — adding documents, how
  folders work, and who can see what.
- **[Your profile & tokens](profile-and-tokens.md)** — your display name, changing
  your password, and creating personal access tokens for tools.

## Admins & owners

- **[Admin tasks](admin-tasks.md)** — managing folders, users, and roles; syncing
  a directory of files.
- **[Owner tasks](owner-tasks.md)** — the System config tab, switching auth mode,
  changing models, and the first-run wizard.

## Connecting tools

- **[Using MCP](using-mcp.md)** — query Hippo from Claude Code or Claude Desktop.
- **[Using Slack](using-slack.md)** — ask Hippo from Slack.

## When something's off

- **[Troubleshooting & FAQ](troubleshooting.md)**

## The one rule worth remembering

What you can see depends on your **role** (`user`, `admin`, or `owner`) and the
**tier of the folder** a document lives in. A normal user sees user-tier content;
admins also see admin-tier; owners see everything. This is enforced everywhere —
chat, MCP, and Slack — so you'll only ever get answers from documents you're
allowed to read.
