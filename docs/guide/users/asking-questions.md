# Asking questions

The chat is the heart of Hippo. Type a question in plain language and Hippo
answers from your team's documents, citing its sources.

## How to ask

- Open the chat and type your question, or click one of the suggested prompts on
  an empty conversation.
- Ask follow-ups naturally — the conversation has memory within a session.
- Be specific. "How does Polly integrate with Telegram?" works better than
  "tell me about Polly." If a question is too broad and needs too many lookups,
  Hippo will say it hit its research limit — narrow it or ask one thing at a time.

## Watching it work

While Hippo answers, you'll see progress lines like *"Searching …"*, *"Reading
document #…"*, or *"Scanning sources for …"*. These are the agent's **tools**: it
searches, reads documents, lists what's available, and greps for exact strings to
find the right material before answering. You don't control these directly —
they're how it researches your question.

## How answers are grounded

Hippo is built to answer **only** from indexed documents, never from general
knowledge or guesswork. Concretely:

- It retrieves relevant chunks from your documents and answers from them.
- It is instructed to **cite every claim** and **never improvise**.
- Document text is handed to the model inside an untrusted-content boundary, so
  instructions hidden in a document can't hijack the answer.

If Hippo genuinely can't find anything relevant, it tells you it has no sources
rather than inventing an answer.

## Reading citations

Answers carry **footnote-style citation markers** and a **Sources** list at the
bottom. Each source is shown as `Title › Section`.

- **Click a citation marker or a source entry** to open that document in a side
  drawer, scrolled to the cited section — so you can verify the claim in context.
- A source shown as a plain (non-clickable) entry means the citation didn't
  resolve to a document you can currently open (e.g. it was removed) — treat it
  with extra caution.

## The "No sources cited" warning

If Hippo produces a substantial answer with **no citations** (and it wasn't an
explicit "I have no sources" reply), you'll see:

> ⚠ No sources cited — verify independently.

This is a safety prompt: a good Hippo answer should be backed by citations, so an
uncited one is worth double-checking. (The server also logs when a substantial
answer lacks a citation, so operators can monitor grounding quality.)

## What it won't do

- It won't answer from outside your documents.
- It won't show you content from folders above your role's tier — retrieval is
  filtered by access, so you simply won't get those results. See
  [Documents & folders](documents-and-folders.md).

## Tips

- If an answer seems thin, add a document on that topic (see
  [Adding documents](documents-and-folders.md)) and ask again.
- Use precise nouns, project names, and acronyms — they help both the keyword and
  the semantic search find the right chunks.
- Prefer one focused question over a multi-part one to stay within the research
  budget.
