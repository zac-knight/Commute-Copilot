import { NavLink, Outlet, useLocation } from "react-router-dom";
import { ErrorBoundary } from "./ErrorBoundary";

const TABS = [
  { to: "/agents", label: "Agents", hint: "who can do what" },
  { to: "/analytics", label: "Analytics", hint: "delay patterns" },
  { to: "/evaluation", label: "Evaluation", hint: "how good are the answers" },
  { to: "/traces", label: "Traces", hint: "every request, step by step" },
];

/** The behind-the-scenes pages share one tab bar, so the main navigation stays four items. */
export function InsightsLayout() {
  const loc = useLocation();
  return (
    <div className="flex min-h-full flex-col">
      <div className="sticky top-0 z-20 border-b border-line bg-surface/95 backdrop-blur"><div className="topline" aria-hidden /><div className="px-4 pt-3 md:px-6">
        <p className="text-[12px] font-bold uppercase tracking-wide text-muted">Insights · how the assistant works</p>
        <nav aria-label="Insights" className="-mb-px mt-1 flex gap-1 overflow-x-auto">
          {TABS.map((t) => (
            <NavLink key={t.to} to={t.to} title={t.hint}
              className={({ isActive }) => "shrink-0 border-b-[3px] px-3.5 py-2.5 text-[14px] font-bold transition " + (isActive ? "border-accent text-accent" : "border-transparent text-muted hover:text-ink")}>
              {t.label}
            </NavLink>
          ))}
        </nav>
      </div></div>
      <div className="min-h-0 flex-1"><ErrorBoundary resetKey={loc.pathname}><Outlet /></ErrorBoundary></div>
    </div>
  );
}
