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
