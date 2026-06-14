# Documents & folders

Everything Hippo knows comes from documents you add. Documents live in folders,
and folders control who can see what.

## Supported formats

You can ingest **Markdown (`.md`)**, **plain text (`.txt`)**, **HTML
(`.html`/`.htm`)**, and **Word (`.docx`)**. For a Google Doc, use *File →
Download → Microsoft Word (.docx)* and upload that — headings are preserved. PDF
and direct Google-Drive links are not yet supported.

## Adding a document (upload)

1. Click **"Add doc"** in the header.
2. Pick a file.
3. Choose one or more **destination folders** — only folders you're allowed to
   write to are listed (manual folders at or below your tier).
4. Click **Upload**.

The same file can go into multiple folders; you get one document per destination.
Hippo parses it to clean Markdown, splits it into chunks, optionally enriches each
chunk with a little context, embeds them, and indexes them — after which it's
immediately searchable.

If you upload a newer version of the same file to the same folder, it replaces
the old one (deduplicated by content).

## How folders work

Folders form a **tree**. Every Hippo instance starts with three root folders:

| Root | Tier | Visible to |
|---|---|---|
| **Default** | `user` | everyone |
| **Private** | `admin` | admins and owners |
| **Owner** | `owner` | owners only |

- A folder's **tier** (`user` / `admin` / `owner`) controls who can read the
  documents in it.
- **Child folders inherit their parent's tier** — you don't set tiers per folder,
  you put a folder under the right root.
- The three roots can't be deleted or moved. Admins create child folders under
  them (see [Admin tasks](admin-tasks.md)).

## Who can see what

Visibility is by **role vs. folder tier**:

- a **user** sees `user`-tier folders;
- an **admin** sees `user` + `admin`;
- an **owner** sees everything.

This filter is applied when Hippo retrieves content, so chat answers, MCP
queries, and Slack replies only ever draw from documents you're allowed to read.
You can't get an answer sourced from a folder above your tier.

## Who can upload where

Uploading into a folder requires your role to be at least the folder's tier
**and** the folder to be a *manual* (not filesystem-synced) folder. That's why
the upload modal only lists certain folders. Synced folders are managed from
their source directory, not by manual upload.

## Synced folders (admins)

Admins can mount a directory of files as a **synced folder**: Hippo ingests every
supported file in it and keeps it up to date on re-sync (including removing docs
whose files were deleted). The directory must be inside the server's configured
source-roots allowlist. See [Admin tasks](admin-tasks.md) and, for operators,
[Configuration → sources](../install/configuration.md).

## Browsing what's indexed

Citations in chat link straight to source documents. Admins can see the full
folder tree and document counts in **Settings → Folders**.
