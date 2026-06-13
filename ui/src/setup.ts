export const MIN_PASSWORD_LEN = 8;

export type SetupState = {
  token: string;
  authMode: "password" | "oidc" | "iap";
  ownerEmail: string;
  ownerPassword: string;
  models: { chat_model: string; embedding_model: string; embedding_dim: number };
};

const emailish = (s: string) => /.+@.+\..+/.test(s);

/**
 * Per-field inline messages. A field only errors once it has content — empty
 * required fields stay quiet until submit is attempted (canSubmit gates that),
 * so the form doesn't scream before the user has typed anything.
 */
export function fieldErrors(s: SetupState): { email?: string; password?: string } {
  const e: { email?: string; password?: string } = {};
  if (s.ownerEmail && !emailish(s.ownerEmail)) e.email = "Enter a valid email address";
  if (s.authMode === "password" && s.ownerPassword && s.ownerPassword.length < MIN_PASSWORD_LEN)
    e.password = `Password must be at least ${MIN_PASSWORD_LEN} characters`;
  return e;
}

/** Whether the single-page form may be submitted. The server re-validates everything. */
export function canSubmit(s: SetupState): boolean {
  if (s.token.trim().length === 0) return false;
  if (!emailish(s.ownerEmail)) return false;
  if (s.authMode === "password" && s.ownerPassword.length < MIN_PASSWORD_LEN) return false;
  return true;
}

export function buildSetupPayload(s: SetupState) {
  // No `roots`: folder naming moved out of first-run setup into Settings → Folders,
  // so the three seeded roots keep their defaults (Default / Private / Owner).
  return {
    token: s.token,
    auth_mode: s.authMode,
    owner_email: s.ownerEmail,
    owner_password: s.ownerPassword,
    models: s.models,
  };
}
