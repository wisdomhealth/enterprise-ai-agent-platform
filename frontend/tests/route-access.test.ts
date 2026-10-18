import { hasApplicationAccess } from "../lib/application-access";

const access = [
  { capability: "email", effective_allowed: true },
  { capability: "support", effective_allowed: false },
] as const;

it("allows only the effective application capability", () => {
  expect(hasApplicationAccess(access, "email")).toBe(true);
  expect(hasApplicationAccess(access, "support")).toBe(false);
});
