// Citation handling: the model emits inline citations as [path > heading > path]
// or the fullwidth 【…】 variant. We resolve each to an indexed document, number
// them, and rewrite the inline occurrence to a sentinel the renderer turns into a
// clickable footnote marker. A deduped, ordered Sources list comes out alongside.

export type DocMeta = { id: number; path: string; title: string };

// Basenames and titles can collide across documents; the full path is unique
// (documents.path is UNIQUE). Collisions on a non-unique key resolve to AMBIGUOUS
// so we render the citation non-clickable instead of linking the wrong document.
const AMBIGUOUS = "ambiguous";
export type DocIndex = Map<string, DocMeta | typeof AMBIGUOUS>;

// [a > b] (requires a " > " so normal [text](link) markdown is left alone) or 【a > b】.
export const CITE_RE = /【([^】]+)】|\[([^\][\n]+? > [^\][\n]+?)\]/g;

// Sentinel wrapped in backticks so it survives as an inline-code node in markdown.
export const MARKER_RE = /^⟦(\d+)⟧$/; // ⟦N⟧

export function buildDocIndex(docs: DocMeta[]): DocIndex {
  const idx: DocIndex = new Map();
  const add = (key: string | undefined, d: DocMeta) => {
    if (!key) return;
    const k = key.toLowerCase();
    const cur = idx.get(k);
    if (cur === undefined) idx.set(k, d);
    else if (cur !== AMBIGUOUS && cur.id !== d.id) idx.set(k, AMBIGUOUS);
  };
  for (const d of docs) {
    add(d.path, d); // full path — unique, the disambiguating key
    add(d.path.split("/").pop(), d); // basename — may collide
    add(d.title, d); // title — may collide
  }
  return idx;
}

function lookup(idx: DocIndex, key: string | undefined): DocMeta | null {
  if (!key) return null;
  const v = idx.get(key.toLowerCase());
  return v && v !== AMBIGUOUS ? v : null;
}

export type Source = {
  num: number;
  docId: number | null; // null => unresolved or ambiguous (rendered non-clickable)
  title: string; // display name for the document
  section: string; // heading path below the document title, joined with ›
  scrollTarget: string; // last heading segment, used to scroll the drawer
};

function resolveCitation(inner: string, docIndex: DocIndex): Omit<Source, "num"> {
  const segments = inner
    .split(">")
    .map((s) => s.trim())
    .filter(Boolean);
  const seg0 = segments[0] ?? inner.trim();
  const base = (seg0.split("/").pop() || seg0).trim();
  // seg0 as given first (it may be a full path or an exact title — both unique
  // when they resolve), then fall back to basename. A collision on the chosen
  // key returns null => non-clickable, never the wrong document.
  const hit = lookup(docIndex, seg0) ?? lookup(docIndex, base);

  let rest = segments.slice(1);
  const title = hit ? hit.title : seg0.split("/").pop() || seg0;
  // The chunker prepends the H1 (== doc title) to every heading path; drop it when
  // it duplicates the resolved title so "Title › Title › Overview" reads "Title › Overview".
  if (hit && rest.length && rest[0].toLowerCase() === hit.title.toLowerCase()) {
    rest = rest.slice(1);
  }
  return {
    docId: hit ? hit.id : null,
    title,
    section: rest.join(" › "),
    scrollTarget: rest.length ? rest[rest.length - 1] : "",
  };
}

export function processCitations(
  text: string,
  docIndex: DocIndex,
): { processed: string; sources: Source[] } {
  const sources: Source[] = [];
  const keyToNum = new Map<string, number>();
  // Citations the model emits back-to-back ([a > b][c > d]) would otherwise produce
  // two adjacent backtick-wrapped sentinels (`⟦1⟧``⟦2⟧`); CommonMark reads the middle
  // `` as one span and the marker is lost. Insert a space between sentinels whose
  // source citations were adjacent so each survives as its own inline-code node.
  let prevEnd = -1;
  const processed = text.replace(CITE_RE, (m: string, a: string, b: string, offset: number) => {
    const inner = (a ?? b ?? "").trim();
    const r = resolveCitation(inner, docIndex);
    const key = `${r.docId ?? r.title}::${r.scrollTarget}`;
    let num = keyToNum.get(key);
    if (num === undefined) {
      num = sources.length + 1;
      keyToNum.set(key, num);
      sources.push({ num, ...r });
    }
    const sep = offset === prevEnd ? " " : "";
    prevEnd = offset + m.length;
    return sep + "`⟦" + num + "⟧`";
  });
  return { processed, sources };
}
