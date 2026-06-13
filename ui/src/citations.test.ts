import { describe, expect, it } from "vitest";
import {
  buildDocIndex,
  MARKER_RE,
  NO_SOURCES_MARKER,
  processCitations,
  stripNoSourcesMarker,
} from "./citations";

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

  it("separates a citation touching an inline-code span on either side", () => {
    // code span glued directly to the citation: `make`[...]
    const before = processCitations("Run `make`[Polly Telegram Integration > Webhook setup].", idx);
    expect(before.processed).not.toContain("``");
    expect(markerNums(before.processed)).toEqual([1]);

    // citation glued directly to a following code span: [...]`make`
    const after = processCitations("[Polly Telegram Integration > Webhook setup]`make` runs it.", idx);
    expect(after.processed).not.toContain("``");
    expect(markerNums(after.processed)).toEqual([1]);
  });

  it("handles three citations back-to-back", () => {
    const { processed, sources } = processCitations(
      "[Polly Telegram Integration > Overview]" +
        "[Polly Telegram Integration > Webhook setup]" +
        "[Project X Decision > Rationale]",
      idx,
    );
    expect(sources).toHaveLength(3);
    expect(processed).not.toContain("``");
    expect(markerNums(processed)).toEqual([1, 2, 3]);
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

describe("stripNoSourcesMarker", () => {
  it("detects and removes a trailing refusal marker", () => {
    const raw = `I couldn't find anything about that.\n${NO_SOURCES_MARKER}`;
    const { text, refused } = stripNoSourcesMarker(raw);
    expect(refused).toBe(true);
    expect(text).toBe("I couldn't find anything about that.");
    expect(text).not.toContain(NO_SOURCES_MARKER);
  });

  it("leaves a normal answer untouched and reports refused=false", () => {
    const raw = "Polly uses a webhook.";
    const { text, refused } = stripNoSourcesMarker(raw);
    expect(refused).toBe(false);
    expect(text).toBe(raw);
  });
});
