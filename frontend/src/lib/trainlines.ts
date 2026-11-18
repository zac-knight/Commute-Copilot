/** Train line identity and colour. The timetable names a route by a code like SPRP (= Springfield + Redcliffe
 *  Peninsula) which means nothing to a rider; this maps the two-letter halves to the real line names and gives
 *  every line its own colour. Through-running services belong to two lines; they take the colour of whichever
 *  comes first in PRIORITY, so a line and its reverse (SPRP / RPSP) always look the same. */
export interface LineFamily { id: string; name: string; colour: string }

const F = {
  airport: { id: "airport", name: "Airport", colour: "#f59e0b" },
  beenleigh: { id: "beenleigh", name: "Beenleigh", colour: "#2563eb" },
  cleveland: { id: "cleveland", name: "Cleveland", colour: "#9333ea" },
  doomben: { id: "doomben", name: "Doomben", colour: "#ec4899" },
  fernyGrove: { id: "ferny-grove", name: "Ferny Grove", colour: "#dc2626" },
  goldCoast: { id: "gold-coast", name: "Gold Coast", colour: "#0891b2" },
  ipswich: { id: "ipswich", name: "Ipswich / Rosewood", colour: "#16a34a" },
  sunshine: { id: "sunshine", name: "Caboolture / Sunshine Coast", colour: "#ea580c" },
  redcliffe: { id: "redcliffe", name: "Redcliffe Peninsula", colour: "#0d9488" },
  shorncliffe: { id: "shorncliffe", name: "Shorncliffe", colour: "#65a30d" },
  springfield: { id: "springfield", name: "Springfield", colour: "#92400e" },
} satisfies Record<string, LineFamily>;

const BY_CODE: Record<string, LineFamily> = {
  BD: F.airport, BN: F.beenleigh, CL: F.cleveland, DB: F.doomben, FG: F.fernyGrove, VL: F.goldCoast,
  IP: F.ipswich, RW: F.ipswich, CA: F.sunshine, NA: F.sunshine, GY: F.sunshine, RP: F.redcliffe, SH: F.shorncliffe, SP: F.springfield,
};
const PRIORITY = ["BD", "BN", "CL", "DB", "FG", "VL", "IP", "RW", "CA", "NA", "GY", "RP", "SH", "SP"];
export const OTHER_LINE: LineFamily = { id: "other", name: "Train", colour: "#64748b" };
export const LINE_FAMILIES: LineFamily[] = Object.values(F);

export interface TrainLineInfo { primary: LineFamily; families: LineFamily[] }

export function trainLine(route: string): TrainLineInfo {
  const codes = route.length === 4 ? [route.slice(0, 2), route.slice(2)] : [];
  const known = codes.filter((c) => BY_CODE[c]).sort((a, b) => PRIORITY.indexOf(a) - PRIORITY.indexOf(b));
  const families = [...new Map(known.map((c) => [BY_CODE[c]!.id, BY_CODE[c]!])).values()];
  return { primary: families[0] ?? OTHER_LINE, families: families.length ? families : [OTHER_LINE] };
}

/** "Springfield / Redcliffe Peninsula" style name for a route's line(s). */
export const lineNames = (route: string): string => trainLine(route).families.map((f) => f.name).join(" / ");
