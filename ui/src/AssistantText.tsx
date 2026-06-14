import { useMemo } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  type DocIndex,
  MARKER_RE,
  processCitations,
  stripNoSourcesMarker,
} from "./citations";

/** Renders one assistant message: markdown + inline citation refs + the Sources
 * list, and the "no sources cited" advisory when a substantial answer carries no
 * citation and no refusal marker. Citation refs and source entries open the doc
 * drawer via onOpen. */
export default function AssistantText({
  text,
  docIndex,
  onOpen,
}: {
  text: string;
  docIndex: DocIndex;
  onOpen: (id: number, section: string) => void;
}) {
  const { text: grounded, refused } = useMemo(() => stripNoSourcesMarker(text), [text]);
  const { processed, sources } = useMemo(
    () => processCitations(grounded, docIndex),
    [grounded, docIndex],
  );

  const components = useMemo(
    () => ({
      code({ className, children, ...props }: React.ComponentProps<"code">) {
        const m = MARKER_RE.exec(String(children));
        if (m) {
          const num = Number(m[1]);
          const src = sources.find((s) => s.num === num);
          const clickable = !!src && src.docId != null;
          return (
            <sup
              className={`cite-ref${clickable ? " clickable" : ""}`}
              title={src ? src.title + (src.section ? ` › ${src.section}` : "") : ""}
              onClick={clickable ? () => onOpen(src!.docId!, src!.scrollTarget) : undefined}
            >
              {num}
            </sup>
          );
        }
        return (
          <code className={className} {...props}>
            {children}
          </code>
        );
      },
    }),
    [sources, onOpen],
  );

  return (
    <div className="md">
      <Markdown remarkPlugins={[remarkGfm]} components={components}>
        {processed}
      </Markdown>
      {sources.length > 0 && (
        <div className="sources">
          <span className="sources-label">Sources</span>
          <ol>
            {sources.map((s) => (
              <li key={s.num}>
                {s.docId != null ? (
                  <button className="source-link" onClick={() => onOpen(s.docId!, s.scrollTarget)}>
                    {s.title}
                    {s.section && <span className="sec"> › {s.section}</span>}
                  </button>
                ) : (
                  <span className="source-dead">
                    {s.title}
                    {s.section && ` › ${s.section}`}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </div>
      )}
      {sources.length === 0 && !refused && processed.trim().length > 120 && (
        <p className="no-sources" title="Hippo should answer only from indexed docs with citations.">
          ⚠ No sources cited — verify independently.
        </p>
      )}
    </div>
  );
}
