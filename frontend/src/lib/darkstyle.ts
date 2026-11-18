/** A purpose-made dark basemap: the OpenFreeMap "positron" style recoloured layer by layer (instead of a CSS
 *  invert, which leaves roads and labels murky). Roads step up in brightness with importance, motorways are
 *  amber, labels are light with dark halos, water and parks keep distinct hues. */
const SOURCE = "https://tiles.openfreemap.org/styles/positron";

const C = {
  bg: "#171c25", park: "#1d2b26", wood: "#1b2924", residential: "#1c2230", water: "#1f3550", waterway: "#2b4c72",
  building: "#262d3a", buildingLine: "#333c4c", casing: "#0f131a", minor: "#465163", path: "#3a4454", major: "#8a96ab",
  majorSubtle: "rgba(138,150,171,0.35)", motorway: "#d9984a", motorwaySubtle: "rgba(217,152,74,0.35)", tunnel: "#394252",
  rail: "#566073", railDash: "#98a3b6", boundary: "#5f6a7e", runway: "#475163", taxiway: "#394252", aeroArea: "#252c38", pier: "#171c25",
  text: "#e7ecf5", textSoft: "#b4bfd1", textWater: "#8dbcf2", halo: "#0f131a",
};

const COLOURS: Record<string, Record<string, string>> = {
  background: { "background-color": C.bg },
  park: { "fill-color": C.park }, landcover_wood: { "fill-color": C.wood }, landuse_residential: { "fill-color": C.residential },
  landcover_ice_shelf: { "fill-color": C.bg }, landcover_glacier: { "fill-color": C.bg },
  water: { "fill-color": C.water }, waterway: { "line-color": C.waterway },
  building: { "fill-color": C.building, "fill-outline-color": C.buildingLine },
  tunnel_motorway_casing: { "line-color": C.casing }, tunnel_motorway_inner: { "line-color": C.tunnel },
  "aeroway-taxiway": { "line-color": C.taxiway }, "aeroway-runway-casing": { "line-color": C.taxiway },
  "aeroway-area": { "fill-color": C.aeroArea }, "aeroway-runway": { "line-color": C.runway },
  road_area_pier: { "fill-color": C.pier }, road_pier: { "line-color": C.pier },
  highway_path: { "line-color": C.path }, highway_minor: { "line-color": C.minor },
  highway_major_casing: { "line-color": C.casing }, highway_major_inner: { "line-color": C.major }, highway_major_subtle: { "line-color": C.majorSubtle },
  highway_motorway_casing: { "line-color": C.casing }, highway_motorway_inner: { "line-color": C.motorway }, highway_motorway_subtle: { "line-color": C.motorwaySubtle },
  highway_motorway_bridge_casing: { "line-color": C.casing }, highway_motorway_bridge_inner: { "line-color": C.motorway },
  railway_transit: { "line-color": C.rail }, railway_transit_dashline: { "line-color": C.railDash },
  railway_service: { "line-color": C.rail }, railway_service_dashline: { "line-color": C.railDash },
  railway: { "line-color": C.rail }, railway_dashline: { "line-color": C.railDash },
  boundary_3: { "line-color": C.boundary }, boundary_2: { "line-color": C.boundary }, boundary_disputed: { "line-color": C.boundary },
};
const TEXT: Record<string, string> = {
  waterway_line_label: C.textWater, water_name_point_label: C.textWater, water_name_line_label: C.textWater,
  "highway-name-path": C.textSoft, "highway-name-minor": C.textSoft, "highway-name-major": C.text,
  airport: C.textSoft, label_other: C.textSoft,
};

interface Layer { id: string; type: string; paint?: Record<string, unknown>; [k: string]: unknown }
interface Style { layers: Layer[]; [k: string]: unknown }

/** Recolour a positron style document into the dark theme (pure, exported for tests). */
export function darken(style: Style): Style {
  const layers = style.layers.map((l) => {
    const paint = { ...(l.paint ?? {}) };
    Object.assign(paint, COLOURS[l.id] ?? {});
    if (l.type === "symbol") {
      paint["text-color"] = TEXT[l.id] ?? C.text;
      paint["text-halo-color"] = C.halo;
      const w = paint["text-halo-width"];
      paint["text-halo-width"] = typeof w === "number" ? Math.max(w, 1.5) : 1.6;
    }
    return { ...l, paint };
  });
  return { ...style, layers };
}

let cached: Promise<Style> | null = null;
export function darkStyle(): Promise<Style> {
  cached ??= fetch(SOURCE).then((r) => {
    if (!r.ok) throw new Error(`style HTTP ${r.status}`);
    return r.json() as Promise<Style>;
  }).then(darken).catch((e: unknown) => { cached = null; throw e; });
  return cached;
}
