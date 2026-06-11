// Citation handling: the model emits inline citations as [path > heading > path]
// or the fullwidth 【…】 variant. We resolve each to an indexed document, number
// them, and rewrite the inline occurrence to a sentinel the renderer turns into a
// clickable footnote marker. A deduped, ordered Sources list comes out alongside.

export type DocMeta = { id: number; path: string; title: string };

// [a > b] (requires a " > " so normal [text](link) markdown is left alone) or 【a > b】.
export const CITE_RE = /【([^】]+)】|\[([^\][\n]+? > [^\][\n]+?)\]/g;

// Sentinel wrapped in backticks so it survives as an inline-code node in markdown.
export const MARKER_RE = /^⟦(\d+)⟧$/; // ⟦N⟧

export function buildDocIndex(docs: DocMeta[]): Map<string, DocMeta> {
  const idx = new Map<string, DocMeta>();
  for (const d of docs) {
    const base = (d.path.split("/").pop() || d.path).toLowerCase();
    idx.set(base, d);
    if (d.title) idx.set(d.title.toLowerCase(), d);
  }
  return idx;
}

export type Source = {
  num: number;
  docId: number | null; // null => couldn't resolve to an indexed doc (non-clickable)
  title: string; // display name for the document
  section: string; // heading path below the document title, joined with ›
  scrollTarget: string; // last heading segment, used to scroll the drawer
};

function resolveCitation(inner: string, docIndex: Map<string, DocMeta>): Omit<Source, "num"> {
  const segments = inner
    .split(">")
    .map((s) => s.trim())
    .filter(Boolean);
  const seg0 = segments[0] ?? inner.trim();
  const base = (seg0.split("/").pop() || seg0).trim().toLowerCase();
  const hit = docIndex.get(base) ?? docIndex.get(seg0.toLowerCase());

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
  docIndex: Map<string, DocMeta>,
): { processed: string; sources: Source[] } {
  const sources: Source[] = [];
  const keyToNum = new Map<string, number>();
  const processed = text.replace(CITE_RE, (_m, a: string, b: string) => {
    const inner = (a ?? b ?? "").trim();
    const r = resolveCitation(inner, docIndex);
    const key = `${r.docId ?? r.title}::${r.scrollTarget}`;
    let num = keyToNum.get(key);
    if (num === undefined) {
      num = sources.length + 1;
      keyToNum.set(key, num);
      sources.push({ num, ...r });
    }
    return "`⟦" + num + "⟧`";
  });
  return { processed, sources };
}
