import { useState } from "react";
import type { StepView } from "../lib/chat";
import { fmtMs } from "../lib/format";
import { AgentBadge, Icon, StatusChip } from "./ui";

/** The visible plan: which agents run, in what order, and where each one is up to. */
export function PlanView({ label, steps, streaming }: { label: string; steps: StepView[]; streaming: boolean }) {
  const [open, setOpen] = useState(true);
  if (steps.length === 0) return null;
  const done = steps.filter((s) => s.status === "ok").length;
  const panelId = "plan-steps-" + steps.map((s) => s.id).join("");
  return (
    <div className="overflow-hidden rounded-2xl border border-line bg-surface" data-testid="plan">
      <button
        type="button" aria-expanded={open} aria-controls={panelId} onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-[13px] font-bold hover:bg-surface2"
      >
        <span className={"transition " + (open ? "rotate-90" : "")}><Icon.Chevron /></span>
        <span>Plan: {label}</span>
        <span className="ml-auto text-xs font-normal text-muted">{streaming ? `${done} of ${steps.length} done…` : `${done} of ${steps.length} succeeded`}</span>
      </button>
      {open && (
        <ol id={panelId} className="divide-y divide-line px-1 pb-1">
          {steps.map((s) => (
            <li key={s.id} data-testid={`step-${s.id}`} className="px-2 py-1.5">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className="text-[12px] font-bold text-muted">{s.id}</span>
                <AgentBadge agent={s.agent} />
                <StatusChip status={s.status} />
                {s.dependsOn.length > 0 && <span className="text-[12px] text-muted">after {s.dependsOn.join(",")}</span>}
                {s.durationMs > 0 && <span className="ml-auto text-[12px] text-muted">{fmtMs(s.durationMs)}</span>}
              </div>
              <p className="mt-1 text-[12.5px] [overflow-wrap:anywhere]">{s.task}</p>
              {s.toolCalls.length > 0 && <p className="mt-0.5 text-[12px] text-muted">→ {[...new Set(s.toolCalls)].join(", ")}</p>}
              {s.error && <p role="alert" className="mt-1 text-xs text-bad">{s.error}</p>}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
