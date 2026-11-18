import { useEffect, useState, type ReactNode } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useApp } from "../state/AppContext";
import { useTheme } from "../state/theme";
import { ErrorBoundary } from "./ErrorBoundary";
import { Icon } from "./ui";

export const INSIGHT_PATHS = ["/agents", "/analytics", "/evaluation", "/traces"];
const NAV: { to: string; label: string; end?: boolean; icon: ReactNode; hue: string; match?: (p: string) => boolean }[] = [
  { to: "/", label: "Plan", end: true, icon: <Icon.Map />, hue: "#fde68a" },
  { to: "/assistant", label: "Assistant", icon: <Icon.Chat />, hue: "#fbcfe8" },
  { to: "/agents", label: "Insights", icon: <Icon.Chart />, hue: "#fcd34d", match: (p) => INSIGHT_PATHS.some((x) => p.startsWith(x)) },
  { to: "/settings", label: "Settings", icon: <Icon.Gear />, hue: "#fecdd3" },
];

const clock = new Intl.DateTimeFormat("en-AU", { timeZone: "Australia/Brisbane", hour: "numeric", minute: "2-digit", hour12: true });
function useClock(): string {
  const [t, setT] = useState(() => clock.format(new Date()));
  useEffect(() => { const i = setInterval(() => setT(clock.format(new Date())), 15_000); return () => clearInterval(i); }, []);
  return t;
}

/** Four destinations: a slim rail on a laptop (the map keeps the width), a bottom tab bar on a phone. */
export function Layout() {
  const { health, healthError, approvals } = useApp();
  const [theme, toggle] = useTheme();
  const loc = useLocation();
  const time = useClock();
  const n = approvals.pending.length;
  const model = health ? Object.values(health.llm.models)[0]?.model ?? "?" : "…";
  const llm = healthError ? { t: "Offline", short: "Offline", c: "bad" as const } : health?.demo ? { t: "Demo replay", short: "Demo", c: "warn" as const } : health?.llm.reachable === false ? { t: "Model down", short: "LLM down", c: "bad" as const } : health ? { t: "Model online", short: "Online", c: "ok" as const } : { t: "Connecting", short: "…", c: "warn" as const };
  const dot = { ok: "bg-ok", warn: "bg-warn", bad: "bg-bad" }[llm.c];
  return (
    <div className="flex h-dvh flex-col overflow-hidden md:flex-row">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:bg-accent focus:px-3 focus:py-2 focus:text-accent-ink">Skip to content</a>
      <aside className="no-print z-30 order-2 flex shrink-0 items-center justify-between grad-nav px-3 py-1 text-nav-ink md:order-none md:w-[78px] md:flex-col md:justify-start md:px-0 md:py-3">
        <div className="gold-badge hidden h-11 w-11 place-items-center rounded-2xl md:mb-4 md:grid" title="South East Queensland" aria-hidden><Icon.Sun /></div>
        <nav aria-label="Main" className="flex flex-1 items-center justify-around gap-1 md:flex-none md:flex-col md:justify-start md:gap-1.5">
          {NAV.map((x) => {
            const active = x.match ? x.match(loc.pathname) : x.end ? loc.pathname === x.to : loc.pathname.startsWith(x.to);
            return (
              <NavLink key={x.to} to={x.to} end={x.end} title={x.label} aria-current={active ? "page" : undefined}
                className={"relative flex w-16 flex-col items-center gap-0.5 rounded-2xl px-1 py-2 text-[11px] font-semibold transition " + (active ? "bg-white/16 text-white shadow-inner md:before:absolute md:before:-left-1.5 md:before:top-3 md:before:h-7 md:before:w-1 md:before:rounded-full md:before:bg-[#f2b705]" : "text-nav-ink/75 hover:bg-white/10 hover:text-white")}>
                <span className={"grid size-8 place-items-center rounded-xl transition " + (active ? "shadow-md" : "")} style={{ color: x.hue, background: active ? "rgb(255 255 255 / .14)" : "transparent" }}>{x.icon}</span><span>{x.label}</span>
                {x.to === "/assistant" && n > 0 && <span aria-label={`${n} waiting for approval`} className="absolute right-2 top-0.5 min-w-4 rounded-full bg-warn px-1 text-center text-[10.5px] font-extrabold text-white">{n}</span>}
              </NavLink>
            );
          })}
        </nav>
        <div className="hidden items-center gap-2 md:mt-auto md:flex md:flex-col md:pb-1" role="status" aria-label="System status">
          {health?.demo && <span className="rounded-md bg-warn px-1.5 py-0.5 text-[10px] font-extrabold text-white" data-testid="demo-badge">DEMO</span>}
          <span className="flex flex-col items-center gap-0.5 text-center text-[10.5px] leading-tight" title={`${llm.t} · model ${model} · ${health?.agents_available.length ?? 0}/3 agents`}>
            <span className={"size-2.5 rounded-full " + dot} aria-hidden />{llm.short}
          </span>
          <span className="text-[11px] font-bold tabular-nums" title="Brisbane time">{time}</span>
          <button type="button" onClick={toggle} data-tip={theme === "dark" ? "Light theme" : "Dark theme"} aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"} className="tip tip-right grid size-9 place-items-center rounded-xl text-nav-ink/80 hover:bg-white/10 hover:text-white">
            {theme === "dark" ? <Icon.Sun /> : <Icon.Moon />}
          </button>
        </div>
      </aside>
      <main id="main" className="relative min-h-0 min-w-0 flex-1 overflow-y-auto">
        <div key={loc.pathname} className="rise h-full"><ErrorBoundary resetKey={loc.pathname}><Outlet /></ErrorBoundary></div>
        {healthError && <p role="alert" className="mx-4 my-3 rounded-xl border border-bad bg-bad-bg px-4 py-2 text-center text-sm text-bad">{healthError}. Start the backend (<code>./start.sh</code>) and reload.</p>}
      </main>
    </div>
  );
}
