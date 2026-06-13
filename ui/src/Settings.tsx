import { useCallback, useEffect, useState } from "react";

type Role = "developer" | "manager" | "admin";

export function tabsForRole(role: string): string[] {
  return role === "admin"
    ? ["Sources", "Users", "Tokens", "Status"]
    : ["Tokens"];
}

async function getJSON(url: string) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json();
}

export default function Settings({ role, onClose }: { role: Role; onClose: () => void }) {
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
      {tab === "Tokens" && <TokensPanel admin={role === "admin"} />}
      {tab === "Sources" && <SourcesPanel />}
      {tab === "Users" && <UsersPanel />}
      {tab === "Status" && <StatusPanel />}
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

function SourcesPanel() {
  const [rows, setRows] = useState<any[]>([]);
  const [loc, setLoc] = useState("");
  const [access, setAccess] = useState("everyone");
  const [note, setNote] = useState("");
  const load = useCallback(() => { getJSON("/sources").then(setRows).catch(() => setRows([])); }, []);
  useEffect(() => { load(); }, [load]);
  const add = async () => {
    const r = await fetch("/sources", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: "folder", location: loc, access }) });
    setNote(r.ok ? "added" : `error ${r.status}`); if (r.ok) { setLoc(""); load(); }
  };
  const resync = async (id: number) => { setNote("syncing…");
    const r = await fetch(`/sources/${id}/resync`, { method: "POST" }); setNote(r.ok ? "synced" : `error ${r.status}`); };
  const del = async (id: number) => {
    const r = await fetch(`/sources/${id}`, { method: "DELETE" });
    setNote(r.ok ? "deleted" : `error ${r.status}`);
    load();
  };
  return (
    <div className="panel">
      <div className="row">
        <input placeholder="/path/to/docs (within HIPPO_SOURCE_ROOTS)" value={loc}
          onChange={(e) => setLoc(e.target.value)} />
        <select value={access} onChange={(e) => setAccess(e.target.value)}>
          <option value="everyone">everyone</option><option value="managers">managers</option>
        </select>
        <button onClick={add}>Add source</button><span className="note">{note}</span>
      </div>
      <table><tbody>
        {rows.map((s) => (
          <tr key={s.id}><td>{s.location}</td><td>{s.access}</td>
            <td><button onClick={() => resync(s.id)}>Re-sync</button>
                <button onClick={() => del(s.id)}>Delete</button></td></tr>
        ))}
      </tbody></table>
    </div>
  );
}

function UsersPanel() {
  const [rows, setRows] = useState<any[]>([]);
  const load = useCallback(() => { getJSON("/users").then(setRows).catch(() => setRows([])); }, []);
  useEffect(() => { load(); }, [load]);
  const setRole = async (email: string, role: string) => {
    await fetch(`/users/${encodeURIComponent(email)}/role`, { method: "PUT",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ role }) });
    load();
  };
  return (
    <div className="panel">
      <table><tbody>
        {rows.map((u) => (
          <tr key={u.email}><td>{u.email}</td>
            <td><select value={u.role} onChange={(e) => setRole(u.email, e.target.value)}>
              {["developer", "manager", "admin"].map((r) => <option key={r} value={r}>{r}</option>)}
            </select></td></tr>
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
        <dt>Repos</dt><dd>team: {String(s.repos.team)} · managers: {String(s.repos.managers)}</dd>
        <dt>MCP</dt><dd>{String(s.mcp_enabled)}</dd>
        <dt>Slack</dt><dd>{String(s.slack_enabled)}</dd>
        <dt>Counts</dt><dd>{s.counts.documents} docs · {s.counts.sources} sources · {s.counts.users} users</dd>
      </dl>
    </div>
  );
}
