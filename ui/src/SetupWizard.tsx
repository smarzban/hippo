import { useState } from "react";
import { buildSetupPayload, canSubmit, fieldErrors, type SetupState } from "./setup";

const INIT_SETUP: SetupState = {
  token: "",
  authMode: "password",
  ownerEmail: "",
  ownerPassword: "",
  models: { chat_model: "" },
};

/** First-run setup wizard. Self-contained: posts /setup and reloads on success.
 * Shown by App when GET /setup/status reports setup is incomplete. */
export default function SetupWizard() {
  const [state, setState] = useState<SetupState>(INIT_SETUP);
  const [submitErr, setSubmitErr] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const errors = fieldErrors(state);
  const ready = canSubmit(state);

  async function finish() {
    if (!ready) return;
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

  const label = { display: "block", fontWeight: 600, marginBottom: 2 } as const;
  const field = { marginBottom: 12 } as const;

  return (
    <div className="app">
      <div className="empty signin">
        <span className="logo">{"\u{1F99B}"}</span>
        <h1>Hippo — First-run Setup</h1>
        <p className="tagline">Set up your instance — this runs once.</p>

        <form
          className="setup-form"
          style={{ width: "100%", maxWidth: 420, textAlign: "left" }}
          onSubmit={(e) => {
            e.preventDefault();
            void finish();
          }}
        >
          <div style={field}>
            <label style={label}>Setup token</label>
            <input
              type="text"
              placeholder="From HIPPO_SETUP_TOKEN (env or startup log)"
              value={state.token}
              autoFocus
              style={{ width: "100%" }}
              onChange={(e) => setState((s) => ({ ...s, token: e.target.value }))}
            />
          </div>

          <div style={field}>
            <label style={label}>Authentication mode</label>
            <select
              value={state.authMode}
              style={{ width: "100%" }}
              onChange={(e) =>
                setState((s) => ({ ...s, authMode: e.target.value as SetupState["authMode"] }))
              }
            >
              <option value="password">Password (email + password)</option>
              <option value="oidc">OIDC / Google (OAuth2)</option>
              <option value="iap">IAP (Google Cloud Identity-Aware Proxy)</option>
            </select>
          </div>

          <div style={field}>
            <label style={label}>Owner email</label>
            <input
              type="email"
              placeholder="you@example.com"
              value={state.ownerEmail}
              style={{ width: "100%" }}
              onChange={(e) => setState((s) => ({ ...s, ownerEmail: e.target.value }))}
            />
            {errors.email && <p className="error" style={{ margin: "4px 0 0" }}>{errors.email}</p>}
          </div>

          {state.authMode === "password" && (
            <div style={field}>
              <label style={label}>Owner password</label>
              <input
                type="password"
                placeholder="At least 8 characters"
                value={state.ownerPassword}
                style={{ width: "100%" }}
                onChange={(e) => setState((s) => ({ ...s, ownerPassword: e.target.value }))}
              />
              {errors.password && (
                <p className="error" style={{ margin: "4px 0 0" }}>{errors.password}</p>
              )}
            </div>
          )}

          <details style={{ marginBottom: 12 }}>
            <summary style={{ cursor: "pointer", fontWeight: 600 }}>
              Chat model (optional — default from server env)
            </summary>
            <div style={{ marginTop: 8 }}>
              <div style={field}>
                <label style={label}>Chat model</label>
                <input
                  type="text"
                  placeholder="e.g. openai:gpt-4o"
                  value={state.models.chat_model}
                  style={{ width: "100%" }}
                  onChange={(e) =>
                    setState((s) => ({ ...s, models: { ...s.models, chat_model: e.target.value } }))
                  }
                />
              </div>
              <p className="sec" style={{ margin: "4px 0 0" }}>
                Embedding model/dimension are set via <code>HIPPO_EMBEDDING_*</code> in the
                environment (they can't change after the index is created).
              </p>
            </div>
          </details>

          {submitErr && <p className="error">{submitErr}</p>}

          <button
            className="upload-btn"
            type="submit"
            disabled={!ready || submitting}
            style={{ marginTop: 4 }}
          >
            {submitting ? "Setting up…" : "Complete setup"}
          </button>
        </form>
      </div>
    </div>
  );
}
