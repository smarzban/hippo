import { describe, expect, it } from "vitest";
import { WIZARD_STEPS, nextStep, stepValid, type SetupState } from "./setup";

const base: SetupState = {
  step: 0, token: "", authMode: "password", ownerEmail: "", ownerPassword: "",
  roots: { user: "Default", admin: "Private", owner: "Owner" },
  models: { chat_model: "", embedding_model: "", embedding_dim: 1536 },
};

describe("wizard", () => {
  it("has the expected ordered steps", () => {
    expect(WIZARD_STEPS).toEqual(["token", "auth", "owner", "roots", "models", "finish"]);
  });
  it("token step needs a token", () => {
    expect(stepValid({ ...base, step: 0, token: "" })).toBe(false);
    expect(stepValid({ ...base, step: 0, token: "abc" })).toBe(true);
  });
  it("password owner step needs email + 8-char password", () => {
    expect(stepValid({ ...base, step: 2, ownerEmail: "o@x.com", ownerPassword: "short" })).toBe(false);
    expect(stepValid({ ...base, step: 2, ownerEmail: "o@x.com", ownerPassword: "longenough" })).toBe(true);
  });
  it("nextStep advances but clamps at the last step", () => {
    expect(nextStep({ ...base, step: 0, token: "abc" }).step).toBe(1);
    expect(nextStep({ ...base, step: 5 }).step).toBe(5);
  });
});
