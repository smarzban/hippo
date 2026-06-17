import type { UIMessage } from "ai";

/** Schema version of the persisted payload. Bump when the stored shape changes
 *  so old/incompatible blobs are dropped (treated as "no history") instead of
 *  being fed into useChat. The storage-key prefix is derived from this so the
 *  two version concepts can never drift apart. */
export const CHAT_HISTORY_VERSION = 1;

const KEY_PREFIX = `hippo:chat:v${CHAT_HISTORY_VERSION}`;

/** Local transcripts expire after the same 7 days as the session cookie, so a
 *  device-local copy never outlives the session that produced it. */
export const HISTORY_TTL_MS = 7 * 24 * 60 * 60 * 1000;

/** Hard cap on persisted messages: bounds the localStorage footprint so we
 *  never hit the ~5 MB quota (which would otherwise silently stop persistence
 *  and serve a stale transcript). Oldest messages are pruned first. */
export const MAX_STORED_MESSAGES = 200;

const VALID_ROLES = new Set(["user", "assistant", "system"]);

type StoredPayload = { v: number; ts: number; messages: UIMessage[] };

const NOOP_STORAGE: Storage = {
  length: 0,
  clear: () => {},
  getItem: () => null,
  key: () => null,
  removeItem: () => {},
  setItem: () => {},
};

/** A Storage that can never throw on access. In sandboxed iframes or with
 *  cookies fully blocked, reading the `window.localStorage` *property* itself
 *  throws a SecurityError — before any of our try/catch guards run. Resolve it
 *  once through this guard and fall back to a no-op store so the app degrades
 *  to "no history" instead of crashing on load (#4). */
export function safeStorage(): Storage {
  try {
    const s = globalThis.localStorage;
    if (s) return s;
  } catch {
    // property access denied (sandbox / cookies blocked)
  }
  return NOOP_STORAGE;
}

/** Storage key for a given signed-in user, normalized so one identity maps to
 *  exactly one key (case/whitespace differences in the login email don't split
 *  history). Falls back to a shared "default" key when the user is unknown, so
 *  a pre-auth session still persists; once we know who's signed in we re-key to
 *  the user's own key (see nextChatHistoryAction) so a shared browser never
 *  bleeds one user's transcript into another's. */
export function chatHistoryKey(email?: string | null): string {
  const id = email?.trim().toLowerCase();
  return `${KEY_PREFIX}:${id || "default"}`;
}

function isValidMessage(m: unknown): m is UIMessage {
  if (typeof m !== "object" || m === null) return false;
  const msg = m as Record<string, unknown>;
  return (
    typeof msg.id === "string" &&
    typeof msg.role === "string" &&
    VALID_ROLES.has(msg.role) &&
    Array.isArray(msg.parts)
  );
}

/** Read persisted messages. Degrades to [] on every failure path: storage
 *  unavailable (private mode), missing key, invalid JSON, wrong shape, version
 *  mismatch, expired TTL, or any malformed element. Never throws. */
export function loadMessages(storage: Storage, key: string, now: number = Date.now()): UIMessage[] {
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
    typeof (parsed as StoredPayload).ts !== "number" ||
    !Array.isArray((parsed as StoredPayload).messages)
  ) {
    return []; // wrong shape or version mismatch
  }
  const payload = parsed as StoredPayload;
  if (now - payload.ts > HISTORY_TTL_MS) return []; // expired with the session
  // Validate each element: a single malformed message would otherwise crash
  // ChatView (m.parts.map) or be replayed to /chat. Drop the whole batch.
  if (!payload.messages.every(isValidMessage)) return [];
  return payload.messages;
}

/** Persist messages under a versioned, timestamped envelope, capped to the
 *  most recent MAX_STORED_MESSAGES. An empty transcript removes the key. On any
 *  write failure (quota, serialization, unavailable storage) it removes the key
 *  so the next load degrades to no-history rather than serving a stale blob. */
export function saveMessages(
  storage: Storage,
  key: string,
  messages: UIMessage[],
  now: number = Date.now(),
): void {
  if (messages.length === 0) {
    clearMessages(storage, key);
    return;
  }
  try {
    const capped = messages.length > MAX_STORED_MESSAGES ? messages.slice(-MAX_STORED_MESSAGES) : messages;
    const payload: StoredPayload = { v: CHAT_HISTORY_VERSION, ts: now, messages: capped };
    storage.setItem(key, JSON.stringify(payload));
  } catch {
    // quota exceeded, serialization failure, or unavailable storage: drop the
    // key so a later refresh sees "no history", never a stale transcript.
    clearMessages(storage, key);
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

export type HistoryAction =
  | { kind: "restore"; key: string; clearKey: string }
  | { kind: "persist"; key: string }
  | { kind: "noop" };

/** Pure persistence policy — the decision logic that used to live (buggy) in
 *  App.tsx effects. Given the user we last restored for, the currently
 *  signed-in user, and the chat stream status, decide the single next action.
 *
 *  - Unknown user            -> noop  (never write the shared key once auth matters)
 *  - User not yet restored    -> restore from THAT user's key (incl. empty),
 *                                clearing the shared default key (no bleed, #2)
 *  - Already-restored user +
 *    settled stream           -> persist (on ready/error only, never per token, #7)
 *  - Already-restored user +
 *    mid-stream               -> noop
 *
 *  Because "restore" and "persist" are mutually exclusive for a given
 *  (restoredFor, meEmail) and the caller marks the user restored *before*
 *  applying setMessages, the restore→persist ordering race (#6) cannot occur. */
export function nextChatHistoryAction(
  restoredFor: string | null,
  meEmail: string | null,
  status: string,
): HistoryAction {
  if (!meEmail) return { kind: "noop" };
  const key = chatHistoryKey(meEmail);
  if (restoredFor !== meEmail) {
    return { kind: "restore", key, clearKey: chatHistoryKey(null) };
  }
  if (status === "ready" || status === "error") {
    return { kind: "persist", key };
  }
  return { kind: "noop" };
}
