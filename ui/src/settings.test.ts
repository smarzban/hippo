import { describe, expect, it } from "vitest";
import { tabsForRole } from "./Settings";

describe("tabsForRole", () => {
  it("gives a plain user only their profile", () => {
    expect(tabsForRole("user")).toEqual(["My Profile"]);
  });
  it("gives admins management tabs but not System config", () => {
    expect(tabsForRole("admin")).toEqual(["Folders", "Users", "My Profile", "Status"]);
  });
  it("gives owners the System config tab too", () => {
    expect(tabsForRole("owner")).toEqual([
      "Folders",
      "Users",
      "My Profile",
      "Status",
      "System config",
    ]);
  });
});
