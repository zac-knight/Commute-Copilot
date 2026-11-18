import { useEffect, useId, useState } from "react";
import type { PendingAction } from "../api/types";
import { actionKind, approvalRows, confirmation } from "../lib/diff";
import { minutesUntil, toBrisbaneIso, toLocalInput } from "../lib/format";
import { useApp } from "../state/AppContext";
import { Button, Card, Icon, Spinner } from "./ui";

const KIND_LABEL = { create: "Add event", update: "Change event", delete: "Delete event", other: "Change" } as const;

function Rows({ action }: { action: PendingAction }) {
  const rows = approvalRows(action);
  const kind = actionKind(action.type);
  if (rows.length === 0) return <pre className="whitespace-pre-wrap text-sm">{action.diff}</pre>;
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-sm">
      {rows.map((r) => (
        <div key={r.label} className="contents">
          <dt className="font-semibold text-muted">{r.label}</dt>
          <dd className="[overflow-wrap:anywhere]">
            {r.before !== undefined && <span className={kind === "delete" ? "font-semibold line-through decoration-bad decoration-2" : "text-muted line-through"}>{r.before}</span>}
            {r.before !== undefined && r.after !== undefined && <span aria-label="changes to" className="mx-1.5 text-muted">→</span>}
            {r.after !== undefined && <span className="font-semibold">{r.after}</span>}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function EditForm({ action, onDone }: { action: PendingAction; onDone: () => void }) {
  const { approvals } = useApp();
  const kind = actionKind(action.type);
  const p = action.payload as Record<string, string | undefined> & { changes?: Record<string, string | undefined>; before?: Record<string, string | undefined> };
  const src = kind === "update" ? { ...(p.before ?? {}), ...(p.changes ?? {}) } : p;
  const [f, setF] = useState({
    title: src.title ?? "", start: toLocalInput(src.start ?? ""), end: toLocalInput(src.end ?? ""),
    location: src.location ?? "", description: src.description ?? "",
  });
  const id = useId();
  const busy = approvals.busy.has(action.id);
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF((x) => ({ ...x, [k]: e.target.value }));
  const field = "mt-1 w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-sm";

  const save = async () => {
    const values = { title: f.title, start: toBrisbaneIso(f.start), end: toBrisbaneIso(f.end), location: f.location, description: f.description };
    await approvals.edit(action, kind === "update" ? { event_id: p.event_id, changes: values } : values);
    onDone();
  };
  return (
    <form className="space-y-2" onSubmit={(e) => { e.preventDefault(); void save(); }} aria-label="Edit proposal">
      <label className="block text-sm font-semibold" htmlFor={`${id}-t`}>Title<input id={`${id}-t`} required maxLength={200} className={field} value={f.title} onChange={set("title")} /></label>
      <div className="grid grid-cols-2 gap-2">
        <label className="block text-sm font-semibold" htmlFor={`${id}-s`}>Starts<input id={`${id}-s`} required type="datetime-local" className={field} value={f.start} onChange={set("start")} /></label>
        <label className="block text-sm font-semibold" htmlFor={`${id}-e`}>Ends<input id={`${id}-e`} required type="datetime-local" className={field} value={f.end} onChange={set("end")} /></label>
      </div>
      <label className="block text-sm font-semibold" htmlFor={`${id}-l`}>Location<input id={`${id}-l`} maxLength={200} className={field} value={f.location} onChange={set("location")} /></label>
      <label className="block text-sm font-semibold" htmlFor={`${id}-d`}>Notes<textarea id={`${id}-d`} maxLength={2000} rows={2} className={field} value={f.description} onChange={set("description")} /></label>
      <div className="flex gap-2">
        <Button variant="primary" type="submit" disabled={busy}>{busy && <Spinner />}Save changes</Button>
        <Button onClick={onDone}>Cancel</Button>
      </div>
      <p className="text-xs text-muted">Saving changes the proposal. You then approve the new version.</p>
    </form>
  );
}

function PendingCard({ action }: { action: PendingAction }) {
  const { approvals } = useApp();
  const [mode, setMode] = useState<"view" | "edit" | "reject">("view");
  const [reason, setReason] = useState("");
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 15000); return () => clearInterval(t); }, []);
  const busy = approvals.busy.has(action.id);
  const err = approvals.errors[action.id];
  const kind = actionKind(action.type);
  const left = minutesUntil(action.expires_at, now);
  return (
    <Card className="p-4">
      <article aria-label={action.summary} data-testid="approval-card" data-action-id={action.id}>
        <div className="mb-2 flex items-center gap-2">
          <span className="rounded-full bg-warn-bg px-2.5 py-0.5 text-xs font-semibold text-warn">{KIND_LABEL[kind]}</span>
          <span className="ml-auto text-xs text-muted">{left > 0 ? `expires in ${left} min` : "expired"}</span>
        </div>
        {mode === "edit" ? <EditForm action={action} onDone={() => setMode("view")} /> : <Rows action={action} />}
        {mode === "view" && (
          <>
            <details className="mt-3 text-xs text-muted">
              <summary className="cursor-pointer font-semibold">Exact text the system will act on</summary>
              <pre className="mt-1 overflow-x-auto whitespace-pre-wrap rounded-lg bg-surface2 p-2 text-ink">{action.diff}</pre>
              <p className="mt-1 font-mono">sha256 {action.payload_hash.slice(0, 16)}…  ·  version {action.version}</p>
            </details>
            {err && <p role="alert" className="mt-2 rounded-lg bg-bad-bg px-2.5 py-1.5 text-sm text-bad">{err}</p>}
            <div className="mt-3 flex flex-wrap gap-2">
              <Button variant="primary" disabled={busy} onClick={() => void approvals.approve(action)}>{busy ? <Spinner /> : <Icon.Check />}Approve</Button>
              {kind !== "delete" && kind !== "other" && <Button disabled={busy} onClick={() => setMode("edit")}>Edit</Button>}
              <Button variant="danger" disabled={busy} onClick={() => setMode("reject")}>Reject</Button>
            </div>
          </>
        )}
        {mode === "reject" && (
          <div className="mt-3 space-y-2">
            <label className="block text-sm font-semibold">Why? (optional, helps me suggest something better)
              <input className="mt-1 w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-sm" maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
            </label>
            <div className="flex gap-2">
              <Button variant="danger" disabled={busy} onClick={() => void approvals.reject(action, reason.trim() || undefined)}>Confirm reject</Button>
              <Button onClick={() => setMode("view")}>Back</Button>
            </div>
          </div>
        )}
      </article>
    </Card>
  );
}

function RecentRow({ a }: { a: PendingAction }) {
  const tone = a.status === "executed" ? "text-ok" : a.status === "failed" ? "text-bad" : "text-muted";
  const text = a.status === "executed" ? confirmation(a) : a.status === "rejected" ? `Rejected: ${a.summary}` : a.status === "expired" ? `Expired: ${a.summary}` : `${a.summary}${a.error ? `: ${a.error}` : ""}`;
  return <li className={"flex items-start gap-2 text-sm " + tone} data-testid="recent-action" data-status={a.status}><span className="mt-0.5">{a.status === "executed" ? <Icon.Check /> : a.status === "failed" ? <Icon.X /> : <Icon.Minus />}</span><span className="[overflow-wrap:anywhere]">{text}</span></li>;
}

/** Sticky panel: everything the assistant has proposed that is waiting for YOU. Nothing changes until you approve. */
export function ApprovalsPanel() {
  const { approvals } = useApp();
  const n = approvals.pending.length;
  // Hidden (on small screens) only until the number of pending items changes, so a new proposal always shows.
  const [hiddenAt, setHiddenAt] = useState<number | null>(null);
  const open = hiddenAt !== n;
  return (
    <section id="approvals" aria-labelledby="approvals-h" className="space-y-3" data-testid="approvals-panel">
      <div className="flex items-center gap-2">
        <h2 id="approvals-h" className="label flex items-center gap-2"><Icon.Shield />Needs your approval</h2>
        <span aria-label={`${n} pending`} className={"rounded-full px-2 py-0.5 text-xs font-bold " + (n ? "bg-warn-bg text-warn" : "bg-surface2 text-muted")}>{n}</span>
        <button type="button" className="ml-auto text-xs font-semibold text-muted underline lg:hidden" aria-expanded={open} onClick={() => setHiddenAt(open ? n : null)}>{open ? "Hide" : "Show"}</button>
      </div>
      <div className={open ? "space-y-3" : "hidden lg:block lg:space-y-3"}>
        {n === 0 && <p className="border border-dashed border-line p-3 text-xs text-muted">Nothing is waiting. Proposed calendar changes appear here; <strong>nothing changes until you approve</strong>.</p>}
        {approvals.pending.map((a) => <PendingCard key={`${a.id}-${a.version}`} action={a} />)}
        {approvals.recent.length > 0 && (
          <div>
            <h3 className="mb-1 text-xs font-bold uppercase tracking-wide text-muted">Recent</h3>
            <ul className="space-y-1.5">{approvals.recent.map((a) => <RecentRow key={a.id} a={a} />)}</ul>
          </div>
        )}
      </div>
    </section>
  );
}
