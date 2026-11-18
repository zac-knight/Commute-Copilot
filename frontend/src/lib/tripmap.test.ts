import { describe, expect, it } from "vitest";
import { bearing, fmtDist, legShares, makeProjection, mapHeight, niceScale } from "./tripmap";

describe("makeProjection", () => {
  const p = makeProjection([-27.3, 153.0, -27.1, 153.1], 800, 480);
  it("puts north above south and east right of west", () => {
    expect(p.y(-27.1)).toBeLessThan(p.y(-27.3));
    expect(p.x(153.1)).toBeGreaterThan(p.x(153.0));
  });
  it("keeps everything inside the frame, with padding", () => {
    for (const [lat, lon] of [[-27.3, 153.0], [-27.1, 153.1], [-27.1, 153.0], [-27.3, 153.1]] as const) {
      expect(p.x(lon)).toBeGreaterThanOrEqual(0); expect(p.x(lon)).toBeLessThanOrEqual(800);
      expect(p.y(lat)).toBeGreaterThanOrEqual(0); expect(p.y(lat)).toBeLessThanOrEqual(480);
    }
  });
  it("preserves aspect: equal metres north and east are equal pixels", () => {
    const dLat = 0.01, dLon = 0.01 / Math.cos((-27.2 * Math.PI) / 180); // same ground distance
    const dy = p.y(-27.2) - p.y(-27.2 + dLat), dx = p.x(153.05 + dLon) - p.x(153.05);
    expect(Math.abs(dx - dy)).toBeLessThan(0.01 * Math.abs(dy));
  });
  it("reports a believable scale (about 1.1 km per 0.01 degrees of latitude)", () => {
    const pxFor1km = 1000 / p.metresPerPx;
    expect(Math.abs((p.y(-27.2) - p.y(-27.2 + 1000 / 111_320)) - pxFor1km)).toBeLessThan(0.05);
  });
  it("survives a degenerate box", () => {
    const q = makeProjection([-27.2, 153.0, -27.2, 153.0], 400, 300);
    expect(Number.isFinite(q.x(153.0)) && Number.isFinite(q.y(-27.2))).toBe(true);
  });
});

describe("niceScale", () => {
  it("picks a round length that fits", () => {
    for (const mpp of [3, 12, 40, 180]) {
      const s = niceScale(mpp, 150);
      expect(s.px).toBeLessThanOrEqual(150.0001);
      expect([1, 2, 5].includes(s.metres / 10 ** Math.floor(Math.log10(s.metres)))).toBe(true);
    }
  });
});

describe("formatting and shares", () => {
  it("formats metres and kilometres", () => {
    expect(fmtDist(78)).toBe("78 m"); expect(fmtDist(6855)).toBe("6.86 km"); expect(fmtDist(24730)).toBe("24.7 km");
  });
  it("shares sum to one and tolerate an empty trip", () => {
    const s = legShares([6855, 70, 14319]);
    expect(s.reduce((a, b) => a + b, 0)).toBeCloseTo(1, 10);
    expect(legShares([0, 0])).toEqual([0, 0]);
  });
  it("computes compass bearings", () => {
    expect(Math.round(bearing(0, 0, 1, 0))).toBe(0);
    expect(Math.round(bearing(0, 0, 0, 1))).toBe(90);
    expect(Math.round(bearing(1, 0, 0, 0))).toBe(180);
  });
});

describe("mapHeight", () => {
  it("is taller for a north-south route than an east-west one, and always clamped", () => {
    const tall = mapHeight([-27.4, 153.0, -27.0, 153.1], 1000);
    const wide = mapHeight([-27.2, 152.8, -27.19, 153.3], 1000);
    expect(tall).toBeGreaterThan(wide);
    expect(tall).toBeLessThanOrEqual(640);
    expect(wide).toBeGreaterThanOrEqual(380);
  });
});
