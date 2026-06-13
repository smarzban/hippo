import { describe, expect, it } from "vitest";
import { buildDocIndex, MARKER_RE, processCitations } from "./citations";

const DOCS = [
  { id: 1, path: "docs/polly.md", title: "Polly Telegram Integration" },
  { id: 2, path: "docs/project-x.md", title: "Project X Decision" },
];
const idx = buildDocIndex(DOCS);

// Pull the ⟦N⟧ sentinels out of processed text as the renderer would (each is a
// standalone inline-code node ⟦N⟧). If two markers collided into one code span,
// MARKER_RE would not match and a marker would go missing.
function markerNums(processed: string): number[] {
  return [...processed.matchAll(/`([^`]+)`/g)]
    .map((m) => MARKER_RE.exec(m[1]))
    .filter((m): m is RegExpExecArray => m != null)
    .map((m) => Number(m[1]));
}

describe("processCitations", () => {
  it("resolves a single citation to a numbered, clickable source", () => {
    const { processed, sources } = processCitations(
      "Polly uses a webhook [Polly Telegram Integration > Webhook setup].",
      idx,
    );
    expect(sources).toHaveLength(1);
    expect(sources[0]).toMatchObject({ num: 1, docId: 1, section: "Webhook setup" });
    expect(markerNums(processed)).toEqual([1]);
  });

  it("keeps adjacent citations as two separately-parseable markers", () => {
    // The model often emits two citations back-to-back with no separator.
    const { processed, sources } = processCitations(
      "It routes updates to the poller [Polly Telegram Integration > Overview]" +
        "[Polly Telegram Integration > Webhook setup].",
      idx,
    );
    expect(sources).toHaveLength(2);
    // No "``" run: adjacent backtick-wrapped sentinels must not collide into one span.
    expect(processed).not.toContain("``");
    expect(markerNums(processed)).toEqual([1, 2]);
  });

  it("supports the fullwidth 【…】 citation form", () => {
    const { processed, sources } = processCitations(
      "See 【Project X Decision > Rationale】 for why.",
      idx,
    );
    expect(sources).toHaveLength(1);
    expect(sources[0]).toMatchObject({ num: 1, docId: 2 });
    expect(markerNums(processed)).toEqual([1]);
  });

  it("dedupes repeated citations to the same source+section", () => {
    const { sources } = processCitations(
      "First [Polly Telegram Integration > Overview] and again " +
        "[Polly Telegram Integration > Overview].",
      idx,
    );
    expect(sources).toHaveLength(1);
  });

  it("renders an unresolvable citation as a non-clickable source (docId null)", () => {
    const { sources } = processCitations("[Nonexistent Doc > Section] here.", idx);
    expect(sources).toHaveLength(1);
    expect(sources[0].docId).toBeNull();
  });
});
