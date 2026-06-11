import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

type Doc = { id: number; path: string; title: string; content: string };

export function DocDrawer({
  docId,
  section,
  onClose,
}: {
  docId: number;
  section: string;
  onClose: () => void;
}) {
  const [doc, setDoc] = useState<Doc | null>(null);
  const [loading, setLoading] = useState(true);
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setDoc(null);
    fetch(`/documents/${docId}`)
      .then((r) => r.json())
      .then((d) => alive && (setDoc(d), setLoading(false)))
      .catch(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [docId]);

  // Scroll to the cited heading once content has rendered.
  useEffect(() => {
    const body = bodyRef.current;
    if (!doc || !body) return;
    const target = section.trim().toLowerCase();
    if (!target) {
      body.scrollTop = 0;
      return;
    }
    const headings = Array.from(body.querySelectorAll("h1,h2,h3,h4,h5,h6"));
    const match = headings.find((h) => (h.textContent || "").trim().toLowerCase() === target);
    if (match) {
      match.scrollIntoView({ block: "start" });
      match.classList.add("hl");
    } else {
      body.scrollTop = 0;
    }
  }, [doc, section]);

  // Close on Escape.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()}>
        <header className="drawer-head">
          <span className="drawer-path">{doc ? doc.path.split("/").pop() : "…"}</span>
          <button className="drawer-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>
        <div className="drawer-body md" ref={bodyRef}>
          {loading ? (
            <p className="muted">Loading…</p>
          ) : doc ? (
            <Markdown remarkPlugins={[remarkGfm]}>{doc.content}</Markdown>
          ) : (
            <p className="error">Couldn't load that document.</p>
          )}
        </div>
      </aside>
    </div>
  );
}
