import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { DocDrawer } from "./DocDrawer";
import {
  buildDocIndex,
  type DocIndex,
  type DocMeta,
  MARKER_RE,
  processCitations,
  stripNoSourcesMarker,
} from "./citations";
import Settings from "./Settings";
import { flattenTree, writableFolders, uploadReducer, type Folder } from "./folders";
import {
  WIZARD_STEPS,
  buildSetupPayload,
  nextStep,
  stepValid,
  type SetupState,
} from "./setup";

type OpenDoc = { id: number; section: string };

const INIT_SETUP: SetupState = {
  step: 0,
  token: "",
  authMode: "password",
  ownerEmail: "",
  ownerPassword: "",
  roots: { user: "Default", admin: "Private", owner: "Owner" },
  models: { chat_model: "", embedding_model: "", embedding_dim: 1536 },
};

function SetupWizard() {
  const [state, setState] = useState<SetupState>(INIT_SETUP);
  const [submitErr, setSubmitErr] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const step = WIZARD_STEPS[state.step];
  const isFirst = state.step === 0;
  const isLast = state.step === WIZARD_STEPS.length - 1;
  const canNext = stepValid(state);

  function back() {
    if (state.step > 0) setState((s) => ({ ...s, step: s.step - 1 }));
  }

  function advance() {
    setState((s) => nextStep(s));
  }

  async function finish() {
    setSubmitErr("");
    setSubmitting(true);
    try {
      const r = await fetch("/setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildSetupPayload(state)),
      });
      if (r.ok) {
        window.location.reload();
      } else {
        const b = await r.json().catch(() => ({}));
        setSubmitErr(b.detail || `Setup failed (${r.status})`);
        setSubmitting(false);
      }
    } catch {
      setSubmitErr("Network error — is the server running?");
      setSubmitting(false);
    }
  }

  return (
    <div className="app">
      <div className="empty signin">
        <span className="logo">{"\u{1F99B}"}</span>
        <h1>Hippo — First-run Setup</h1>
        <p className="tagline">
          Step {state.step + 1} of {WIZARD_STEPS.length}: <strong>{step}</strong>
        </p>

        <div style={{ width: "100%", maxWidth: 420, textAlign: "left" }}>
          {step === "token" && (
            <div>
              <p>Enter the setup token (from <code>HIPPO_SETUP_TOKEN</code> env or logged at startup).</p>
              <input
                type="text"
                placeholder="Setup token"
                value={state.token}
                autoFocus
                style={{ width: "100%", marginBottom: 8 }}
                onChange={(e) => setState((s) => ({ ...s, token: e.target.value }))}
              />
            </div>
          )}

          {step === "auth" && (
            <div>
              <p>Choose the authentication mode for this Hippo instance.</p>
              <select
                value={state.authMode}
                style={{ width: "100%", marginBottom: 8 }}
                onChange={(e) =>
                  setState((s) => ({ ...s, authMode: e.target.value as SetupState["authMode"] }))
                }
              >
                <option value="password">Password (email + password)</option>
                <option value="oidc">OIDC / Google (OAuth2)</option>
                <option value="iap">IAP (Google Cloud Identity-Aware Proxy)</option>
              </select>
            </div>
          )}

          {step === "owner" && (
            <div>
              <p>Create the owner account.</p>
              <input
                type="email"
                placeholder="Owner email"
                value={state.ownerEmail}
                autoFocus
                style={{ width: "100%", marginBottom: 8 }}
                onChange={(e) => setState((s) => ({ ...s, ownerEmail: e.target.value }))}
              />
              {state.authMode === "password" && (
                <input
                  type="password"
                  placeholder="Owner password (min 8 chars)"
                  value={state.ownerPassword}
                  style={{ width: "100%", marginBottom: 8 }}
                  onChange={(e) => setState((s) => ({ ...s, ownerPassword: e.target.value }))}
                />
              )}
            </div>
          )}

          {step === "roots" && (
            <div>
              <p>Name the three access-tier root folders.</p>
              {(["user", "admin", "owner"] as const).map((tier) => (
                <div key={tier} style={{ marginBottom: 8 }}>
                  <label style={{ display: "block", fontWeight: 600, marginBottom: 2 }}>
                    {tier} tier
                  </label>
                  <input
                    type="text"
                    value={state.roots[tier]}
                    style={{ width: "100%" }}
                    onChange={(e) =>
                      setState((s) => ({ ...s, roots: { ...s.roots, [tier]: e.target.value } }))
                    }
                  />
                </div>
              ))}
            </div>
          )}

          {step === "models" && (
            <div>
              <p>Configure models (optional — leave blank to use server defaults).</p>
              <div style={{ marginBottom: 8 }}>
                <label style={{ display: "block", fontWeight: 600, marginBottom: 2 }}>
                  Chat model
                </label>
                <input
                  type="text"
                  placeholder="e.g. ollama:llama3 or openai:gpt-4o"
                  value={state.models.chat_model}
                  style={{ width: "100%" }}
                  onChange={(e) =>
                    setState((s) => ({ ...s, models: { ...s.models, chat_model: e.target.value } }))
                  }
                />
              </div>
              <div style={{ marginBottom: 8 }}>
                <label style={{ display: "block", fontWeight: 600, marginBottom: 2 }}>
                  Embedding model
                </label>
                <input
                  type="text"
                  placeholder="e.g. openai:text-embedding-3-small"
                  value={state.models.embedding_model}
                  style={{ width: "100%" }}
                  onChange={(e) =>
                    setState((s) => ({
                      ...s,
                      models: { ...s.models, embedding_model: e.target.value },
                    }))
                  }
                />
              </div>
              <div style={{ marginBottom: 8 }}>
                <label style={{ display: "block", fontWeight: 600, marginBottom: 2 }}>
                  Embedding dimension
                </label>
                <input
                  type="number"
                  value={state.models.embedding_dim}
                  style={{ width: "100%" }}
                  onChange={(e) =>
                    setState((s) => ({
                      ...s,
                      models: { ...s.models, embedding_dim: Number(e.target.value) },
                    }))
                  }
                />
              </div>
            </div>
          )}

          {step === "finish" && (
            <div>
              <p>Everything looks good. Click <strong>Finish</strong> to complete setup.</p>
              <ul style={{ marginBottom: 8 }}>
                <li>Auth mode: <strong>{state.authMode}</strong></li>
                <li>Owner: <strong>{state.ownerEmail}</strong></li>
                <li>Folders: {state.roots.user} / {state.roots.admin} / {state.roots.owner}</li>
              </ul>
            </div>
          )}

          {submitErr && <p className="error">{submitErr}</p>}

          <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
            {!isFirst && (
              <button className="upload-btn" onClick={back} disabled={submitting}>
                Back
              </button>
            )}
            {!isLast && (
              <button className="upload-btn" onClick={advance} disabled={!canNext}>
                Next
              </button>
            )}
            {isLast && (
              <button className="upload-btn" onClick={finish} disabled={submitting}>
                {submitting ? "Setting up…" : "Finish"}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

type Me = { email: string; role: string; auth_mode: string };

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

function AssistantText({
  text,
  docIndex,
  onOpen,
}: {
  text: string;
  docIndex: DocIndex;
  onOpen: (id: number, section: string) => void;
}) {
  const { text: grounded, refused } = useMemo(() => stripNoSourcesMarker(text), [text]);
  const { processed, sources } = useMemo(
    () => processCitations(grounded, docIndex),
    [grounded, docIndex],
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
      {sources.length === 0 && !refused && processed.trim().length > 120 && (
        <p className="no-sources" title="Hippo should answer only from indexed docs with citations.">
          ⚠ No sources cited — verify independently.
        </p>
      )}
    </div>
  );
}

export default function App() {
  const { messages, sendMessage, status, error } = useChat({
    transport: new DefaultChatTransport({ api: "/chat" }),
  });
  const [input, setInput] = useState("");
  const [docIndex, setDocIndex] = useState<DocIndex>(new Map());
  const [openDoc, setOpenDoc] = useState<OpenDoc | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [needsLogin, setNeedsLogin] = useState(false);
  const [setupComplete, setSetupComplete] = useState<boolean | null>(null);
  const [view, setView] = useState<"chat" | "settings">("chat");
  const [folders, setFolders] = useState<Folder[]>([]);
  const [showUpload, setShowUpload] = useState(false);
  const [up, dispatchUp] = useReducer(uploadReducer,
    { status: "idle", file: null, dests: [], done: 0, error: null });
  const [pickFile, setPickFile] = useState<File | null>(null);
  const [picked, setPicked] = useState<number[]>([]);
  const bottomRef = useRef<HTMLDivElement>(null);
  const [authMode, setAuthMode] = useState<string>("none");
  const [loginEmail, setLoginEmail] = useState("");
  const [loginPw, setLoginPw] = useState("");
  const [loginErr, setLoginErr] = useState("");

  useEffect(() => {
    fetch("/setup/status")
      .then((r) => r.json())
      .then((s) => setSetupComplete(s.setup_complete === true))
      .catch(() => setSetupComplete(true)); // if endpoint absent, assume complete
  }, []);

  const refreshDocs = useCallback(() => {
    fetch("/documents")
      .then((r) => r.json())
      .then((docs: DocMeta[]) => setDocIndex(buildDocIndex(docs)))
      .catch(() => {});
  }, []);

  const refreshFolders = useCallback(() => {
    fetch("/folders").then((r) => r.json()).then(setFolders).catch(() => {});
  }, []);

  useEffect(() => {
    refreshDocs();
  }, [refreshDocs]);

  useEffect(() => { refreshFolders(); }, [refreshFolders]);

  useEffect(() => {
    fetch("/me").then((r) => {
      if (r.status === 401) setNeedsLogin(true);
      else if (r.ok) r.json().then(setMe);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    fetch("/auth/config").then((r) => r.json()).then((c) => setAuthMode(c.auth_mode)).catch(() => {});
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages, status]);

  const onOpen = useCallback((id: number, section: string) => setOpenDoc({ id, section }), []);

  async function passwordLogin(e: React.FormEvent) {
    e.preventDefault();
    setLoginErr("");
    const r = await fetch("/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: loginEmail, password: loginPw }),
    });
    if (r.ok) window.location.reload();
    else setLoginErr((await r.json().catch(() => ({}))).detail || "Sign-in failed.");
  }

  async function runUpload() {
    if (!pickFile || picked.length === 0) return;
    dispatchUp({ type: "start", file: pickFile, dests: picked });
    const form = new FormData();
    form.append("file", pickFile);
    for (const id of picked) form.append("folder_ids", String(id));
    const res = await fetch("/ingest", { method: "POST", body: form });
    if (!res.ok) {
      const b = await res.json().catch(() => ({ detail: `error ${res.status}` }));
      dispatchUp({ type: "error", error: b.detail });
      return;
    }
    // server ingests into every destination; advance the bar to done
    for (let i = 0; i < picked.length; i++) dispatchUp({ type: "progress" });
    refreshDocs();
  }

  // Setup wizard takes precedence over the login screen.
  if (setupComplete === false) {
    return <SetupWizard />;
  }

  if (needsLogin) {
    return (
      <div className="app">
        <div className="empty signin">
          <span className="logo">{"\u{1F99B}"}</span>
          <h1>Hippo</h1>
          {authMode === "password" ? (
            <form className="login-form" onSubmit={passwordLogin}>
              <input type="email" placeholder="email" value={loginEmail} autoFocus
                onChange={(e) => setLoginEmail(e.target.value)} />
              <input type="password" placeholder="password" value={loginPw}
                onChange={(e) => setLoginPw(e.target.value)} />
              <button className="upload-btn" type="submit">Sign in</button>
              {loginErr && <p className="error">{loginErr}</p>}
            </form>
          ) : (
            <>
              <p>Sign in with your Google account to continue.</p>
              <a className="upload-btn" href="/auth/login">Sign in with Google</a>
            </>
          )}
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
              {me.auth_mode === "password" && <> · <button className="linklike"
                onClick={async () => { await fetch("/auth/logout", { method: "POST" }); window.location.reload(); }}>sign out</button></>}
            </span>
          )}
          {me && (
            <button className="gear" title="Settings" onClick={() => setView("settings")}>⚙</button>
          )}
          <button className="upload-btn" onClick={() => { setShowUpload(true); dispatchUp({ type: "reset" }); }}>
            Add doc
          </button>
        </div>
      </header>

      {showUpload && (
        <div className="modal-backdrop" onClick={() => setShowUpload(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>Add a document</h3>
            <input type="file" accept=".md,.markdown,.txt,.html,.htm,.docx"
              onChange={(e) => setPickFile(e.target.files?.[0] ?? null)} />
            <p>Destination folders</p>
            <div className="dest-list">
              {flattenTree(writableFolders(folders)).map((f) => (
                <label key={f.id} style={{ paddingLeft: f.depth * 12 }}>
                  <input type="checkbox" checked={picked.includes(f.id)}
                    onChange={(e) => setPicked((p) =>
                      e.target.checked ? [...p, f.id] : p.filter((x) => x !== f.id))} />
                  {f.name} <span className="sec">{f.tier}</span>
                </label>
              ))}
            </div>
            {up.status === "uploading" && <p>Uploading… {up.done}/{up.dests.length}</p>}
            {up.status === "error" && <p className="error">{up.error}</p>}
            {up.status === "done"
              ? <button onClick={() => setShowUpload(false)}>Done</button>
              : <button disabled={!pickFile || picked.length === 0 || up.status === "uploading"}
                  onClick={runUpload}>Upload</button>}
          </div>
        </div>
      )}

      {view === "settings" && me ? (
        <Settings role={me.role as "user" | "admin" | "owner"} authMode={authMode} onClose={() => setView("chat")} />
      ) : (
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

          {openDoc && (
            <DocDrawer docId={openDoc.id} section={openDoc.section} onClose={() => setOpenDoc(null)} />
          )}
        </>
      )}
    </div>
  );
}
