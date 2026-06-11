import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Matches both [path > section] and the fullwidth 【path > section】 some models emit.
const CITE_RE = /【([^】]+)】|\[([^\][\n]+? > [^\][\n]+?)\]/g;

/** Turn citations into inline-code chips (rendered specially below) and trim
 *  absolute paths down to the filename. */
function prepMarkdown(text: string): string {
  return text.replace(CITE_RE, (_m, a, b) => {
    const inner = (a ?? b ?? "").trim();
    const [path, ...sections] = inner.split(" > ");
    const file = path.split("/").pop() || path;
    return "`\u{1F4C4} " + [file, ...sections].join(" › ") + "`";
  });
}

const mdComponents = {
  code({ className, children, ...props }: React.ComponentProps<"code">) {
    const txt = String(children);
    if (txt.startsWith("\u{1F4C4} ")) {
      return <span className="cite" title={txt.slice(3)}>{txt.slice(3)}</span>;
    }
    return (
      <code className={className} {...props}>
        {children}
      </code>
    );
  },
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

export default function App() {
  const { messages, sendMessage, status, error } = useChat({
    transport: new DefaultChatTransport({ api: "/chat" }),
  });
  const [input, setInput] = useState("");
  const [uploadNote, setUploadNote] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages, status]);

  async function upload(file: File) {
    const form = new FormData();
    form.append("file", file);
    setUploadNote(`adding ${file.name}…`);
    const res = await fetch("/ingest", { method: "POST", body: form });
    const body = await res.json();
    setUploadNote(
      res.ok ? `added ${file.name} — ${body.chunks} chunks` : `failed: ${body.detail}`,
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
                  <p key={i} className="user-text">{part.text}</p>
                ) : (
                  <div key={i} className="md">
                    <Markdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                      {prepMarkdown(part.text)}
                    </Markdown>
                  </div>
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
                const p = part as { type: string; state?: string; input?: unknown; toolName?: string };
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
        {error && <div className="error">Something went wrong: {error.message}</div>}
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
    </div>
  );
}
