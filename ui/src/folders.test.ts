// ui/src/folders.test.ts
import { describe, expect, it } from "vitest";
import {
  type Folder,
  flattenTree,
  writableFolders,
  uploadReducer,
  type UploadState,
} from "./folders";

const TREE: Folder[] = [
  { id: 1, parent_id: null, name: "Default", tier: "user", origin: "manual", doc_count: 0, writable: true },
  { id: 2, parent_id: 1, name: "Retail", tier: "user", origin: "manual", doc_count: 2, writable: true },
  { id: 3, parent_id: null, name: "Private", tier: "admin", origin: "manual", doc_count: 0, writable: true },
  { id: 4, parent_id: 1, name: "Mirror", tier: "user", origin: "folder", doc_count: 5, writable: false },
];

describe("flattenTree", () => {
  it("orders children under parents with depth", () => {
    const flat = flattenTree(TREE);
    expect(flat.map((f) => [f.id, f.depth])).toEqual([
      [1, 0], [2, 1], [4, 1], [3, 0],
    ]);
  });
});

describe("writableFolders", () => {
  it("keeps only manual + writable folders", () => {
    expect(writableFolders(TREE).map((f) => f.id)).toEqual([1, 2, 3]);
  });
});

describe("uploadReducer", () => {
  const init: UploadState = { status: "idle", file: null, dests: [], done: 0, error: null };
  it("walks idle → uploading → done", () => {
    let s = uploadReducer(init, { type: "start", file: { name: "a.md" } as File, dests: [1, 2] });
    expect(s.status).toBe("uploading");
    s = uploadReducer(s, { type: "progress" });
    expect(s.done).toBe(1);
    s = uploadReducer(s, { type: "progress" });
    expect(s).toMatchObject({ status: "done", done: 2 });
  });
  it("captures failure", () => {
    let s = uploadReducer(init, { type: "start", file: { name: "a.md" } as File, dests: [1] });
    s = uploadReducer(s, { type: "error", error: "too large" });
    expect(s).toMatchObject({ status: "error", error: "too large" });
  });
});
