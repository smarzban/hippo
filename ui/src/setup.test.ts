import { describe, expect, it } from "vitest";
import {
  buildSetupPayload,
  canSubmit,
  fieldErrors,
  type SetupState,
} from "./setup";

const base: SetupState = {
  token: "",
  authMode: "password",
  ownerEmail: "",
  ownerPassword: "",
  models: { chat_model: "", embedding_model: "", embedding_dim: 1536 },
};

describe("setup form", () => {
  it("requires token, valid email, and an 8-char password in password mode", () => {
    expect(canSubmit(base)).toBe(false);
    expect(
      canSubmit({ ...base, token: "t", ownerEmail: "o@x.com", ownerPassword: "longenough" }),
    ).toBe(true);
    expect(
      canSubmit({ ...base, token: "t", ownerEmail: "o@x.com", ownerPassword: "short" }),
    ).toBe(false);
    expect(
      canSubmit({ ...base, token: "t", ownerEmail: "nope", ownerPassword: "longenough" }),
    ).toBe(false);
  });

  it("does not require a password in oidc/iap mode", () => {
    expect(canSubmit({ ...base, token: "t", ownerEmail: "o@x.com", authMode: "oidc" })).toBe(true);
  });

  it("flags a malformed email only once it has been typed", () => {
    expect(fieldErrors(base).email).toBeUndefined();
    expect(fieldErrors({ ...base, ownerEmail: "nope" }).email).toBeTruthy();
    expect(fieldErrors({ ...base, ownerEmail: "o@x.com" }).email).toBeUndefined();
  });

  it("flags a short password only once it has been typed", () => {
    expect(fieldErrors(base).password).toBeUndefined();
    expect(fieldErrors({ ...base, ownerPassword: "short" }).password).toBeTruthy();
    expect(fieldErrors({ ...base, ownerPassword: "longenough" }).password).toBeUndefined();
  });

  it("payload omits the dropped folder step", () => {
    const p = buildSetupPayload({
      ...base,
      token: "t",
      ownerEmail: "o@x.com",
      ownerPassword: "longenough",
    });
    expect(p).not.toHaveProperty("roots");
    expect(p.owner_email).toBe("o@x.com");
    expect(p.auth_mode).toBe("password");
  });
});
