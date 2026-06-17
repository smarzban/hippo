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
    saveMessages(s, key, MSGS, NOW);
    expect(loadMessages(s, key, NOW)).toEqual(MSGS);
  });

  it("writes a versioned, timestamped envelope", () => {
    const s = memStorage();
    saveMessages(s, "k", MSGS, NOW);
    expect(JSON.parse(s.getItem("k")!)).toEqual({
      v: CHAT_HISTORY_VERSION,
      ts: NOW,
      messages: MSGS,
    });
  });
});

describe("loadMessages degradation", () => {
  it("returns [] when the key is missing", () => {
    expect(loadMessages(memStorage(), "nope", NOW)).toEqual([]);
  });

  it("returns [] on corrupt JSON", () => {
    expect(loadMessages(memStorage({ k: "{not json" }), "k", NOW)).toEqual([]);
  });

  it("returns [] on a version mismatch", () => {
    const stale = JSON.stringify({ v: CHAT_HISTORY_VERSION + 1, ts: NOW, messages: MSGS });
    expect(loadMessages(memStorage({ k: stale }), "k", NOW)).toEqual([]);
  });

  it("returns [] when the payload shape is wrong", () => {
    const bad = JSON.stringify({ v: CHAT_HISTORY_VERSION, ts: NOW, messages: "oops" });
    expect(loadMessages(memStorage({ k: bad }), "k", NOW)).toEqual([]);
  });

  it("returns [] when getItem throws (private/locked storage)", () => {
    const throwing = {
      ...memStorage(),
      getItem: () => {
        throw new Error("SecurityError");
      },
    } as unknown as Storage;
    expect(loadMessages(throwing, "k", NOW)).toEqual([]);
  });
});

describe("loadMessages per-element validation (#5)", () => {
  const store = (messages: unknown) =>
    memStorage({ k: JSON.stringify({ v: CHAT_HISTORY_VERSION, ts: NOW, messages }) });

  it("drops the whole batch when any element lacks parts", () => {
    expect(loadMessages(store([{ id: "1", role: "user" }]), "k", NOW)).toEqual([]);
  });
  it("drops the whole batch when any element lacks an id", () => {
    expect(loadMessages(store([{ role: "user", parts: [] }]), "k", NOW)).toEqual([]);
  });
  it("drops the whole batch on an unknown role", () => {
    expect(loadMessages(store([{ id: "1", role: "robot", parts: [] }]), "k", NOW)).toEqual([]);
  });
  it("drops the whole batch on a non-object element", () => {
    expect(loadMessages(store([null]), "k", NOW)).toEqual([]);
  });
  it("returns well-formed messages untouched", () => {
    expect(loadMessages(store(MSGS), "k", NOW)).toEqual(MSGS);
  });
});

describe("loadMessages TTL (#1 — local history expires with the session)", () => {
  it("returns [] when the payload is older than the 7-day TTL", () => {
    const old = JSON.stringify({ v: CHAT_HISTORY_VERSION, ts: NOW - HISTORY_TTL_MS - 1, messages: MSGS });
    expect(loadMessages(memStorage({ k: old }), "k", NOW)).toEqual([]);
  });
  it("returns the messages when within the TTL", () => {
    const fresh = JSON.stringify({ v: CHAT_HISTORY_VERSION, ts: NOW - HISTORY_TTL_MS + 1000, messages: MSGS });
    expect(loadMessages(memStorage({ k: fresh }), "k", NOW)).toEqual(MSGS);
  });
  it("returns [] when the timestamp is missing or non-numeric", () => {
    const noTs = JSON.stringify({ v: CHAT_HISTORY_VERSION, messages: MSGS });
    expect(loadMessages(memStorage({ k: noTs }), "k", NOW)).toEqual([]);
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
    saveMessages(s, "k", many, NOW);
    const stored = loadMessages(s, "k", NOW);
    expect(stored).toHaveLength(MAX_STORED_MESSAGES);
    expect(stored[0].id).toBe("25"); // oldest 25 pruned
    expect(stored[stored.length - 1].id).toBe(String(MAX_STORED_MESSAGES + 24));
  });

  it("removes the key when asked to save an empty transcript", () => {
    const s = memStorage();
    saveMessages(s, "k", MSGS, NOW);
    saveMessages(s, "k", [], NOW);
    expect(s.getItem("k")).toBeNull();
  });

  it("does not throw when setItem throws (quota exceeded)", () => {
    const throwing = {
      ...memStorage(),
      setItem: vi.fn(() => {
        throw new Error("QuotaExceededError");
      }),
    } as unknown as Storage;
    expect(() => saveMessages(throwing, "k", MSGS, NOW)).not.toThrow();
  });

  it("degrades to no-history (removes the key) on a failed write, never stale (#8)", () => {
    // pre-existing stale value; the new write fails -> the key must be removed,
    // so a later load returns [] rather than the old transcript.
    const removeItem = vi.fn();
    const throwing = {
      ...memStorage({ k: JSON.stringify({ v: CHAT_HISTORY_VERSION, ts: NOW, messages: MSGS }) }),
      setItem: () => {
        throw new Error("QuotaExceededError");
      },
      removeItem,
    } as unknown as Storage;
    saveMessages(throwing, "k", MSGS, NOW);
    expect(removeItem).toHaveBeenCalledWith("k");
  });
});

describe("clearMessages", () => {
  it("removes a persisted entry", () => {
    const s = memStorage();
    saveMessages(s, "k", MSGS, NOW);
    clearMessages(s, "k");
    expect(loadMessages(s, "k", NOW)).toEqual([]);
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

describe("nextChatHistoryAction (#2/#6/#7 — race-free persistence policy)", () => {
  const userKey = chatHistoryKey("a@b.com");
  const defaultKey = chatHistoryKey(null);

  it("no-ops while the signed-in user is unknown (never writes the shared key)", () => {
    expect(nextChatHistoryAction(null, null, "ready")).toEqual({ kind: "noop" });
    expect(nextChatHistoryAction("a@b.com", null, "ready")).toEqual({ kind: "noop" });
  });

  it("restores from the USER key (not the shared default) when a user first resolves", () => {
    expect(nextChatHistoryAction(null, "a@b.com", "ready")).toEqual({
      kind: "restore",
      key: userKey,
      clearKey: defaultKey,
    });
  });

  it("restores regardless of stream status (restore is not gated on settled)", () => {
    expect(nextChatHistoryAction(null, "a@b.com", "streaming").kind).toBe("restore");
  });

  it("restores again when the signed-in user changes (no cross-user bleed)", () => {
    const action = nextChatHistoryAction("a@b.com", "b@b.com", "ready");
    expect(action).toEqual({ kind: "restore", key: chatHistoryKey("b@b.com"), clearKey: defaultKey });
  });

  it("persists only once the current user is already restored AND the stream settled", () => {
    expect(nextChatHistoryAction("a@b.com", "a@b.com", "ready")).toEqual({ kind: "persist", key: userKey });
    expect(nextChatHistoryAction("a@b.com", "a@b.com", "error")).toEqual({ kind: "persist", key: userKey });
  });

  it("does NOT persist mid-stream (#7 — no per-token writes)", () => {
    expect(nextChatHistoryAction("a@b.com", "a@b.com", "submitted")).toEqual({ kind: "noop" });
    expect(nextChatHistoryAction("a@b.com", "a@b.com", "streaming")).toEqual({ kind: "noop" });
  });
});
