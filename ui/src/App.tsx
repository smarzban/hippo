import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { DocDrawer } from "./DocDrawer";
import { buildDocIndex, type DocIndex, type DocMeta, MARKER_RE, processCitations } from "./citations";

type OpenDoc = { id: number; section: string };

type Me = {
  email: string;
  role: string;
  auth_mode: string;
  upload: { team_repo: boolean; managers_repo: boolean };
};

function toolLabel(name: string | undefined, input: unknown): string {
  const i = (input ?? {}) as Record<string, unknown>;
  switch (name) {
    case "search":
      return `Searching “${i.query ?? "…"}”`;
    case "read_document":
      return `Reading document #${i.doc_id ?? "…"}`;
    case "list_documents":
      return i.query ? `Listing documents matching “${i.query}”` : "Listing documents";
    case "grep":
      return `Scanning sources for ${i.pattern ?? "…"}`;
    default:
      return name ?? "working";
  }
}

const SUGGESTIONS = [
  "What docs do you have in there?",
  "How does Polly integrate with Telegram?",
  "Why did we do Project X?",
];

function AssistantText({
  text,
  docIndex,
  onOpen,
}: {
  text: string;
  docIndex: DocIndex;
  onOpen: (id: number, section: string) => void;
}) {
  const { processed, sources } = useMemo(
    () => processCitations(text, docIndex),
    [text, docIndex],
  );

  const components = useMemo(
    () => ({
      code({ className, children, ...props }: React.ComponentProps<"code">) {
        const m = MARKER_RE.exec(String(children));
        if (m) {
          const num = Number(m[1]);
          const src = sources.find((s) => s.num === num);
          const clickable = !!src && src.docId != null;
          return (
            <sup
              className={`cite-ref${clickable ? " clickable" : ""}`}
              title={src ? src.title + (src.section ? ` › ${src.section}` : "") : ""}
              onClick={clickable ? () => onOpen(src!.docId!, src!.scrollTarget) : undefined}
            >
              {num}
            </sup>
          );
        }
        return (
          <code className={className} {...props}>
            {children}
          </code>
        );
      },
    }),
    [sources, onOpen],
  );

  return (
    <div className="md">
      <Markdown remarkPlugins={[remarkGfm]} components={components}>
        {processed}
      </Markdown>
      {sources.length > 0 && (
        <div className="sources">
          <span className="sources-label">Sources</span>
          <ol>
            {sources.map((s) => (
              <li key={s.num}>
                {s.docId != null ? (
                  <button className="source-link" onClick={() => onOpen(s.docId!, s.scrollTarget)}>
                    {s.title}
                    {s.section && <span className="sec"> › {s.section}</span>}
                  </button>
                ) : (
                  <span className="source-dead">
                    {s.title}
                    {s.section && ` › ${s.section}`}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}

export default function App() {
  const { messages, sendMessage, status, error } = useChat({
    transport: new DefaultChatTransport({ api: "/chat" }),
  });
  const [input, setInput] = useState("");
  const [uploadNote, setUploadNote] = useState("");
  const [docIndex, setDocIndex] = useState<DocIndex>(new Map());
  const [openDoc, setOpenDoc] = useState<OpenDoc | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [needsLogin, setNeedsLogin] = useState(false);
  const [uploadRepo, setUploadRepo] = useState("team");
  const bottomRef = useRef<HTMLDivElement>(null);

  const refreshDocs = useCallback(() => {
    fetch("/documents")
      .then((r) => r.json())
      .then((docs: DocMeta[]) => setDocIndex(buildDocIndex(docs)))
      .catch(() => {});
  }, []);

  useEffect(() => {
    refreshDocs();
  }, [refreshDocs]);

  useEffect(() => {
    fetch("/me").then((r) => {
      if (r.status === 401) setNeedsLogin(true);
      else if (r.ok) r.json().then(setMe);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages, status]);

  const onOpen = useCallback((id: number, section: string) => setOpenDoc({ id, section }), []);

  async function upload(file: File) {
    const form = new FormData();
    form.append("file", file);
    form.append("repo", uploadRepo);
    setUploadNote(`adding ${file.name}…`);
    const res = await fetch("/ingest", { method: "POST", body: form });
    const body = await res.json();
    if (!res.ok) {
      setUploadNote(`failed: ${body.detail}`);
    } else if (body.status === "committed") {
      setUploadNote(`committed ${file.name} to ${body.repo} — searchable after the next sync`);
    } else {
      setUploadNote(`added ${file.name} (unversioned) — ${body.chunks} chunks`);
      refreshDocs();
    }
  }

  if (needsLogin) {
    return (
      <div className="app">
        <div className="empty signin">
          <span className="logo">{"\u{1F99B}"}</span>
          <h1>Hippo</h1>
          <p>Sign in with your Google account to continue.</p>
          <a className="upload-btn" href="/auth/login">Sign in with Google</a>
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <header>
        <div className="brand">
          <span className="logo">{"\u{1F99B}"}</span>
          <div>
            <h1>Hippo</h1>
            <p className="tagline">your team's memory</p>
          </div>
        </div>
        <div className="upload">
          {me && me.auth_mode !== "none" && (
            <span className="whoami">
              {me.email} ({me.role})
              {me.auth_mode === "oidc" && <> · <a href="/auth/logout">sign out</a></>}
            </span>
          )}
          {me?.upload.managers_repo && (
            <select value={uploadRepo} onChange={(e) => setUploadRepo(e.target.value)}>
              <option value="team">team docs</option>
              <option value="managers">managers docs</option>
            </select>
          )}
          <label className="upload-btn">
            Add doc
            <input
              type="file"
              hidden
              accept=".md,.markdown,.txt,.html,.htm"
              onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
            />
          </label>
          <span className="note">{uploadNote}</span>
        </div>
      </header>

      <main>
        {messages.length === 0 && (
          <div className="empty">
            <p>Ask me anything that lives in the team docs.</p>
            <div className="suggestions">
              {SUGGESTIONS.map((s) => (
                <button key={s} onClick={() => sendMessage({ text: s })}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>
            {m.parts.map((part, i) => {
              if (part.type === "text") {
                return m.role === "user" ? (
                  <p key={i} className="user-text">
                    {part.text}
                  </p>
                ) : (
                  <AssistantText key={i} text={part.text} docIndex={docIndex} onOpen={onOpen} />
                );
              }
              if (part.type === "reasoning" && part.text.trim()) {
                return (
                  <details key={i} className="reasoning">
                    <summary>thinking</summary>
                    <p>{part.text}</p>
                  </details>
                );
              }
              if (part.type.startsWith("tool-") || part.type === "dynamic-tool") {
                const p = part as {
                  type: string;
                  state?: string;
                  input?: unknown;
                  toolName?: string;
                };
                const name =
                  part.type === "dynamic-tool" ? p.toolName : part.type.replace("tool-", "");
                const pending = p.state !== "output-available" && p.state !== "output-error";
                return (
                  <div key={i} className={`tool ${pending ? "pending" : "done"}`}>
                    {toolLabel(name, p.input)}
                  </div>
                );
              }
              return null;
            })}
          </div>
        ))}

        {status === "submitted" && <div className="thinking">&bull;&bull;&bull;</div>}
        {error && (
          <div className="error">
            {/limit/i.test(error.message)
              ? "I reached my research limit for this question — it needed more lookups than I'm allowed per answer. Try narrowing it, or ask about one thing at a time."
              : `Something went wrong: ${error.message}`}
          </div>
        )}
        <div ref={bottomRef} />
      </main>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!input.trim()) return;
          sendMessage({ text: input });
          setInput("");
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask Hippo…"
          autoFocus
        />
        <button type="submit" disabled={status !== "ready"}>
          Send
        </button>
      </form>

      {openDoc && (
        <DocDrawer docId={openDoc.id} section={openDoc.section} onClose={() => setOpenDoc(null)} />
      )}
    </div>
  );
}
