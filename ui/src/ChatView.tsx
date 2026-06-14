import type { useChat } from "@ai-sdk/react";
import AssistantText from "./AssistantText";
import { type DocIndex } from "./citations";

function toolLabel(name: string | undefined, input: unknown): string {
  const i = (input ?? {}) as Record<string, unknown>;
  switch (name) {
    case "search":
      return `Searching "${i.query ?? "…"}"`;
    case "read_document":
      return `Reading document #${i.doc_id ?? "…"}`;
    case "list_documents":
      return i.query ? `Listing documents matching "${i.query}"` : "Listing documents";
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

type ChatApi = ReturnType<typeof useChat>;

type Props = {
  messages: ChatApi["messages"];
  sendMessage: ChatApi["sendMessage"];
  status: ChatApi["status"];
  error: ChatApi["error"];
  input: string;
  setInput: (v: string) => void;
  docIndex: DocIndex;
  onOpen: (id: number, section: string) => void;
  bottomRef: React.RefObject<HTMLDivElement | null>;
};

/** The chat surface: empty-state suggestions, the message transcript (user text,
 * assistant text via AssistantText, reasoning, and tool-progress lines), the
 * thinking/error indicators, and the composer form. */
export default function ChatView({
  messages, sendMessage, status, error, input, setInput, docIndex, onOpen, bottomRef,
}: Props) {
  return (
    <>
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
    </>
  );
}
