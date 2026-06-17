import { describe, expect, it, vi } from "vitest";
import type { UIMessage } from "ai";
import {
  CHAT_HISTORY_VERSION,
  HISTORY_TTL_MS,
  MAX_STORED_MESSAGES,
  chatHistoryKey,
  clearMessages,
  loadMessages,
  nextChatHistoryAction,
  purgeForeignKeys,
  safeStorage,
  saveMessages,
} from "./chatHistory";

/** Minimal in-memory Storage stub matching the DOM Storage interface. */
function memStorage(seed: Record<string, string> = {}): Storage {
  const map = new Map<string, string>(Object.entries(seed));
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (k: string) => (map.has(k) ? map.get(k)! : null),
    key: (i: number) => Array.from(map.keys())[i] ?? null,
    removeItem: (k: string) => map.delete(k),
    setItem: (k: string, v: string) => void map.set(k, v),
  };
}

const NOW = 1_700_000_000_000; // fixed clock for deterministic ts/TTL assertions

const MSGS: UIMessage[] = [
  { id: "1", role: "user", parts: [{ type: "text", text: "hi" }] },
  { id: "2", role: "assistant", parts: [{ type: "text", text: "hello" }] },
];

describe("chatHistoryKey", () => {
  it("namespaces by email when known", () => {
    expect(chatHistoryKey("a@b.com")).toBe("hippo:chat:v1:a@b.com");
  });
  it("normalizes case and surrounding whitespace (one identity = one key)", () => {
    expect(chatHistoryKey("  A@B.Com ")).toBe("hippo:chat:v1:a@b.com");
  });
  it("falls back to a default key when unknown", () => {
    expect(chatHistoryKey(null)).toBe("hippo:chat:v1:default");
    expect(chatHistoryKey(undefined)).toBe("hippo:chat:v1:default");
    expect(chatHistoryKey("   ")).toBe("hippo:chat:v1:default");
  });
  it("derives the key prefix from the schema version (single source)", () => {
    expect(chatHistoryKey("x")).toContain(`v${CHAT_HISTORY_VERSION}`);
  });
});

describe("loadMessages / saveMessages round-trip", () => {
  it("saves then loads the same messages", () => {
    const s = memStorage();
    const key = chatHistoryKey("a@b.com");
    saveMessages(s, key, MSGS, { now: NOW });
    expect(loadMessages(s, key, { now: NOW })).toEqual(MSGS);
  });

  it("writes a versioned, timestamped envelope", () => {
    const s = memStorage();
    saveMessages(s, "k", MSGS, { now: NOW });
    expect(JSON.parse(s.getItem("k")!)).toMatchObject({
      v: CHAT_HISTORY_VERSION,
      ts: NOW,
      messages: MSGS,
    });
  });
});

describe("loadMessages degradation", () => {
  it("returns [] when the key is missing", () => {
    expect(loadMessages(memStorage(), "nope", { now: NOW })).toEqual([]);
  });

  it("returns [] on corrupt JSON", () => {
    expect(loadMessages(memStorage({ k: "{not json" }), "k", { now: NOW })).toEqual([]);
  });

  it("returns [] on a version mismatch", () => {
    const stale = JSON.stringify({ v: CHAT_HISTORY_VERSION + 1, ts: NOW, messages: MSGS });
    expect(loadMessages(memStorage({ k: stale }), "k", { now: NOW })).toEqual([]);
  });

  it("returns [] when the payload shape is wrong", () => {
    const bad = JSON.stringify({ v: CHAT_HISTORY_VERSION, ts: NOW, messages: "oops" });
    expect(loadMessages(memStorage({ k: bad }), "k", { now: NOW })).toEqual([]);
  });

  it("returns [] when getItem throws (private/locked storage)", () => {
    const throwing = {
      ...memStorage(),
      getItem: () => {
        throw new Error("SecurityError");
      },
    } as unknown as Storage;
    expect(loadMessages(throwing, "k", { now: NOW })).toEqual([]);
  });
});

describe("loadMessages per-element validation (#5)", () => {
  const store = (messages: unknown) =>
    memStorage({ k: JSON.stringify({ v: CHAT_HISTORY_VERSION, ts: NOW, messages }) });

  it("drops the whole batch when any element lacks parts", () => {
    expect(loadMessages(store([{ id: "1", role: "user" }]), "k", { now: NOW })).toEqual([]);
  });
  it("drops the whole batch when any element lacks an id", () => {
    expect(loadMessages(store([{ role: "user", parts: [] }]), "k", { now: NOW })).toEqual([]);
  });
  it("drops the whole batch on an unknown role", () => {
    expect(loadMessages(store([{ id: "1", role: "robot", parts: [] }]), "k", { now: NOW })).toEqual([]);
  });
  it("drops the whole batch on a non-object element", () => {
    expect(loadMessages(store([null]), "k", { now: NOW })).toEqual([]);
  });
  it("drops the whole batch when a part has no string type (would crash ChatView)", () => {
    expect(loadMessages(store([{ id: "1", role: "user", parts: [{}] }]), "k", { now: NOW })).toEqual([]);
    expect(loadMessages(store([{ id: "1", role: "user", parts: [{ type: 7 }] }]), "k", { now: NOW })).toEqual([]);
  });
  it("drops the whole batch when a text/reasoning part has no string text", () => {
    expect(
      loadMessages(store([{ id: "1", role: "assistant", parts: [{ type: "text" }] }]), "k", { now: NOW }),
    ).toEqual([]);
    expect(
      loadMessages(store([{ id: "1", role: "assistant", parts: [{ type: "reasoning", text: 5 }] }]), "k", { now: NOW }),
    ).toEqual([]);
  });
  it("accepts non-text parts (e.g. tool parts) that legitimately have no text", () => {
    const toolMsg = [{ id: "1", role: "assistant", parts: [{ type: "tool-search", state: "input-available" }] }];
    expect(loadMessages(store(toolMsg), "k", { now: NOW })).toEqual(toolMsg);
  });
  it("returns well-formed messages untouched", () => {
    expect(loadMessages(store(MSGS), "k", { now: NOW })).toEqual(MSGS);
  });
});

describe("loadMessages TTL (#1 — local history expires with the session)", () => {
  it("returns [] when the payload is older than the 7-day TTL", () => {
    const old = JSON.stringify({ v: CHAT_HISTORY_VERSION, ts: NOW - HISTORY_TTL_MS - 1, messages: MSGS });
    expect(loadMessages(memStorage({ k: old }), "k", { now: NOW })).toEqual([]);
  });
  it("returns the messages when within the TTL", () => {
    const fresh = JSON.stringify({ v: CHAT_HISTORY_VERSION, ts: NOW - HISTORY_TTL_MS + 1000, messages: MSGS });
    expect(loadMessages(memStorage({ k: fresh }), "k", { now: NOW })).toEqual(MSGS);
  });
  it("returns [] when the timestamp is missing or non-numeric", () => {
    const noTs = JSON.stringify({ v: CHAT_HISTORY_VERSION, messages: MSGS });
    expect(loadMessages(memStorage({ k: noTs }), "k", { now: NOW })).toEqual([]);
  });
});

describe("loadMessages role binding (re-review #A — drop on role downgrade)", () => {
  it("returns the transcript when the stored role matches the current role", () => {
    const s = memStorage();
    saveMessages(s, "k", MSGS, { now: NOW, role: "admin" });
    expect(loadMessages(s, "k", { now: NOW, role: "admin" })).toEqual(MSGS);
  });
  it("drops the transcript when the role changed since it was stored (downgrade)", () => {
    const s = memStorage();
    saveMessages(s, "k", MSGS, { now: NOW, role: "admin" });
    // user was downgraded admin -> user; their admin-era transcript must not replay
    expect(loadMessages(s, "k", { now: NOW, role: "user" })).toEqual([]);
  });
  it("drops a role-less legacy payload when a role is required", () => {
    const legacy = JSON.stringify({ v: CHAT_HISTORY_VERSION, ts: NOW, messages: MSGS });
    expect(loadMessages(memStorage({ k: legacy }), "k", { now: NOW, role: "user" })).toEqual([]);
  });
});

describe("saveMessages", () => {
  it("caps to the last MAX_STORED_MESSAGES, pruning the oldest (#8)", () => {
    const s = memStorage();
    const many: UIMessage[] = Array.from({ length: MAX_STORED_MESSAGES + 25 }, (_, i) => ({
      id: String(i),
      role: "user",
      parts: [{ type: "text", text: String(i) }],
    }));
    saveMessages(s, "k", many, { now: NOW });
    const stored = loadMessages(s, "k", { now: NOW });
    expect(stored).toHaveLength(MAX_STORED_MESSAGES);
    expect(stored[0].id).toBe("25"); // oldest 25 pruned
    expect(stored[stored.length - 1].id).toBe(String(MAX_STORED_MESSAGES + 24));
  });

  it("removes the key when asked to save an empty transcript", () => {
    const s = memStorage();
    saveMessages(s, "k", MSGS, { now: NOW });
    saveMessages(s, "k", [], { now: NOW });
    expect(s.getItem("k")).toBeNull();
  });

  it("does not throw when setItem throws (quota exceeded)", () => {
    const throwing = {
      ...memStorage(),
      setItem: vi.fn(() => {
        throw new Error("QuotaExceededError");
      }),
    } as unknown as Storage;
    expect(() => saveMessages(throwing, "k", MSGS, { now: NOW })).not.toThrow();
  });

  it("degrades to no-history (removes the key) on a failed write, never stale (#8)", () => {
    const removeItem = vi.fn();
    const throwing = {
      ...memStorage({ k: JSON.stringify({ v: CHAT_HISTORY_VERSION, ts: NOW, messages: MSGS }) }),
      setItem: () => {
        throw new Error("QuotaExceededError");
      },
      removeItem,
    } as unknown as Storage;
    saveMessages(throwing, "k", MSGS, { now: NOW });
    expect(removeItem).toHaveBeenCalledWith("k");
  });
});

describe("clearMessages", () => {
  it("removes a persisted entry", () => {
    const s = memStorage();
    saveMessages(s, "k", MSGS, { now: NOW });
    clearMessages(s, "k");
    expect(loadMessages(s, "k", { now: NOW })).toEqual([]);
  });

  it("does not throw when removeItem throws", () => {
    const throwing = {
      ...memStorage(),
      removeItem: () => {
        throw new Error("nope");
      },
    } as unknown as Storage;
    expect(() => clearMessages(throwing, "k")).not.toThrow();
  });
});

describe("purgeForeignKeys (re-review #D — no foreign blobs / email enumeration on shared devices)", () => {
  it("removes every other hippo chat key, keeping only the current user's", () => {
    const keep = chatHistoryKey("me@x.com");
    const s = memStorage({
      [keep]: "mine",
      [chatHistoryKey("prev@x.com")]: "theirs",
      [chatHistoryKey(null)]: "anon",
      "unrelated:key": "leave-me",
    });
    purgeForeignKeys(s, keep);
    expect(s.getItem(keep)).toBe("mine");
    expect(s.getItem(chatHistoryKey("prev@x.com"))).toBeNull();
    expect(s.getItem(chatHistoryKey(null))).toBeNull();
    expect(s.getItem("unrelated:key")).toBe("leave-me"); // non-hippo keys untouched
  });

  it("never throws when storage access fails", () => {
    const throwing = {
      ...memStorage(),
      get length(): number {
        throw new Error("SecurityError");
      },
    } as unknown as Storage;
    expect(() => purgeForeignKeys(throwing, "k")).not.toThrow();
  });
});

describe("safeStorage (#4 — never throw on storage access)", () => {
  it("returns a working store when localStorage is available", () => {
    const fake = memStorage();
    const orig = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
    Object.defineProperty(globalThis, "localStorage", { value: fake, configurable: true });
    try {
      const s = safeStorage();
      s.setItem("probe", "1");
      expect(s.getItem("probe")).toBe("1");
    } finally {
      if (orig) Object.defineProperty(globalThis, "localStorage", orig);
      else delete (globalThis as { localStorage?: unknown }).localStorage;
    }
  });

  it("returns a no-op store (never throws) when property access throws", () => {
    const orig = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() {
        throw new Error("SecurityError: cookies blocked");
      },
    });
    try {
      const s = safeStorage();
      expect(() => s.setItem("k", "v")).not.toThrow();
      expect(s.getItem("k")).toBeNull(); // no-op store keeps nothing
      expect(() => s.removeItem("k")).not.toThrow();
    } finally {
      if (orig) Object.defineProperty(globalThis, "localStorage", orig);
      else delete (globalThis as { localStorage?: unknown }).localStorage;
    }
  });
});

describe("nextChatHistoryAction (#2/#6/#7 + re-review #C — race-free persistence policy)", () => {
  const userKey = chatHistoryKey("a@b.com");

  it("no-ops while the signed-in user is unknown (never writes the shared key)", () => {
    expect(nextChatHistoryAction(null, null, "ready", false)).toEqual({ kind: "noop" });
    expect(nextChatHistoryAction("a@b.com", null, "ready", false)).toEqual({ kind: "noop" });
  });

  it("restores from the USER key when a user first resolves with no in-flight draft", () => {
    expect(nextChatHistoryAction(null, "a@b.com", "ready", false)).toEqual({ kind: "restore", key: userKey });
  });

  it("ADOPTS (keeps the draft) when a user resolves while a transcript is already on screen (#C)", () => {
    // a message typed/sent before /me resolved must not be clobbered by restore
    expect(nextChatHistoryAction(null, "a@b.com", "ready", true)).toEqual({ kind: "adopt", key: userKey });
  });

  it("restores regardless of stream status (restore is not gated on settled)", () => {
    expect(nextChatHistoryAction(null, "a@b.com", "streaming", false).kind).toBe("restore");
  });

  it("restores/adopts again when the signed-in user changes (no cross-user bleed)", () => {
    expect(nextChatHistoryAction("a@b.com", "b@b.com", "ready", false)).toEqual({
      kind: "restore",
      key: chatHistoryKey("b@b.com"),
    });
    expect(nextChatHistoryAction("a@b.com", "b@b.com", "ready", true).kind).toBe("adopt");
  });

  it("persists only once the current user is already restored AND the stream settled", () => {
    expect(nextChatHistoryAction("a@b.com", "a@b.com", "ready", true)).toEqual({ kind: "persist", key: userKey });
    expect(nextChatHistoryAction("a@b.com", "a@b.com", "error", true)).toEqual({ kind: "persist", key: userKey });
  });

  it("does NOT persist mid-stream (#7 — no per-token writes)", () => {
    expect(nextChatHistoryAction("a@b.com", "a@b.com", "submitted", true)).toEqual({ kind: "noop" });
    expect(nextChatHistoryAction("a@b.com", "a@b.com", "streaming", true)).toEqual({ kind: "noop" });
  });
});
