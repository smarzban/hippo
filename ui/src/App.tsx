import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { DocDrawer } from "./DocDrawer";
import { buildDocIndex, type DocIndex, type DocMeta } from "./citations";
import Settings from "./Settings";
import { errorDetail } from "./auth";
import { uploadReducer, type Folder } from "./folders";
import SetupWizard from "./SetupWizard";
import LoginScreen from "./LoginScreen";
import UploadModal from "./UploadModal";
import ChatView from "./ChatView";
import { chatHistoryKey, clearMessages, loadMessages, saveMessages } from "./chatHistory";

type OpenDoc = { id: number; section: string };

type Me = { email: string; role: string; auth_mode: string; name: string };

export default function App() {
  // Seed the transcript from device-local storage so a refresh keeps the
  // current conversation. The signed-in user isn't known at first render, so
  // we seed from the default key here and re-key to the user's history once
  // /me resolves (see the effect below). Persistence logic lives in
  // chatHistory.ts; App just calls it.
  const { messages, sendMessage, status, error, setMessages } = useChat({
    transport: new DefaultChatTransport({ api: "/chat" }),
    messages: loadMessages(localStorage, chatHistoryKey(null)),
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

  // Once we know who's signed in, swap the seeded "default" transcript for that
  // user's own history (avoids one user's chat bleeding into another's on a
  // shared browser). Only restores if the user-specific key holds something.
  const meEmail = me?.email ?? null;
  useEffect(() => {
    if (!meEmail) return;
    const restored = loadMessages(localStorage, chatHistoryKey(meEmail));
    if (restored.length > 0) setMessages(restored);
  }, [meEmail, setMessages]);

  // Persist the transcript on every change, namespaced by the signed-in user.
  useEffect(() => {
    saveMessages(localStorage, chatHistoryKey(meEmail), messages);
  }, [messages, meEmail]);

  const newChat = useCallback(() => {
    clearMessages(localStorage, chatHistoryKey(meEmail));
    setMessages([]);
  }, [meEmail, setMessages]);

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
    else setLoginErr(await errorDetail(r, "Sign-in failed."));
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
      <LoginScreen
        authMode={authMode}
        email={loginEmail}
        setEmail={setLoginEmail}
        password={loginPw}
        setPassword={setLoginPw}
        error={loginErr}
        onSubmit={passwordLogin}
      />
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
          {messages.length > 0 && (
            <button className="upload-btn" title="Start a fresh conversation" onClick={newChat}>
              New chat
            </button>
          )}
          <button className="upload-btn" onClick={() => { setShowUpload(true); dispatchUp({ type: "reset" }); }}>
            Add doc
          </button>
        </div>
      </header>

      {showUpload && (
        <UploadModal
          folders={folders}
          picked={picked}
          setPicked={setPicked}
          pickFile={pickFile}
          setPickFile={setPickFile}
          up={up}
          onUpload={runUpload}
          onClose={() => setShowUpload(false)}
        />
      )}

      {view === "settings" && me ? (
        <Settings role={me.role as "user" | "admin" | "owner"} authMode={authMode} onClose={() => setView("chat")} />
      ) : (
        <>
          <ChatView
            messages={messages}
            sendMessage={sendMessage}
            status={status}
            error={error}
            input={input}
            setInput={setInput}
            docIndex={docIndex}
            onOpen={onOpen}
            bottomRef={bottomRef}
          />

          {openDoc && (
            <DocDrawer docId={openDoc.id} section={openDoc.section} onClose={() => setOpenDoc(null)} />
          )}
        </>
      )}
    </div>
  );
}
