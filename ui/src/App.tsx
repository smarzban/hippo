import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import { useRef, useState } from "react";

export default function App() {
  const { messages, sendMessage, status } = useChat({
    transport: new DefaultChatTransport({ api: "/chat" }),
  });
  const [input, setInput] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploadNote, setUploadNote] = useState("");

  async function upload(file: File) {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch("/ingest", { method: "POST", body: form });
    const body = await res.json();
    setUploadNote(
      res.ok ? `added ${file.name} — ${body.chunks} chunks` : `failed: ${body.detail}`,
    );
  }

  return (
    <div className="app">
      <header>
        <h1>Knowledge Hub</h1>
        <div className="upload">
          <input
            type="file"
            ref={fileRef}
            accept=".md,.txt,.html"
            onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
          />
          <span className="note">{uploadNote}</span>
        </div>
      </header>

      <main>
        {messages.map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>
            {m.parts.map((part, i) => {
              if (part.type === "text") return <p key={i}>{part.text}</p>;
              if (part.type.startsWith("tool-") || part.type === "dynamic-tool") {
                const p = part as { type: string; state?: string; input?: unknown };
                const name = part.type === "dynamic-tool"
                  ? (part as { toolName?: string }).toolName
                  : part.type.replace("tool-", "");
                return (
                  <div key={i} className="tool">
                    ⚙ {name}({p.input ? JSON.stringify(p.input) : ""}) — {p.state ?? "…"}
                  </div>
                );
              }
              return null;
            })}
          </div>
        ))}
        {status === "streaming" && <div className="thinking">…</div>}
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
          placeholder="Ask the knowledge hub…"
          autoFocus
        />
        <button type="submit" disabled={status !== "ready"}>
          Send
        </button>
      </form>
    </div>
  );
}
