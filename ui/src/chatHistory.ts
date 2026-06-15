import type { UIMessage } from "ai";

/** Schema version of the persisted payload. Bump when the stored shape changes
 *  so old/incompatible blobs are dropped (treated as "no history") instead of
 *  being fed into useChat. */
export const CHAT_HISTORY_VERSION = 1;

const KEY_PREFIX = "hippo:chat:v1";

type StoredPayload = { v: number; messages: UIMessage[] };

/** Storage key for a given signed-in user. Falls back to a shared "default"
 *  key when the user is unknown, so a logged-out/none-mode session still
 *  persists, but a shared browser doesn't bleed one user's transcript into
 *  another's once we know who's signed in. */
export function chatHistoryKey(email?: string | null): string {
  return `${KEY_PREFIX}:${email && email.trim() ? email : "default"}`;
}

/** Read persisted messages. Degrades to [] on every failure path: storage
 *  unavailable (private mode), missing key, invalid JSON, wrong shape, or a
 *  version mismatch. Never throws. */
export function loadMessages(storage: Storage, key: string): UIMessage[] {
  let raw: string | null;
  try {
    raw = storage.getItem(key);
  } catch {
    return []; // storage access can throw in private/locked-down modes
  }
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return []; // corrupt JSON
  }
  if (
    typeof parsed !== "object" ||
    parsed === null ||
    (parsed as StoredPayload).v !== CHAT_HISTORY_VERSION ||
    !Array.isArray((parsed as StoredPayload).messages)
  ) {
    return []; // wrong shape or version mismatch
  }
  return (parsed as StoredPayload).messages;
}

/** Persist messages under a versioned envelope. Swallows quota and
 *  serialization errors so a failed write can never crash the chat. */
export function saveMessages(storage: Storage, key: string, messages: UIMessage[]): void {
  try {
    const payload: StoredPayload = { v: CHAT_HISTORY_VERSION, messages };
    storage.setItem(key, JSON.stringify(payload));
  } catch {
    // quota exceeded, serialization failure, or unavailable storage — ignore
  }
}

/** Remove any persisted history for this key. Never throws. */
export function clearMessages(storage: Storage, key: string): void {
  try {
    storage.removeItem(key);
  } catch {
    // ignore
  }
}
