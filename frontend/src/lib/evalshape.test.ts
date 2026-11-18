import { describe, expect, it } from "vitest";
import { normalizeEval } from "./evalshape";

describe("normalizeEval", () => {
  it("survives the old backend's payload (no router/e2e/deterministic)", () => {
    expect(normalizeEval({ available: false, note: "no evaluation has been run yet (Phase 7)" })).toEqual({ available: false, deterministic: null, router: {}, e2e: {} });
  });
  it("survives junk", () => {
    for (const bad of [null, undefined, 42, "x", []]) expect(Object.keys(normalizeEval(bad).router)).toEqual([]);
  });
  it("keeps real data", () => {
    const d = normalizeEval({ available: true, router: { m: { n: 1 } }, e2e: {}, deterministic: null });
    expect(Object.keys(d.router)).toEqual(["m"]);
  });
});
