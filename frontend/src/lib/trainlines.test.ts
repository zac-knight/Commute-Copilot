import { describe, expect, it } from "vitest";
import { LINE_FAMILIES, OTHER_LINE, lineNames, trainLine } from "./trainlines";

describe("trainLine", () => {
  it("names the line behind a timetable code", () => {
    expect(trainLine("BNBR").primary.name).toBe("Beenleigh");
    expect(trainLine("BDVL").families.map((f) => f.name)).toEqual(["Airport", "Gold Coast"]);
  });
  it("gives a line and its reverse the same colour", () => {
    expect(trainLine("SPRP").primary.colour).toBe(trainLine("RPSP").primary.colour);
    expect(trainLine("BRCA").primary.colour).toBe(trainLine("CABR").primary.colour);
  });
  it("treats Brisbane City as a hub, not a line", () => {
    expect(trainLine("BRFG").families).toHaveLength(1);
    expect(trainLine("BRFG").primary.name).toBe("Ferny Grove");
  });
  it("merges Ipswich and Rosewood, and the Sunshine Coast family", () => {
    expect(trainLine("IPRW").families).toHaveLength(1);
    expect(trainLine("CANA").primary.name).toBe("Caboolture / Sunshine Coast");
  });
  it("falls back to a neutral line for unknown codes", () => {
    expect(trainLine("ZZZZ").primary).toBe(OTHER_LINE);
    expect(trainLine("X").primary).toBe(OTHER_LINE);
  });
  it("describes through-running services with both lines", () => {
    expect(lineNames("SPRP")).toBe("Redcliffe Peninsula / Springfield");
  });
  it("uses a distinct colour for every line", () => {
    expect(new Set(LINE_FAMILIES.map((f) => f.colour)).size).toBe(LINE_FAMILIES.length);
  });
});
