// Types mirroring the backend (backend/app/orchestrator/events.py, core/approvals.py, main.py).

export type AgentName = "transit" | "weather" | "calendar";
export type StepStatus = "pending" | "running" | "ok" | "failed" | "skipped";
export type ActionStatus = "pending" | "approved" | "rejected" | "expired" | "executed" | "failed";

export interface PlanStep { id: string; agent: AgentName; task: string; depends_on: string[] }
export interface Plan {
  intent: "multi_agent" | "transit" | "weather" | "calendar" | "clarify" | "out_of_scope";
  steps: PlanStep[];
  clarification_question: string | null;
  repairs: string[];
}

export interface PendingAction {
  id: string;
  type: string;
  payload: Record<string, unknown>;
  diff: string;
  summary: string;
  payload_hash: string;
  status: ActionStatus;
  version: number;
  created_at: string;
  expires_at: string;
  decided_at: string | null;
  executed_at: string | null;
  reject_reason: string | null;
  result: Record<string, unknown> | null;
  error: string | null;
}

export type FinalStatus = "ok" | "clarify" | "out_of_scope" | "partial" | "failed";

export type ChatEvent =
  | { type: "plan"; request_id: string; plan: Plan }
  | { type: "step_started"; id: string; agent: AgentName; task: string }
  | { type: "step_result"; id: string; agent: AgentName; status: "ok" | "failed" | "skipped"; text: string; error: string | null; tool_calls: string[]; duration_ms: number }
  | { type: "approval_required"; action: PendingAction }
  | { type: "final"; request_id: string; status: FinalStatus; text: string; pending_approvals: string[]; tokens_in: number; tokens_out: number; duration_ms: number }
  | { type: "error"; request_id: string | null; message: string }
  | LiveEvent;

export interface SpanNode {
  span_id: string;
  request_id: string;
  parent_id: string | null;
  kind: "llm" | "tool" | "agent" | "orchestrator";
  name: string;
  start: number;
  end: number | null;
  status: "ok" | "error";
  input_preview: string;
  output_preview: string;
  tokens_in: number;
  tokens_out: number;
  model: string | null;
  cost_estimate: number | null;
  error: string | null;
  attributes: Record<string, unknown>;
  children: SpanNode[];
}

export interface TraceSummary {
  request_id: string;
  start: number;
  end: number | null;
  span_count: number;
  error_count: number;
  tokens_in: number;
  tokens_out: number;
  cost_estimate: number;
}
export interface TraceDetail { request_id: string; summary: TraceSummary; tree: SpanNode[] }

export interface Settings {
  home: string;
  destinations: Record<string, string>;
  working_hours_start: string;
  working_hours_end: string;
  buffer_minutes: number;
  router_model: string;
  available_models: string[];
  home_resolves_to: string | null;
}
export type SettingsInput = Omit<Settings, "available_models" | "home_resolves_to" | "router_model"> & { router_model?: string | null };

export interface Health {
  status: string;
  demo: boolean;
  demo_examples?: string[];
  demo_examples_by_agent?: Partial<Record<AgentName, string[]>>;
  llm: { backend: string; reachable: boolean | null; models: Record<string, { model: string; think: boolean }> };
  agents_available: string[];
  transit: { loaded: boolean; feed_start?: string; feed_end?: string; last_refresh_check?: { status: string; at: string } | null };
  approvals_pending: number;
}

export class ApiError extends Error {
  constructor(public status: number, public kind: string, message: string, public details: { field: string; message: string }[] = []) {
    super(message);
    this.name = "ApiError";
  }
}

/* ---- agents, analytics, evaluation (backend: /agents, /analytics/summary, /eval/latest) ---- */

export type Mode = "auto" | AgentName;

export interface AgentTool { name: string; access: "read" | "propose" | "execute"; description: string; calls_24h: number; errors_24h: number }
export interface AgentCard {
  name: AgentName; title: string; summary: string; source: string; prompt_version: string; available: boolean;
  tools: AgentTool[]; calls_24h: number; errors_24h: number; mean_s_24h: number | null; tokens_in_24h: number; tokens_out_24h: number;
}

export interface Rate { k: number; n: number; rate: number; lo: number; hi: number }
export interface DelayRow { key: string | number; n: number; median_s: number; q1_s: number; q3_s: number; p90_s: number; median_lo_s: number; median_hi_s: number; cancelled: Rate }
export interface Analytics {
  coverage: { polls: number; ok_polls: number; first_ts: number | null; last_ts: number | null; rows: number; hours_collected: number; days_collected: number; final_rows: number; rows_with_delay: number; collector_running: boolean; success_rate: Rate };
  distributions: null | { overall: { n: number; median_s: number; q1_s: number; q3_s: number; p90_s: number }; route: DelayRow[]; hour: DelayRow[]; weekday: DelayRow[] };
  distributions_reason: string | null;
  cancellations: Rate | null;
  cancellations_reason: string | null;
  rain: null | { wet_n: number; dry_n: number; wet_median_s: number; dry_median_s: number; diff_s: number; ci_lo_s: number; ci_hi_s: number; note: string };
  rain_reason: string | null;
  model: { available: false; reason: string; min_days: number } | {
    available: true; train_rows: number; test_rows: number; train_until: string; test_days: number;
    mae_s: Record<"model" | "always_on_time" | "last_observed_delay", number>; best_baseline: string;
    gain_over_best_baseline_s: number; gain_ci_s: [number, number]; beats_baselines: boolean; coefficients: Record<string, number>;
  };
}

export interface EvalSuite { passed: number; n: number; rate: Rate | null; cases: { id: string; desc: string; passed: boolean }[] }
export interface EvalSummary {
  available: boolean;
  deterministic: null | { run_at: string; suites: Record<string, EvalSuite> };
  router: Record<string, { run_at: string; n: number; exact: Rate; intent: Rate; over_acted: number; over_clarified: number; latency_ms: { p50: number; p95: number; mean: number }; by_category: Record<string, Rate>; tokens_in_mean: number; tokens_out_mean: number }>;
  e2e: Record<string, { run_at: string; n: number; task_success: Rate & { boot_lo: number; boot_hi: number }; grounded: Rate; latency_ms: { p50: number; p95: number; mean: number }; unsupported_claims: { checked: number; unsupported: number }; no_unapproved_write: Rate; tool_selection: Record<string, { all: Rate; when_invoked: Rate }>; by_category: Record<string, Rate> }>;
}

/* ---- trip artifact (backend: app/agents/transit/geo.py) and live-processing events ---- */

export type TransitMode = "bus" | "train" | "tram" | "ferry";
export interface TripStop { name: string; lat: number; lon: number }
export interface TripLeg {
  kind: "walk" | "transit"; from: string; to: string; depart: string; arrive: string; mode: TransitMode | null;
  route: string | null; route_name?: string | null; headsign: string | null; realtime: { status: string; delay_min: number | null } | null;
  path: [number, number][]; stops?: TripStop[]; distance_m: number; num_stops: number;
  stage?: number; wait_before_min?: number; trip_id?: string | null; stopover?: string | null; stay_min?: number | null; stopover_index?: number | null; depart_iso?: string | null; arrive_iso?: string | null;
}
export interface TripSummary {
  stages: number; wait_min: number; all_bus: boolean;
  total_m: number; walk_m: number; transit_m: number; by_mode_m: Record<string, number>; straight_line_m: number;
  circuity: number | null; stops_passed: number; avg_speed_kmh: number | null;
}
export interface TripJourney {
  depart: string; arrive: string; duration_min: number; transfers: number; legs: TripLeg[]; summary: TripSummary;
  origin: { name: string; lat: number; lon: number }; destination: { name: string; lat: number; lon: number };
}
export interface TripArtifact {
  origin_label: string; destination_label: string; journeys: TripJourney[]; bbox: [number, number, number, number];
  context_stops: { lat: number; lon: number; train: boolean }[]; realtime: { source: string; feed_age_s: number | null; stale: boolean; note: string | null } | null;
  note: string | null; measurement_notes: string;
}

export type LiveEvent =
  | { type: "llm_started"; step: string; agent: string; n: number; model: string; think: boolean | null; prompt_version: string; messages: number; tools: string[] }
  | { type: "thought"; step: string; agent: string; delta: string }
  | { type: "token"; step: string; agent: string; phase: string; delta: string }
  | { type: "tool_call"; step: string; agent: string; call_id: string; name: string; input: Record<string, unknown> }
  | { type: "tool_result"; step: string; agent: string; call_id: string; name: string; output: string; is_error: boolean; duration_ms: number }
  | { type: "llm_usage"; step: string; agent: string; n: number; model: string; tokens_in: number; tokens_out: number; duration_ms: number; thinking_chars: number }
  | { type: "trip"; step: string; agent: string; artifact: TripArtifact };

/* ---- journey planner (backend: app/planner_api.py) ---- */

export type PlaceKind = "saved" | "station" | "stop" | "address" | "gps";
export interface PlaceHit { id: string; name: string; kind: PlaceKind; modes: string[]; lat: number; lon: number; alias?: string }
export interface PlanResponse {
  now: string; artifact: TripArtifact | null; note: string | null; basis: string | null;
  realtime: { source: string; feed_age_s: number | null; stale: boolean; note: string | null } | null;
}
export interface DepartureRow {
  route: string; route_name: string; mode: TransitMode; headsign: string; stop_name: string; platform: string | null;
  scheduled: string; scheduled_iso: string; predicted: string | null; predicted_iso: string | null;
  delay_min: number | null; status: string; trip_id: string;
}
export interface DeparturesResponse { departures: DepartureRow[]; stops: { stop_id: string; name: string }[]; generated_at: string; note: string | null; realtime: { source: string; feed_age_s: number | null } }
export interface AlertRow { alert_id: string; effect: string; cause: string; header: string; description: string; url: string; route_ids: string[] }
export interface AlertsResponse { alerts: AlertRow[]; total_matching: number }
export interface PlanQuery { origin: string; destination: string; when?: string; by: "leave" | "arrive"; maxTransfers: number; via?: string[]; stay?: number }

export interface VehicleRow {
  id: string; label: string; route: string; route_name: string; headsign?: string; mode: TransitMode; lat: number; lon: number;
  bearing: number | null; speed_kmh: number | null; status: string; timestamp: number; trip_id: string; on_trip: boolean;
}
export interface VehiclesResponse { vehicles: VehicleRow[]; total?: number; age_s: number | null; stale?: boolean; available: boolean; error: string | null }
export interface TripPath { trip_id: string; route: string; route_name: string; mode: "bus" | "train" | "ferry" | "tram"; headsign: string; stops: TripStop[]; path: [number, number][] }
export interface TrainLineRoute { key: string; route: string; route_name: string; path: [number, number][]; road: boolean }
