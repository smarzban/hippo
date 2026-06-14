export const MIN_PASSWORD_LEN = 8;

/** Client-side pre-check for the password-change form. Returns an error string
 *  or null when valid. The server re-validates (this is UX, not the gate). */
export function passwordChangeError(
  current: string,
  next: string,
  confirm: string,
): string | null {
  if (!current) return "Enter your current password.";
  if (next.length < MIN_PASSWORD_LEN) return `New password must be at least ${MIN_PASSWORD_LEN} characters.`;
  if (next !== confirm) return "New password and confirmation do not match.";
  return null;
}

/** Pull a human error message off a non-OK fetch Response: the API's JSON `detail`
 *  if present, else the provided fallback, else a generic `error <status>` (LOW-30).
 *  Single helper so every panel reports errors consistently. */
export async function errorDetail(r: Response, fallback?: string): Promise<string> {
  const body = await r.json().catch(() => ({} as { detail?: string }));
  return body?.detail ?? fallback ?? `error ${r.status}`;
}
