export type Tier = "user" | "admin" | "owner";

export type Folder = {
  id: number;
  parent_id: number | null;
  name: string;
  tier: Tier;
  origin: "manual" | "folder" | "repo";
  doc_count: number;
  writable: boolean;
};

export type FlatFolder = Folder & { depth: number };

/** Depth-first flatten: each parent immediately followed by its children, roots
 *  in input order. Children inherit depth = parent.depth + 1. */
export function flattenTree(folders: Folder[]): FlatFolder[] {
  const byParent = new Map<number | null, Folder[]>();
  for (const f of folders) {
    const key = f.parent_id;
    (byParent.get(key) ?? byParent.set(key, []).get(key)!).push(f);
  }
  const out: FlatFolder[] = [];
  const walk = (parent: number | null, depth: number) => {
    for (const f of byParent.get(parent) ?? []) {
      out.push({ ...f, depth });
      walk(f.id, depth + 1);
    }
  };
  walk(null, 0);
  return out;
}

/** Folders a caller may upload into: server already set `writable`
 *  (rank ≥ tier ∧ manual); this is the picker's source list. */
export function writableFolders(folders: Folder[]): Folder[] {
  return folders.filter((f) => f.writable);
}

export type UploadState = {
  status: "idle" | "uploading" | "done" | "error";
  file: File | null;
  dests: number[];
  done: number;
  error: string | null;
};

export type UploadAction =
  | { type: "start"; file: File; dests: number[] }
  | { type: "progress" }
  | { type: "error"; error: string }
  | { type: "reset" };

/** Drives the upload modal: one `progress` per finished destination; flips to
 *  `done` when every destination is uploaded. */
export function uploadReducer(state: UploadState, action: UploadAction): UploadState {
  switch (action.type) {
    case "start":
      return { status: "uploading", file: action.file, dests: action.dests, done: 0, error: null };
    case "progress": {
      const done = state.done + 1;
      return { ...state, done, status: done >= state.dests.length ? "done" : "uploading" };
    }
    case "error":
      return { ...state, status: "error", error: action.error };
    case "reset":
      return { status: "idle", file: null, dests: [], done: 0, error: null };
  }
}
