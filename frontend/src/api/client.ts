import { readSse } from "../lib/sse";
import { ApiError, type ActionStatus, type AlertsResponse, type DeparturesResponse, type PlaceHit, type PlanQuery, type PlanResponse, type VehiclesResponse, type TripPath, type TrainLineRoute, type AgentCard, type AgentName, type Analytics, type ChatEvent, type EvalSummary, type Health, type PendingAction, type Settings, type SettingsInput, type TraceDetail, type TraceSummary } from "./types";

/** Everything the UI needs from the backend. Implemented over HTTP below; tests/a static demo can supply another. */
export interface Api {
  health(): Promise<Health>;
  chat(sessionId: string, message: string, signal?: AbortSignal, agent?: AgentName | null, reasoning?: boolean): AsyncGenerator<ChatEvent>;
  agents(): Promise<AgentCard[]>;
  analytics(): Promise<Analytics>;
  evalLatest(): Promise<EvalSummary>;
  runDeterministicEval(): Promise<EvalSummary>;
  approvals(status?: ActionStatus): Promise<PendingAction[]>;
  approve(id: string, payloadHash: string): Promise<PendingAction>;
  reject(id: string, reason?: string): Promise<PendingAction>;
  edit(id: string, payload: Record<string, unknown>): Promise<PendingAction>;
  traces(limit?: number): Promise<TraceSummary[]>;
  trace(requestId: string): Promise<TraceDetail>;
  settings(): Promise<Settings>;
  saveSettings(s: SettingsInput): Promise<Settings>;
  places(q: string, geocode?: boolean, signal?: AbortSignal): Promise<PlaceHit[]>;
  plan(q: PlanQuery, signal?: AbortSignal): Promise<PlanResponse>;
  departures(stop: string, limit?: number): Promise<DeparturesResponse>;
  alerts(routes: string[]): Promise<AlertsResponse>;
  vehicles(bbox: [number, number, number, number], tripIds: string[], signal?: AbortSignal, modes?: string[]): Promise<VehiclesResponse>;
  trainLines(): Promise<TrainLineRoute[]>;
  tripPath(tripId: string): Promise<TripPath>;
}

async function toApiError(r: Response): Promise<ApiError> {
  try {
    const body = (await r.json()) as { error?: { type?: string; message?: string; details?: { field: string; message: string }[] } };
    return new ApiError(r.status, body.error?.type ?? "http_error", body.error?.message ?? r.statusText, body.error?.details ?? []);
  } catch {
    return new ApiError(r.status, "http_error", r.statusText || `HTTP ${r.status}`);
  }
}

export class HttpApi implements Api {
  constructor(private base: string = (import.meta.env.VITE_API_BASE as string | undefined) ?? "/api") {}

  private async json<T>(path: string, init?: RequestInit): Promise<T> {
    let r: Response;
    try {
      r = await fetch(this.base + path, { ...init, headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) } });
    } catch (e) {
      throw new ApiError(0, "network", e instanceof Error ? `Cannot reach the server (${e.message})` : "Cannot reach the server");
    }
    if (!r.ok) throw await toApiError(r);
    return (await r.json()) as T;
  }
  private post<T>(path: string, body: unknown): Promise<T> {
    return this.json<T>(path, { method: "POST", body: JSON.stringify(body) });
  }

  agents = () => this.json<AgentCard[]>("/agents");
  analytics = () => this.json<Analytics>("/analytics/summary");
  evalLatest = () => this.json<EvalSummary>("/eval/latest");
  runDeterministicEval = () => this.json<EvalSummary>("/eval/run/deterministic", { method: "POST" });
  health = () => this.json<Health>("/health");
  approvals = async (status?: ActionStatus) => this.json<PendingAction[]>(`/approvals${status ? `?status=${status}` : ""}`);
  approve = async (id: string, payloadHash: string) => (await this.post<{ action: PendingAction }>(`/approvals/${id}/approve`, { payload_hash: payloadHash })).action;
  reject = async (id: string, reason?: string) => (await this.post<{ action: PendingAction }>(`/approvals/${id}/reject`, { reason: reason || null })).action;
  edit = async (id: string, payload: Record<string, unknown>) => (await this.post<{ action: PendingAction }>(`/approvals/${id}/edit`, { payload })).action;
  traces = (limit = 50) => this.json<TraceSummary[]>(`/traces?limit=${limit}`);
  trace = (requestId: string) => this.json<TraceDetail>(`/traces/${encodeURIComponent(requestId)}`);
  settings = () => this.json<Settings>("/settings");
  saveSettings = (s: SettingsInput) => this.json<Settings>("/settings", { method: "PUT", body: JSON.stringify(s) });

  places = async (q: string, geocode = false, signal?: AbortSignal) =>
    (await this.json<{ places: PlaceHit[] }>(`/places?q=${encodeURIComponent(q)}${geocode ? "&geocode=1" : ""}`, { signal })).places;
  plan = (q: PlanQuery, signal?: AbortSignal) =>
    this.json<PlanResponse>(`/plan?origin=${encodeURIComponent(q.origin)}&destination=${encodeURIComponent(q.destination)}&by=${q.by}&max_transfers=${q.maxTransfers}${q.when ? `&when=${q.when}` : ""}${(q.via ?? []).map((v) => `&via=${encodeURIComponent(v)}`).join("")}${q.via?.length ? `&stay=${q.stay ?? 5}` : ""}`, { signal });
  departures = (stop: string, limit = 8) => this.json<DeparturesResponse>(`/departures?stop=${encodeURIComponent(stop)}&limit=${limit}`);
  alerts = (routes: string[]) => this.json<AlertsResponse>(`/alerts?routes=${encodeURIComponent(routes.join(","))}`);

  vehicles = (bbox: [number, number, number, number], tripIds: string[], signal?: AbortSignal, modes: string[] = []) =>
    this.json<VehiclesResponse>(`/vehicles?bbox=${bbox.map((n) => n.toFixed(5)).join(",")}${tripIds.length ? `&trips=${encodeURIComponent(tripIds.join(","))}` : ""}${modes.length ? `&modes=${modes.join(",")}` : ""}`, { signal });
  trainLines = async () => (await this.json<{ lines: TrainLineRoute[] }>("/train-lines")).lines;

  tripPath = (tripId: string) => this.json<TripPath>(`/trip-path?trip_id=${encodeURIComponent(tripId)}`);

  async *chat(sessionId: string, message: string, signal?: AbortSignal, agent?: AgentName | null, reasoning = false): AsyncGenerator<ChatEvent> {
    let r: Response;
    try {
      r = await fetch(`${this.base}/chat`, {
        method: "POST", signal, headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ session_id: sessionId, message, detail: true, ...(reasoning ? { reasoning: true } : {}), ...(agent ? { agent } : {}) }),
      });
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") throw e;
      throw new ApiError(0, "network", "Cannot reach the server. Is the backend running?");
    }
    if (!r.ok) throw await toApiError(r);
    if (!r.body) throw new ApiError(0, "network", "The server sent no stream");
    for await (const frame of readSse(r.body)) {
      try {
        yield JSON.parse(frame.data) as ChatEvent;
      } catch {
        // a malformed frame is skipped rather than ending the whole answer
      }
    }
  }
}
