import { describe, expect, it, vi } from "vitest";
import type { UIMessage } from "ai";
import {
  CHAT_HISTORY_VERSION,
  chatHistoryKey,
  clearMessages,
  loadMessages,
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

const MSGS: UIMessage[] = [
  { id: "1", role: "user", parts: [{ type: "text", text: "hi" }] },
  { id: "2", role: "assistant", parts: [{ type: "text", text: "hello" }] },
];

describe("chatHistoryKey", () => {
  it("namespaces by email when known", () => {
    expect(chatHistoryKey("a@b.com")).toBe("hippo:chat:v1:a@b.com");
  });
  it("falls back to a default key when unknown", () => {
    expect(chatHistoryKey(null)).toBe("hippo:chat:v1:default");
    expect(chatHistoryKey(undefined)).toBe("hippo:chat:v1:default");
    expect(chatHistoryKey("   ")).toBe("hippo:chat:v1:default");
  });
});

describe("loadMessages / saveMessages round-trip", () => {
  it("saves then loads the same messages", () => {
    const s = memStorage();
    const key = chatHistoryKey("a@b.com");
    saveMessages(s, key, MSGS);
    expect(loadMessages(s, key)).toEqual(MSGS);
  });

  it("writes a versioned envelope", () => {
    const s = memStorage();
    saveMessages(s, "k", MSGS);
    expect(JSON.parse(s.getItem("k")!)).toEqual({ v: CHAT_HISTORY_VERSION, messages: MSGS });
  });
});

describe("loadMessages degradation", () => {
  it("returns [] when the key is missing", () => {
    expect(loadMessages(memStorage(), "nope")).toEqual([]);
  });

  it("returns [] on corrupt JSON", () => {
    expect(loadMessages(memStorage({ k: "{not json" }), "k")).toEqual([]);
  });

  it("returns [] on a version mismatch", () => {
    const stale = JSON.stringify({ v: CHAT_HISTORY_VERSION + 1, messages: MSGS });
    expect(loadMessages(memStorage({ k: stale }), "k")).toEqual([]);
  });

  it("returns [] when the payload shape is wrong", () => {
    const bad = JSON.stringify({ v: CHAT_HISTORY_VERSION, messages: "oops" });
    expect(loadMessages(memStorage({ k: bad }), "k")).toEqual([]);
  });

  it("returns [] when getItem throws (private/locked storage)", () => {
    const throwing = {
      ...memStorage(),
      getItem: () => {
        throw new Error("SecurityError");
      },
    } as unknown as Storage;
    expect(loadMessages(throwing, "k")).toEqual([]);
  });
});

describe("saveMessages error swallowing", () => {
  it("does not throw when setItem throws (quota exceeded)", () => {
    const throwing = {
      ...memStorage(),
      setItem: vi.fn(() => {
        throw new Error("QuotaExceededError");
      }),
    } as unknown as Storage;
    expect(() => saveMessages(throwing, "k", MSGS)).not.toThrow();
  });
});

describe("clearMessages", () => {
  it("removes a persisted entry", () => {
    const s = memStorage();
    saveMessages(s, "k", MSGS);
    clearMessages(s, "k");
    expect(loadMessages(s, "k")).toEqual([]);
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
