export const WIZARD_STEPS = ["token", "auth", "owner", "roots", "models", "finish"] as const;
export const MIN_PASSWORD_LEN = 8;

export type SetupState = {
  step: number;
  token: string;
  authMode: "password" | "oidc" | "iap";
  ownerEmail: string;
  ownerPassword: string;
  roots: { user: string; admin: string; owner: string };
  models: { chat_model: string; embedding_model: string; embedding_dim: number };
};

const emailish = (s: string) => /.+@.+\..+/.test(s);

/** Per-step validity gate for the Next button. The server re-validates everything. */
export function stepValid(s: SetupState): boolean {
  switch (WIZARD_STEPS[s.step]) {
    case "token": return s.token.trim().length > 0;
    case "auth": return ["password", "oidc", "iap"].includes(s.authMode);
    case "owner":
      if (!emailish(s.ownerEmail)) return false;
      return s.authMode !== "password" || s.ownerPassword.length >= MIN_PASSWORD_LEN;
    case "roots": return !!(s.roots.user && s.roots.admin && s.roots.owner);
    case "models": return true;   // names optional; server falls back to env defaults
    default: return true;
  }
}

export function nextStep(s: SetupState): SetupState {
  if (!stepValid(s)) return s;
  return { ...s, step: Math.min(s.step + 1, WIZARD_STEPS.length - 1) };
}

export function buildSetupPayload(s: SetupState) {
  return {
    token: s.token, auth_mode: s.authMode, owner_email: s.ownerEmail,
    owner_password: s.ownerPassword, roots: s.roots, models: s.models,
  };
}
