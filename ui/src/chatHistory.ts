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

/** A transcript is bound to the role that produced it; `role` lets a load drop
 *  history whose access tier no longer matches the caller's (a downgrade). */
type StoredPayload = { v: number; ts: number; messages: UIMessage[]; role?: string };

export interface HistoryOptions {
  /** Injectable clock (defaults to Date.now()) — keeps TTL logic testable. */
  now?: number;
  /** The caller's current role. When set, a stored transcript whose role
   *  differs is dropped (so a downgraded user can't replay higher-tier
   *  answers); when set on save it stamps the role into the envelope. */
  role?: string | null;
}

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
 *  history). Falls back to a shared "default" key when the user is unknown. */
export function chatHistoryKey(email?: string | null): string {
  const id = email?.trim().toLowerCase();
  return `${KEY_PREFIX}:${id || "default"}`;
}

function isValidPart(p: unknown): boolean {
  if (typeof p !== "object" || p === null) return false;
  const part = p as Record<string, unknown>;
  // ChatView switches on a *string* part.type (part.type.startsWith(...)); a
  // type-less part would crash it. Text-bearing parts must carry string text.
  if (typeof part.type !== "string") return false;
  if ((part.type === "text" || part.type === "reasoning") && typeof part.text !== "string") return false;
  return true;
}

function isValidMessage(m: unknown): m is UIMessage {
  if (typeof m !== "object" || m === null) return false;
  const msg = m as Record<string, unknown>;
  return (
    typeof msg.id === "string" &&
    typeof msg.role === "string" &&
    VALID_ROLES.has(msg.role) &&
    Array.isArray(msg.parts) &&
    msg.parts.every(isValidPart)
  );
}

/** Read persisted messages. Degrades to [] on every failure path: storage
 *  unavailable, missing key, invalid JSON, wrong shape, version mismatch,
 *  expired TTL, a role that no longer matches, or any malformed element.
 *  Never throws. */
export function loadMessages(storage: Storage, key: string, opts: HistoryOptions = {}): UIMessage[] {
  const now = opts.now ?? Date.now();
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
  // Don't replay a transcript whose access tier no longer matches the caller
  // (e.g. an admin downgraded to user must not see/replay admin-era answers).
  if (opts.role != null && payload.role !== opts.role) return [];
  // Validate each element: a single malformed message would otherwise crash
  // ChatView (m.parts.map) or be replayed to /chat. Drop the whole batch.
  if (!payload.messages.every(isValidMessage)) return [];
  return payload.messages;
}

/** Persist messages under a versioned, timestamped (and role-stamped) envelope,
 *  capped to the most recent MAX_STORED_MESSAGES. An empty transcript removes
 *  the key. On any write failure (quota, serialization, unavailable storage) it
 *  removes the key so the next load degrades to no-history, never stale. */
export function saveMessages(
  storage: Storage,
  key: string,
  messages: UIMessage[],
  opts: HistoryOptions = {},
): void {
  if (messages.length === 0) {
    clearMessages(storage, key);
    return;
  }
  const now = opts.now ?? Date.now();
  try {
    const capped = messages.length > MAX_STORED_MESSAGES ? messages.slice(-MAX_STORED_MESSAGES) : messages;
    const payload: StoredPayload = { v: CHAT_HISTORY_VERSION, ts: now, messages: capped };
    if (opts.role != null) payload.role = opts.role;
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

/** Remove every *other* user's persisted chat (and the shared default) on a
 *  shared device, keeping only the signed-in user's key. Prevents a prior
 *  user's transcript — and the enumeration of their email via the key — from
 *  lingering in localStorage after a new user signs in. Never throws. */
export function purgeForeignKeys(storage: Storage, keepKey: string): void {
  try {
    const toRemove: string[] = [];
    for (let i = 0; i < storage.length; i++) {
      const k = storage.key(i);
      if (k && k.startsWith(`${KEY_PREFIX}:`) && k !== keepKey) toRemove.push(k);
    }
    for (const k of toRemove) storage.removeItem(k);
  } catch {
    // storage access can throw; purge is best-effort
  }
}

export type HistoryAction =
  | { kind: "restore"; key: string }
  | { kind: "adopt"; key: string }
  | { kind: "persist"; key: string }
  | { kind: "noop" };

/** Pure persistence policy — the decision logic that used to live (buggy) in
 *  App.tsx effects. Given the user we last restored for, the currently
 *  signed-in user, the chat stream status, and whether a transcript is already
 *  on screen, decide the single next action:
 *
 *  - Unknown user                      -> noop  (never write a shared key)
 *  - User not yet restored, no draft   -> restore from THAT user's key
 *  - User not yet restored, draft shown -> adopt: claim the user's key WITHOUT
 *                                          clobbering an in-flight/pre-auth draft
 *  - Already-restored user + settled    -> persist (ready/error only, never per token)
 *  - Already-restored user + mid-stream -> noop
 *
 *  Because "restore"/"adopt" and "persist" are mutually exclusive for a given
 *  (restoredFor, meEmail) and the caller marks the user restored *before*
 *  applying setMessages, the restore→persist ordering race (#6) cannot occur. */
export function nextChatHistoryAction(
  restoredFor: string | null,
  meEmail: string | null,
  status: string,
  hasMessages: boolean,
): HistoryAction {
  if (!meEmail) return { kind: "noop" };
  const key = chatHistoryKey(meEmail);
  if (restoredFor !== meEmail) {
    return hasMessages ? { kind: "adopt", key } : { kind: "restore", key };
  }
  if (status === "ready" || status === "error") {
    return { kind: "persist", key };
  }
  return { kind: "noop" };
}
