import { describe, expect, it } from "vitest";
import { domainOf, scale, ticks } from "./chartmath";
import { fmtDelayS, fmtRate } from "./format";

describe("domainOf", () => {
  it("always includes zero", () => {
    expect(domainOf([120, 300]).min).toBe(0);
    expect(domainOf([-60, 120]).min).toBeLessThan(-60);
    expect(domainOf([]).max).toBeGreaterThan(0);
  });
  it("pads above the largest value so whiskers are visible", () => {
    expect(domainOf([100]).max).toBeGreaterThan(100);
  });
  it("ignores NaN", () => {
    expect(domainOf([NaN, 50]).max).toBeGreaterThan(50);
  });
});

describe("scale and ticks", () => {
  it("maps the domain onto the pixel range", () => {
    const d = { min: 0, max: 100 };
    expect([scale(d, 0, 200), scale(d, 50, 200), scale(d, 100, 200)]).toEqual([0, 100, 200]);
  });
  it("produces nice, bounded ticks", () => {
    const t = ticks({ min: 0, max: 640 });
    expect(t[0]).toBe(0);
    expect(t.length).toBeLessThanOrEqual(8);
    expect(t.every((x) => x % 100 === 0 || x % 200 === 0 || x % 50 === 0)).toBe(true);
    expect(ticks({ min: -120, max: 300 })).toContain(0);
    expect(ticks({ min: 0, max: 0 })).toEqual([0]);
  });
});

describe("delay formatting", () => {
  it("calls small delays on time and signs the rest", () => {
    expect(fmtDelayS(10)).toBe("on time");
    expect(fmtDelayS(150)).toBe("+2.5 min");
    expect(fmtDelayS(-90)).toBe("-1.5 min");
    expect(fmtDelayS(NaN)).toBe("–");
  });
  it("shows a rate with its interval", () => {
    expect(fmtRate({ rate: 0.92, lo: 0.79, hi: 0.97 })).toBe("92% (79%–97%)");
  });
});
