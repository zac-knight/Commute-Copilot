import type { ReactNode } from "react";
import type { AgentCard, Mode } from "../api/types";
import { Icon } from "./ui";

const META: Record<Mode, { label: string; blurb: string; icon: ReactNode; ring: string; text: string }> = {
  auto: { label: "Auto", blurb: "The router picks agents and combines them", icon: <Icon.Spark />, ring: "ring-accent", text: "text-accent" },
  transit: { label: "Transit", blurb: "Buses, trains, delays, trip plans", icon: <Icon.Bus />, ring: "ring-transit", text: "text-transit" },
  weather: { label: "Weather", blurb: "Forecast and leave-now-or-wait", icon: <Icon.Cloud />, ring: "ring-weather", text: "text-weather" },
  calendar: { label: "Calendar", blurb: "Free time, conflicts, approved changes", icon: <Icon.Calendar />, ring: "ring-calendar", text: "text-calendar" },
};
export const MODES: Mode[] = ["auto", "transit", "weather", "calendar"];
export const modeLabel = (m: Mode) => META[m].label;

/** Choose who answers: Auto (router + any combination) or one specialist directly. */
export function AgentPicker({ mode, onChange, agents, disabled }: { mode: Mode; onChange: (m: Mode) => void; agents: AgentCard[] | null; disabled?: boolean }) {
  const off = (m: Mode) => m !== "auto" && agents != null && agents.find((a) => a.name === m)?.available === false;
  return (
    <div role="radiogroup" aria-label="Who should answer" className="flex flex-wrap gap-1.5">
      {MODES.map((m) => {
        const x = META[m], on = mode === m, unavailable = off(m);
        return (
          <button key={m} type="button" role="radio" aria-checked={on} disabled={disabled || unavailable} data-testid={`mode-${m}`}
            onClick={() => onChange(m)} title={unavailable ? "Not available (no data loaded)" : x.blurb}
            className={"flex items-center gap-1.5 rounded-full border px-3.5 py-1.5 text-[13px] font-bold transition disabled:opacity-40 " + (on ? `border-transparent pill-on shadow` : "border-line bg-surface text-muted hover:border-muted hover:text-ink")}>
            {x.icon}{x.label}
          </button>
        );
      })}
    </div>
  );
}
