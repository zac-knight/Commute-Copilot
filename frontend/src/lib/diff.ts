/** Turn a pending action into labelled before/after rows for the approval card. */
import type { PendingAction } from "../api/types";
import { fmtRange, fmtWhen } from "./format";

export interface DiffRow { label: string; before?: string; after?: string }
export type ActionKind = "create" | "update" | "delete" | "other";

export function actionKind(type: string): ActionKind {
  if (type.endsWith("create_event")) return "create";
  if (type.endsWith("update_event")) return "update";
  if (type.endsWith("delete_event")) return "delete";
  return "other";
}

const str = (v: unknown): string => (typeof v === "string" ? v : "");
const rec = (v: unknown): Record<string, unknown> => (v && typeof v === "object" ? (v as Record<string, unknown>) : {});

export function approvalRows(a: Pick<PendingAction, "type" | "payload">): DiffRow[] {
  const p = a.payload;
  switch (actionKind(a.type)) {
    case "create": {
      const rows: DiffRow[] = [
        { label: "Title", after: str(p.title) },
        { label: "When", after: fmtRange(str(p.start), str(p.end)) },
      ];
      if (str(p.location)) rows.push({ label: "Where", after: str(p.location) });
      if (str(p.description)) rows.push({ label: "Notes", after: str(p.description) });
      return rows;
    }
    case "update": {
      const before = rec(p.before);
      const changes = rec(p.changes);
      const rows: DiffRow[] = [];
      if (changes.title != null) rows.push({ label: "Title", before: str(before.title), after: str(changes.title) });
      if (changes.start != null || changes.end != null) {
        const bs = str(before.start), be = str(before.end);
        rows.push({
          label: "When",
          before: fmtRange(bs, be),
          after: fmtRange(str(changes.start) || bs, str(changes.end) || be),
        });
      }
      if (changes.location != null) rows.push({ label: "Where", before: str(before.location) || "(none)", after: str(changes.location) || "(none)" });
      if (changes.description != null) rows.push({ label: "Notes", before: str(before.description) || "(none)", after: str(changes.description) || "(none)" });
      return rows;
    }
    case "delete": {
      const before = rec(p.before);
      const rows: DiffRow[] = [
        { label: "Title", before: str(before.title) },
        { label: "When", before: str(before.start) ? fmtRange(str(before.start), str(before.end)) : str(before.when) },
      ];
      if (str(before.location)) rows.push({ label: "Where", before: str(before.location) });
      return rows;
    }
    default:
      return [];
  }
}

/** One-line success message once an action has been executed. */
export function confirmation(a: Pick<PendingAction, "type" | "payload" | "result">): string {
  const r = a.result ?? {};
  switch (actionKind(a.type)) {
    case "create": return `Added “${str(r.title) || str(a.payload.title)}” to your calendar (${fmtWhen(str(a.payload.start))}).`;
    case "update": return `Updated “${str(r.title)}” in your calendar.`;
    case "delete": return r.deleted === false ? "That event was already gone." : `Deleted “${str(rec(a.payload.before).title)}” from your calendar.`;
    default: return "Done.";
  }
}
