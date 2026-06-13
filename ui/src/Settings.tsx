import { useCallback, useEffect, useState } from "react";
import { flattenTree, type Folder } from "./folders";
import { passwordChangeError } from "./auth";

type Role = "user" | "admin" | "owner";

export function tabsForRole(role: string): string[] {
  if (role === "user") return ["My Profile"];
  const tabs = ["Folders", "Users", "My Profile", "Status"];
  if (role === "owner") tabs.push("System config");
  return tabs;
}

async function getJSON(url: string) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json();
}

export default function Settings({ role, authMode, onClose }: { role: Role; authMode: string; onClose: () => void }) {
  const tabs = tabsForRole(role);
  const [tab, setTab] = useState(tabs[0]);
  return (
    <div className="settings">
      <div className="settings-head">
        <h2>Settings</h2>
        <button onClick={onClose}>← Back to chat</button>
      </div>
      <nav className="settings-tabs">
        {tabs.map((t) => (
          <button key={t} className={t === tab ? "active" : ""} onClick={() => setTab(t)}>{t}</button>
        ))}
      </nav>
      {tab === "My Profile" && (
        <>
          <ProfilePanel />
          {authMode === "password" && <PasswordPanel />}
          <TokensPanel admin={role !== "user"} />
        </>
      )}
      {tab === "Folders" && <FoldersPanel />}
      {tab === "Users" && <UsersPanel authMode={authMode} />}
      {tab === "Status" && <StatusPanel />}
      {tab === "System config" && <InstancePanel />}
    </div>
  );
}

function ProfilePanel() {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [saved, setSaved] = useState("");
  const [note, setNote] = useState("");
  useEffect(() => {
    getJSON("/me").then((m) => { setEmail(m.email); setName(m.name || ""); setSaved(m.name || ""); })
      .catch(() => setNote("couldn't load profile"));
  }, []);
  const save = async () => {
    const r = await fetch("/me", { method: "PATCH",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    if (r.ok) { const b = await r.json(); setSaved(b.name || ""); setName(b.name || ""); setNote("saved"); }
    else setNote(await r.json().then((b) => b.detail).catch(() => `error ${r.status}`));
  };
  return (
    <div className="panel">
      <p>Your profile</p>
      <div className="row">
        <label>Email</label>
        <input value={email} readOnly disabled title="Email is your login identity and can't be changed here" />
      </div>
      <div className="row">
        <label>Name</label>
        <input placeholder="your display name" value={name} onChange={(e) => setName(e.target.value)} />
        <button onClick={save} disabled={name === saved}>Save</button>
        <span className="note">{note}</span>
      </div>
    </div>
  );
}

function TokensPanel({ admin }: { admin: boolean }) {
  const [rows, setRows] = useState<any[]>([]);
  const [name, setName] = useState("");
  const [secret, setSecret] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [note, setNote] = useState("");
  const load = useCallback(() => {
    getJSON(showAll ? "/tokens?all=true" : "/tokens").then(setRows).catch(() => setRows([]));
  }, [showAll]);
  useEffect(() => { load(); }, [load]);
  const create = async () => {
    const r = await fetch("/tokens", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    if (r.ok) { const b = await r.json(); setSecret(b.token); setName(""); setNote(""); load(); }
    else setNote(`couldn't create token (${r.status})`);
  };
  const revoke = async (id: number) => {
    const r = await fetch(`/tokens/${id}`, { method: "DELETE" });
    setNote(r.ok ? "" : `couldn't revoke (${r.status})`);
    load();
  };
  return (
    <div className="panel">
      <p>Personal access tokens for MCP / Slack / CLI. Each token carries your own role.</p>
      <div className="row">
        <input placeholder="name (e.g. laptop)" value={name} onChange={(e) => setName(e.target.value)} />
        <button onClick={create}>Create token</button>
        {admin && <label><input type="checkbox" checked={showAll}
          onChange={(e) => setShowAll(e.target.checked)} /> show all users</label>}
        <span className="note">{note}</span>
      </div>
      {secret && (
        <div className="secret">
          <strong>Copy now — you won't see it again:</strong>
          <code>{secret}</code>
          <button onClick={() => navigator.clipboard?.writeText(secret)}>Copy</button>
          <button onClick={() => setSecret(null)}>Done</button>
        </div>
      )}
      <table><tbody>
        {rows.map((t) => (
          <tr key={t.id}>
            {t.email && <td>{t.email}</td>}
            <td>{t.name || "(unnamed)"}</td>
            <td>{t.last_used_at ? `used ${t.last_used_at}` : "never used"}</td>
            <td><button onClick={() => revoke(t.id)}>Revoke</button></td>
          </tr>
        ))}
      </tbody></table>
    </div>
  );
}

function PasswordPanel() {
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [note, setNote] = useState("");
  const submit = async () => {
    const err = passwordChangeError(cur, next, confirm);
    if (err) { setNote(err); return; }
    const r = await fetch("/me/password", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current: cur, new: next }) });
    if (r.ok) { setNote("password changed"); setCur(""); setNext(""); setConfirm(""); }
    else setNote(await r.json().then((b) => b.detail).catch(() => `error ${r.status}`));
  };
  return (
    <div className="panel">
      <p>Change your password</p>
      <div className="row">
        <input type="password" placeholder="current" value={cur} onChange={(e) => setCur(e.target.value)} />
        <input type="password" placeholder="new" value={next} onChange={(e) => setNext(e.target.value)} />
        <input type="password" placeholder="confirm" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        <button onClick={submit}>Update</button>
        <span className="note">{note}</span>
      </div>
    </div>
  );
}

function FoldersPanel() {
  const [rows, setRows] = useState<Folder[]>([]);
  const [note, setNote] = useState("");
  const [name, setName] = useState("");
  const [parent, setParent] = useState<number | "">("");
  const load = useCallback(() => {
    getJSON("/folders").then(setRows).catch(() => setRows([]));
  }, []);
  useEffect(() => { load(); }, [load]);
  const create = async () => {
    if (parent === "" || !name.trim()) return;
    const r = await fetch("/folders", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parent_id: parent, name }) });
    if (r.ok) { setName(""); load(); setNote(""); }
    else setNote(await r.json().then((b) => b.detail).catch(() => `error ${r.status}`));
  };
  const rename = async (id: number, current: string) => {
    const next = window.prompt("New name", current);
    if (!next) return;
    const r = await fetch(`/folders/${id}`, { method: "PATCH",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: next }) });
    setNote(r.ok ? "" : `error ${r.status}`); load();
  };
  const del = async (id: number) => {
    const r = await fetch(`/folders/${id}`, { method: "DELETE" });
    setNote(r.ok ? "" : await r.json().then((b) => b.detail).catch(() => `error ${r.status}`));
    load();
  };
  const resync = async (id: number) => {
    setNote("syncing…");
    const r = await fetch(`/folders/${id}/resync`, { method: "POST" });
    setNote(r.ok ? "synced" : `error ${r.status}`); load();
  };
  const flat = flattenTree(rows);
  return (
    <div className="panel">
      <div className="row">
        <select value={parent} onChange={(e) => setParent(e.target.value ? Number(e.target.value) : "")}>
          <option value="">parent folder…</option>
          {flat.filter((f) => f.writable).map((f) => (
            <option key={f.id} value={f.id}>{" ".repeat(f.depth * 2) + f.name}</option>
          ))}
        </select>
        <input placeholder="new subfolder name" value={name} onChange={(e) => setName(e.target.value)} />
        <button onClick={create}>Create</button>
        <span className="note">{note}</span>
      </div>
      <table><tbody>
        {flat.map((f) => (
          <tr key={f.id}>
            <td style={{ paddingLeft: f.depth * 16 }}>
              {f.name} <span className="sec">{f.tier}</span>
              {f.origin !== "manual" && <span className="sec"> · synced ({f.origin})</span>}
            </td>
            <td>{f.doc_count} docs</td>
            <td>
              {f.parent_id !== null && <button onClick={() => rename(f.id, f.name)}>Rename</button>}
              {f.origin === "folder" && <button onClick={() => resync(f.id)}>Re-sync</button>}
              {f.parent_id !== null && <button onClick={() => del(f.id)}>Delete</button>}
            </td>
          </tr>
        ))}
      </tbody></table>
    </div>
  );
}

function UsersPanel({ authMode }: { authMode: string }) {
  const [rows, setRows] = useState<any[]>([]);
  const [note, setNote] = useState("");
  const [secret, setSecret] = useState<{ email: string; pw: string; verb: string } | null>(null);
  const [newEmail, setNewEmail] = useState("");
  const [newName, setNewName] = useState("");
  const [newRole, setNewRole] = useState("user");
  const load = useCallback(() => { getJSON("/users").then(setRows).catch(() => setRows([])); }, []);
  useEffect(() => { load(); }, [load]);
  const setRole = async (email: string, role: string) => {
    const r = await fetch(`/users/${encodeURIComponent(email)}/role`, { method: "PUT",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ role }) });
    if (!r.ok) {
      const detail = await r.json().then((b) => b.detail).catch(() => `error ${r.status}`);
      setNote(detail);
    } else setNote("");
    load();   // reload reverts the dropdown if the change was refused
  };
  const create = async () => {
    const r = await fetch("/users", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: newEmail.trim(), role: newRole, name: newName.trim() }) });
    if (r.ok) {
      const b = await r.json();
      setNote("");
      setNewEmail(""); setNewName(""); setNewRole("user");
      if (b.password) setSecret({ email: b.email, pw: b.password, verb: "Initial password for" });
      load();
    } else setNote(await r.json().then((b) => b.detail).catch(() => `error ${r.status}`));
  };
  const reset = async (email: string) => {
    const r = await fetch(`/users/${encodeURIComponent(email)}/password`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}" });
    if (r.ok) { const b = await r.json(); setSecret({ email, pw: b.password, verb: "Password reset for" }); }
    else setNote(await r.json().then((b) => b.detail).catch(() => `error ${r.status}`));
  };
  if (secret) {
    return (
      <div className="panel">
        <p>{secret.verb} <strong>{secret.email}</strong> — copy now, it won't be shown again:</p>
        <div className="secret">
          <code>{secret.pw}</code>
          <button onClick={() => navigator.clipboard?.writeText(secret.pw)}>Copy</button>
          <button onClick={() => setSecret(null)}>Done</button>
        </div>
      </div>
    );
  }
  return (
    <div className="panel">
      <div className="row">
        <input placeholder="new user email" value={newEmail} onChange={(e) => setNewEmail(e.target.value)} />
        <input placeholder="name (optional)" value={newName} onChange={(e) => setNewName(e.target.value)} />
        <select value={newRole} onChange={(e) => setNewRole(e.target.value)}>
          {["user", "admin", "owner"].map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
        <button onClick={create} disabled={!newEmail.trim()}>Create user</button>
      </div>
      <span className="note">{note}</span>
      <table><tbody>
        {rows.map((u) => (
          <tr key={u.email}><td>{u.email}</td>
            <td><select value={u.role} onChange={(e) => setRole(u.email, e.target.value)}>
              {["user", "admin", "owner"].map((r) => <option key={r} value={r}>{r}</option>)}
            </select></td>
            {authMode === "password" && <td><button onClick={() => reset(u.email)}>Reset password</button></td>}
          </tr>
        ))}
      </tbody></table>
    </div>
  );
}

function StatusPanel() {
  const [s, setS] = useState<any>(null);
  useEffect(() => { getJSON("/settings/status").then(setS).catch(() => setS(null)); }, []);
  if (!s) return <div className="panel">Loading…</div>;
  return (
    <div className="panel status">
      <dl>
        <dt>Auth mode</dt><dd>{s.auth_mode}</dd>
        <dt>Chat model</dt><dd>{s.chat_model}</dd>
        <dt>Embedding model</dt><dd>{s.embedding_model}</dd>
        <dt>MCP</dt><dd>{String(s.mcp_enabled)}</dd>
        <dt>Slack</dt><dd>{String(s.slack_enabled)}</dd>
        <dt>Counts</dt><dd>{s.counts.documents} docs · {s.counts.folders} folders · {s.counts.users} users</dd>
      </dl>
    </div>
  );
}

function InstancePanel() {
  const [cfg, setCfg] = useState<Record<string, any> | null>(null);
  const [note, setNote] = useState("");
  useEffect(() => { getJSON("/config").then(setCfg).catch(() => setCfg(null)); }, []);
  if (!cfg) return <div className="panel">Loading…</div>;
  const save = async (patch: Record<string, any>) => {
    const r = await fetch("/config", { method: "PUT",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
    setNote(r.ok ? "saved (model changes are live; auth-mode/embedding need a restart)"
                 : await r.json().then((b) => b.detail).catch(() => `error ${r.status}`));
    if (r.ok) getJSON("/config").then(setCfg);
  };
  return (
    <div className="panel">
      <div className="row"><label>Chat model</label>
        <input defaultValue={cfg.chat_model}
          onBlur={(e) => e.target.value !== cfg.chat_model && save({ chat_model: e.target.value })} /></div>
      <div className="row"><label>Enrich model</label>
        <input defaultValue={cfg.enrich_model}
          onBlur={(e) => e.target.value !== cfg.enrich_model && save({ enrich_model: e.target.value })} /></div>
      <div className="row"><label>Embedding</label>
        <span className="sec">{cfg.embedding_model} / dim {cfg.embedding_dim} — change via <code>hippo reindex</code></span></div>
      <div className="row"><label>Auth mode</label>
        <select defaultValue={cfg.auth_mode} onChange={(e) => save({ auth_mode: e.target.value })}>
          {["password", "oidc", "iap"].map((m) => <option key={m} value={m}>{m}</option>)}
        </select></div>
      <span className="note">{note}</span>
    </div>
  );
}
