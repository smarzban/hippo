import { describe, expect, it } from "vitest";
import { passwordChangeError, MIN_PASSWORD_LEN } from "./auth";

describe("passwordChangeError", () => {
  it("requires all fields", () => {
    expect(passwordChangeError("", "newlongpass", "newlongpass")).toMatch(/current/i);
  });
  it("enforces minimum length", () => {
    expect(passwordChangeError("cur", "short", "short")).toMatch(new RegExp(`${MIN_PASSWORD_LEN}`));
  });
  it("requires confirmation match", () => {
    expect(passwordChangeError("cur", "newlongpass", "different")).toMatch(/match/i);
  });
  it("returns null when valid", () => {
    expect(passwordChangeError("cur", "newlongpass", "newlongpass")).toBeNull();
  });
});
