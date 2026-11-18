# Decisions and corrections to the brief

"Verified" means I actually ran it against the real thing. Anything else is marked UNVERIFIED.

## Corrections to the brief

| Brief said | What we do instead | Why / status |
|---|---|---|
| LLM: Anthropic API, `claude-haiku-5-5` / `claude-sonnet-5-5` | **Local Ollama only** (`qwen3:8b` for routing and tool steps, `qwen3:14b` for synthesis) | Owner's constraint: free APIs only, no paid keys. `qwen3:8b` VERIFIED live (2026-11-17): pulled, structured parallel tool calls, token counts returned. `qwen3:14b` not yet pulled or tested. |
| Price table + cost per request in the UI | Cost is $0 (local). Tokens and latency are the cost metrics, recorded on every span. `cost_estimate` stays in the span schema but is `None`. | No paid provider. |
| Model comparison: Haiku vs Sonnet in evals | Compare two local models (e.g. `qwen3:8b` vs `qwen3:14b`) in Phase 7 | Same methodology, different models. |
| Repo layout rooted at `commute-agents/` | The git repo root is the project root | Deliberate. |
| Calendar: Google Calendar API with OAuth, `google.py` | **Not built.** The owner cannot set up a Google Cloud OAuth client, so there is no Google code (I will not ship unverifiable integration code). Replaced by a **local calendar** (JSON file, git-ignored; the thing that is read and written, after approval) plus **read-only `.ics` import** (export from Google/Apple/Outlook). Layout differs from the brief: `store.py`, `ics.py`, `service.py`, `proposals.py`, `executor.py`, `models.py` instead of `google.py`. | Owner's constraint; chosen from options. |
| Transit/weather/calendar data | Unchanged: only public, freely provided sources (TransLink GTFS, Open-Meteo, Google Calendar free API) | Owner's constraint. |
| Alerts at `.../realtime/SEQ/ServiceAlerts` | Real endpoint is `.../realtime/SEQ/alerts`; `/ServiceAlerts` returns HTTP 400. | Verified live (2026-11-17). |
| GTFS static URL, TripUpdates and VehiclePositions URLs | Correct as recalled: `gtfsrt.api.translink.com.au/GTFS/SEQ_GTFS.zip` (42.7 MB, ETag + Last-Modified) and `/api/realtime/SEQ/{TripUpdates,VehiclePositions}`. No API key. | Verified live. |
| Realtime trips are SCHEDULED/SKIPPED/CANCELED/NO_DATA | Live feed also has trip-level `REPLACEMENT` (~30% of trips) and `ADDED` (no static trip). | Verified live; both handled. |
| Realtime `start_date` = GTFS service date | **Not for trips that start after midnight.** Static says `24:35:00` on service date Nov 17; realtime says `start_date=20261118`, `start_time=00:35:00`. We store each trip's first-departure time and map: `rt_start_date = service_date + first_departure // 24h`. Without this every after-midnight trip silently loses its realtime. | Found by comparing delay vs time-minus-schedule on the live feed (66 mismatches -> 0 for scheduled trips). |
| Open-Meteo fields `precipitation`, `precipitation_probability`, `weather_code`, `uv_index`, wind | All exist with units degC, %, mm, km/h, WMO code (live, 2026-11-18). But hourly values are NOT all instants: per the Open-Meteo docs, slot `T` means the **preceding hour** for rain amount, rain probability, wind gusts (max) and UV (mean), and an instant for temperature, wind speed and weather code. Free tier: no key, non-commercial, 600 calls/min (matches the live `X-RateLimit-Limit` header), 5,000/h, 10,000/day (limits per the docs; enforcement unverified). My own check of the 'preceding hour' claim against 15-minute data was inconclusive (outside Europe/N. America that data is interpolated and drizzle rounds to 0.0), so this rests on the docs. Coordinates snap to a model grid (asked -27.23, 153.02, got -27.2408, 153.0100). | Verified live (fields/units/limits header); semantics from docs. |
| `delay` and `time` agree | They agree for SCHEDULED trips (1157/1157) but not for REPLACEMENT trips (delay is relative to the replacement's own timetable; offsets are whole minutes). We trust the absolute `time` (spec: time wins). | Verified live. |

## Design decisions (Phase 1)

- **Own agent loop, no framework.** ~150 lines (`core/agent.py`); every stop condition
  (`completed`, `max_steps`, `budget_exceeded`, `error`) is explicit and unit-tested.
  `Agent.run` never raises for model/tool/budget problems.
- **One internal message format** (content blocks: `text`, `tool_use`, `tool_result`).
  `core/llm.py` translates to/from Ollama's native `/api/chat` format with pure, tested functions,
  so swapping backends later touches one file.
- **Tracing is done by the loop, not the LLM wrapper**, so a scripted fake is traced exactly
  like the real client.
- **`num_ctx` is always set explicitly (default 8192).** Ollama silently truncates prompts past
  its default context (4096), which would corrupt tool-heavy conversations.
- **`think=False` for router/agent/synthesis by default.** Thinking models are much slower with
  thinking on. Revisit per role with the Phase 7 evals.
- **No retry layer.** The server is local; connection errors give a clear "is `ollama serve`
  running?" message and a missing model says which `ollama pull` to run. Timeouts/5xx are flagged
  `retryable` for callers.
- **Permissions in code:** `Access.EXECUTE` tools cannot be granted to any agent
  (`ToolRegistry.grant` and `ToolSet.__init__` both refuse); an agent can only call names in its
  `ToolSet`, so a hallucinated tool name returns `unknown_tool`. Tests cover both.
- **Budgets:** a `RequestBudget` (tokens + wall time) is shared by every agent in a request.
  Time is enforced with `asyncio.timeout` around each LLM call and tool batch. Tokens are checked
  *between* steps, so a single call can overshoot the token budget by up to one response.
- **On the last allowed step, requested tools are not executed.**
- **Tool JSON schemas** come from `model_json_schema()` with titles stripped and
  `additionalProperties: false`. Nested models produce `$defs/$ref`; whether Ollama's tool
  handling copes is UNVERIFIED (current tools have flat inputs).
- **Trace store** is stdlib `sqlite3` behind a lock; writes are synchronous and briefly block the
  event loop, which is fine at single-user volume.

## Verified live (hello-world agent, `qwen3:8b`, Apple M4 24 GB)
- Ollama `/api/chat` tool calling returns structured `tool_calls`; `prompt_eval_count`/`eval_count` are populated.
- Two independent tool calls came back in one turn and ran concurrently; final answer was correct.
- Latency: 6.0 s for the first call (includes model load), 2.6 s for the second; 8.6 s end to end.
- Still unverified: nested tool schemas (`$defs`), long conversations near `num_ctx`, `think=True`.

## Known risks of local models (to measure in Phase 7, not assume away)
- Small models sometimes emit malformed tool arguments or write the call as plain text instead of
  a structured tool call. The loop returns validation errors to the model so it can retry, but
  the rate is unmeasured.
- Tool-calling quality, latency (first call includes model load) and synthesis faithfulness will be
  noticeably below frontier hosted models. The hallucination check and deterministic logic
  (journey planner, `commute_advice`, slot finding) are in tested code for this reason.

## Deployment (GitHub Pages)
- Pages serves static files only: it can host the React frontend, not the FastAPI backend,
  SQLite, or Ollama. Verified: TransLink's realtime endpoint sends no CORS headers, so a
  browser-only app could not read live transit data directly either.
- Plan: Pages hosts the frontend in **demo mode** (frozen fixtures, recorded answers, clearly
  labelled as not live). The real app runs locally. Frontend requirements that follow from this:
  static build, configurable API base URL, demo fixtures bundled with the frontend.
- Optional later (needs owner approval and auth): expose the local backend through a free tunnel.
- Test fixtures must be small plain-text files (`.zip` is git-ignored).

## Phase 2: transit agent

### Data (all from TransLink SEQ open data, no key)
- **Ingest:** 25 s, 240 MB SQLite (git-ignored). Bus (route_type 3) + rail (2) only: 164,704 trips,
  4.58M stop times, 1,779 stop patterns. Ferry/tram dropped (verified: the realtime trips missing from
  the static table are exactly the ferry/tram ones). Written to a temp file and renamed, so a failed
  refresh never clobbers a working DB.
- **Refresh:** conditional GET with ETag/Last-Modified. Verified live: second call returns 304 in 0.2 s.
  A daily scheduler is NOT wired yet (comes with the API in Phase 5); `refresh` is a CLI command.
- **Times:** stored as integer seconds from service-day midnight (42,684 rows are >= 24:00:00).
  Absolute time = noon-minus-12h of the service date + seconds; yesterday's, today's and
  tomorrow's services are all queried so after-midnight trips work.
- **Service days:** calendar weekday flag within [start, end], then calendar_dates add (1) / remove (2).
  Both exception types occur in the real feed.
- `pickup_type=1` is how the feed marks terminal stops (~one per trip), so those are not departures.

### Realtime semantics (see the module docstring in `gtfs_realtime.py`)
- Keyed by `(trip_id, start_date)`; a static trip_id runs on many dates.
- Delay seconds, positive = late. `time` beats `delay`. SKIPPED stops are not offered as departures;
  NO_DATA/absent updates give "scheduled only".
- Updates cover only some stops on most trips (132 of 228 live): a stop with no explicit update inherits
  the delay of the closest preceding update, reported as `estimated_from_earlier_stop`. A stop before any
  update gets no prediction.
- TTL cache 25 s (feed header says `max-age=10`; the feed itself updates every few seconds). On refresh
  failure the last copy is served as `stale` for up to 180 s, then the feed is reported unavailable.
  Every result carries `feed_age_s` / `scheduled_only` so nothing stale is presented as live.
- A trip scheduled before now but running late is still listed (30 min look-back).

### Journey planner (`planner.py`; extra file vs the brief's layout)
- RAPTOR-style rounds over SQLite (indexed `stop_id, dep`) rather than 4.6M rows in RAM. Walking
  footpaths within `transfer_radius_m` (default 250 m) at 80 m/min; 60 s minimum transfer after a
  vehicle leg; up to `max_transfers` (default 2). Returns a Pareto set (fewest transfers vs earliest
  arrival). `arrive_by` = binary search over the departure time (arrival is monotone in it).
- Real-network timings: 0.5 s (North Lakes -> Chermside), 1.6 s (-> Robina, 141 min, 2 transfers).
- Realtime is attached per boarding (delay / cancelled) but **connections are not re-validated** against
  it, and planning is on the schedule. Stated in every `plan_trip` result.
- Limits: no pure-walking journeys, destinations by name/stop only (no free lat/lon egress), no fares.
- Place resolution: stop id / station id / profile alias ('home') / "lat,lon" / fuzzy name. A name
  resolves to the best match plus similarly-named stops within 500 m. Names that are not stop names
  (e.g. "Brisbane Airport") are not found; there is no gazetteer.

### Lessons from running the agent live with `qwen3:8b`
- **The model cannot do date arithmetic.** Asked for "tomorrow 07:30" it passed a bare `"07:30"`
  (= today, already in the past) twice, even with tomorrow's date written in the prompt. It also relayed
  a "time is in the past" warning to the user instead of re-calling the tool. Fix: time parsing is
  deterministic code (`parse_when`: "tomorrow 07:30", "friday 8am", "9pm", ISO); the model only has to
  copy the user's own words. After the fix both live questions resolved to the right day.
- Tool results for the router/agent stay small (3-4k tokens per question), well inside `num_ctx=8192`.
- A tool-result warning is a weak safety net with a small model; deterministic parsing is the real fix.

### Verified vs not
- VERIFIED against real services: all feed URLs and fields above; ingest; conditional refresh; stop
  search; departures with realtime merge; plan_trip; alerts parsing/filtering; the LLM agent end to end
  (departures and trip plan) with `qwen3:8b`.
- VERIFIED by independent check: "no North Lakes departures after 22:08" matches the raw CSV.
- NOT verified: the planner's journeys against an independent journey planner (only plausibility and the
  unit tests on a hand-made feed); `can_i_catch`, `service_alerts` and `find_stops` through the LLM;
  behaviour with `think=True`; daily refresh scheduling.

## Phase 3: weather agent

### Components
- `client.py`: Open-Meteo parsing + 10 min TTL cache (keyed by 0.01-degree cell). On refresh failure the last copy is served as
  `stale` for up to 1 h, then reported unavailable (never silently old, never guessed).
- `advice.py`: `commute_advice`, a pure deterministic function (no LLM). `service.py`, `tools.py`, `models.py`, `prompts.py`, `cli.py` as for transit.
- Tools (all read-only): `get_forecast(place, hours)`, `get_current(place)`, `commute_advice(journey_index)`.

### How `commute_advice` models a trip
Only time outside a vehicle is weather-exposed. Exposure windows: **access** (walk to the first stop + wait, ending at boarding),
**walk legs**, **waits** between legs (assumed outdoors), **egress** (walk from the last stop). A window `[s, e]` uses forecast slots
with `s < T < e + 1 h` (slot T covers `(T-1h, T]`). Access/egress durations are assumptions (the timetable has no door-to-stop
distances) and are returned in `assumptions`. Weather is sampled at the trip's start only.

Decision: if the worst hazard over the windows is *blocking* (level >= 2) the same windows are slid later in 15 min steps (up to 2 h)
to find the first time with only minor hazards -> `leave_now=false` and a suggested time (rounded **up** to 5 min when waiting for
weather, **down** when following the timetable, since leaving early is safe). If nothing better exists within 2 h it says waiting will
not help and recommends going prepared. A later equivalent service is *assumed* (stated in `assumptions`); callers should re-plan from
the suggested time. Cancelled leg -> `leave_now=false`, no suggested time, confidence 0.4, plan invalid. First leg delayed -> the
departure moves, so the exposure windows and the leave-by time move with it.

### Thresholds (all in `Thresholds`, overridable; units in the field names)
| Hazard | Level 1 (minor: bring something) | Level 2 (blocking: consider waiting) | Level 3 |
|---|---|---|---|
| Rain, per forecast hour | chance >= 30 % **or** >= 0.5 mm/h | chance >= 60 % **or** >= 1.5 mm/h | >= 5 mm/h, or thunderstorm (WMO 95, 96, 99) |
| Heat (apparent temp) | >= 32 degC | >= 36 degC | |
| Wind gusts | >= 45 km/h (umbrella swapped for a rain jacket) | >= 65 km/h | |
| UV index | >= 6 (sunscreen + hat), >= 8 very high | info only, never blocks | |
| Cold (apparent temp) | <= 12 degC -> light jacket | | |

Exposure minimums: rain is ignored if total time outside < 3 min (storms still count); heat/UV ignored if < 10 min.
A trace amount (e.g. 5 % chance, 0.1 mm, drizzle code) is deliberately *not* rain: seen on the live feed at 08:00.
Trip model: 5 min access walk + 2 min stop buffer, 5 min egress, `leave_now` if you should step out within 5 min, wait search 15 min steps
up to 120 min, first-leg delay noted at >= 5 min, connection-risk note at >= 10 min.

### Confidence (0-1)
0.90 baseline. Minus 0.15 stale weather; minus 0.10 / 0.20 if the trip is > 12 h / > 24 h away; minus 0.15 if any exposed hour has a
30-70 % rain chance (genuinely uncertain); minus 0.10 if no transit leg has realtime info. Weather unavailable or forecast not covering
the trip: 0.25. Cancelled plan: 0.40. Clamped to [0.10, 0.95]. These are my heuristics, not calibrated probabilities.

### Lessons from running it live with `qwen3:8b`
- **The model invented coordinates.** `commute_advice` originally had optional `latitude`/`longitude` ("omit for home"). The model filled in
  Brisbane CBD coordinates (~30 km away), so the advice was for the wrong place (UV 8 vs 5.3) and still delivered confidently. Fix: **no tool
  accepts coordinates any more.** Advice uses the plan's own start location; `get_forecast`/`get_current` take a `place` name that code resolves
  (`home`, `lat,lon` for developers, or a resolver the orchestrator injects from the transit stop search); unknown places raise an actionable
  error. A regression test forbids coordinate parameters on any weather tool. General rule for this project: **never let the small model
  supply values the code can derive.**
- The LLM relayed `commute_advice` faithfully (decision, reasons, bring list, confidence all matched the tool output).
- Plain-forecast summaries contain small numeric slips (feels-like max 21.0 vs 21.4; a gust figure blended into the wind-speed range). The
  decision logic is in code for this reason; the hallucination check in Phase 7 must measure these.
- Asked about Sydney it refused without trying the tool (acceptable outcome, but the tool's unknown-place path was not exercised live).

### Verified vs not
- VERIFIED live: Open-Meteo fields/units/rate-limit header, parsing of the real payload, current + forecast through the CLI, a real planned trip
  (transit) + real forecast through `commute_advice`, the LLM weather agent on a trip question and a plain question.
- NOT verified: the 'preceding hour' semantics empirically (docs only); behaviour in actual rain (the live forecast was dry, so wet-weather
  scenarios are covered by unit tests with synthetic forecasts, not by a real wet day); wind-speed vs gust UX; `think=True`.
- Limits: weather sampled at the trip start only; waits assumed outdoors (stations may be covered); no radar/nowcasting; probabilities are
  hourly, so a 10-minute walk in a rainy hour is treated as exposed to that hour's chance; the `commute_advice` suggestion to wait does not
  re-plan the transit journey.

## Phase 4: calendar agent and approval gate

### What was built
`agents/calendar/`: `models.py` (event + etag), `slots.py` (pure `find_free_slots`, `conflicts_for`), `ics.py` (import), `store.py` (local
calendar; **separate reader and writer classes**), `service.py` (`CalendarReader`, read-only), `proposals.py` (validation + diffs),
`executor.py` (the only module holding a writer), `tools.py`, `prompts.py`. `core/approvals.py` (the gate), `core/untrusted.py`.
Tools: read `list_events`, `find_free_slots`, `check_conflicts`; propose `propose_create_event`, `propose_update_event`, `propose_delete_event`.
Full design, state machine and threat model: `docs/safety.md`.

### Scheduling rules (documented because they are choices)
- Only busy **timed** events block time. **All-day events do not block** unless `all_day_events_block` is set (Google/Apple default to
  'free' for birthdays, holidays); ICS `TRANSP:TRANSPARENT` events never block.
- Overlapping and back-to-back busy events merge. `buffer_minutes` (profile default 10) pads **timed** busy events, not the edges of the
  working-hours window. Working hours default 09:00-17:00 (profile). Overnight windows unsupported. Zero-length events block nothing.
- Everything is converted to Australia/Brisbane (no DST). A Sydney event during daylight saving lands an hour earlier in Brisbane (tested).
- Imported `.ics` events are read-only (`writable=false`); update/delete proposals for them are refused with a clear message.
- `.ics` parsing uses `icalendar` + `recurring-ical-events` (RRULE, EXDATE, RECURRENCE-ID overrides, TZID, floating time, all-day,
  STATUS:CANCELLED). Checked against crafted files; **not** checked against a real Google/Apple/Outlook export.
- `parse_when` ("tomorrow 2pm", "friday 8am") moved to `core/timeutil.py` and is shared by all agents.

### Approval gate decisions
- 30 min expiry, enforced lazily for pending and approved-but-unexecuted actions; max 10 pending cards (flood control).
- Hash = sha256(type + payload + diff); approve needs the hash the user saw; execute recomputes it (tamper check).
- Updates/deletes carry the event's content hash (`etag`); execution fails if the event changed since review.
- Idempotency: create ids are `evt-<action id>`; execute twice runs once; double-approve is a no-op; failed actions may be retried.
- `edit` keeps the action pending but changes the hash, so an approval of the old card cannot apply.
- Concurrency safety is in-process (RLock). Multi-process is unsupported.

### Bugs found by tests and live runs (all fixed, all have regression tests)
1. **Tag-forgery bypass:** an unterminated `<untrusted_data` glued to letters slipped past my `\b`-based sanitiser. Now any `<...untrusted` token is neutralised.
2. **Whitespace-only title** passed `min_length=1`, then stripped to empty, so a proposal for an untitled event was accepted. Now checked after cleaning.
3. **`CalendarReader.find()` on imported events always raised** (its +-1 year scan tripped my own 400-day range guard).
4. **Recurrence bomb:** `FREQ=SECONDLY` over 7 days took 10.7 s and 1.25 GB, because the library expands every occurrence before any cap can apply. My first draft of `safety.md` claimed this was already mitigated; it was not. Fixed by costing rules before expansion.
5. **Malformed RRULE crashed the whole read:** icalendar returns a 'broken property' that raises on access; the exception escaped `parse_ics`. Now only that event is skipped.
6. Two of my own test expectations were wrong (a 45-minute gap is not a 60-minute slot; 'day=13' counts from Mon 9 Nov). Kept here as a reminder that a passing first run is not evidence.

### Live results with `qwen3:8b` (local; throwaway demo calendar, real model)
- 'Find me a free 1 hour slot tomorrow afternoon' -> called `find_free_slots('tomorrow 12:00'-'tomorrow 17:00', 60)`; answered 'none',
  which is correct (gaps of 10, 40 and 50 minutes once lunch, a review and 10-minute buffers are counted).
- 'Add Dentist on Thursday at 2pm' -> `propose_create_event`, said it was 'waiting for your approval'; calendar unchanged until the script
  (playing the human) approved; then executed once.
- Calendar containing hostile events (2 mild + 3 aggressive: fake SYSTEM OVERRIDE, forged closing tag, polite embedded request):
  **0 of 5 runs produced any proposal**; the model listed them as events and warned the user. Blemishes: one run offered to 'proceed with
  deleting them'; another claimed a deletion 'is waiting for your approval' when no card existed (a false statement about system state).
  5 runs is an anecdote, not a robustness number: the safety suite uses a model that always obeys.
- Implication for Phase 5: statements about pending approvals must come from the real approval queue (the `approval_required` events), not
  from the model's prose; the Phase 7 hallucination check should cover it.

### Verified vs not
- VERIFIED: gate state machine, tamper/expiry/double-approve/idempotency/concurrency (unit tests, plus mutation checks that break each defence
  and watch the suite fail); proposals change nothing until a human approves (including with a model that obeys injections); ICS recurrence/TZ/all-day
  behaviour on crafted files; recurrence-bomb limits measured; live LLM proposal, approval and slot-finding.
- NOT verified: any real Google/Apple/Outlook `.ics` export; the approval UI and REST endpoints (Phases 5-6); write persistence under a crash
  mid-write beyond atomic rename; multi-process use; Google Calendar (not built).

## Phase 5: orchestrator, API and SSE

### What was built
`orchestrator/`: `planner.py` (plan model, validation, repair, dependency graph), `router.py`, `placeholders.py`, `synthesiser.py`, `engine.py`
(the `Orchestrator`), `events.py`, `context.py`, `sessions.py`, `wiring.py`, `prompts.py` + `prompts/*.md` (versioned prompt files: the layout in the
brief had only router/planner/synthesiser; the rest are additions). `main.py`: `POST /chat` (SSE: `plan`, `step_started`, `step_result`,
`approval_required`, `final`, plus `error`), `GET /approvals`, `POST /approvals/{id}/approve|reject|edit`, `GET /traces`, `GET /traces/{id}` (span tree),
`GET /health`, `GET /eval/latest` (honestly "not run yet" until Phase 7). Typed errors `{"error": {"type", "message"}}`; CORS only for the local frontend;
all input validated with pydantic. The daily GTFS refresh deferred from Phase 2 now runs in the app lifespan (verified live: first check returned 304).

### Design decisions
- **The model proposes, code disposes.** The router (qwen3:8b, schema-constrained JSON via Ollama's `format`, verified live) returns a plan; `validate_plan`
  accepts, repairs or rejects it. The **intent is derived from the steps**, ids are renamed to s1..sn, a clarification next to real steps is dropped, cycles,
  unknown agents and >4 steps are errors. One retry with the reasons fed back, then a clean failure (never a guess).
- **Placeholders** (`{s1.start-15m}`, `{s2.arrive+10m}`): the brief's own `{s1.start}` idea, extended with offsets. Time arithmetic on earlier results is done
  in code from the step's *structured tool output*; the later agent only sees a finished ISO time. A missing value (e.g. no free slot) fails that step with a
  clear reason instead of letting the model invent a time. A placeholder also adds the dependency edge the model forgot.
- **Structured hand-off.** `Agent` now logs every tool call + raw output (`tool_log`). Dependents receive the earlier steps' tool outputs as JSON; the
  synthesiser gets tool outputs (truncated to 2,500 chars each), not just the specialists' prose. Weather's `commute_advice` reads the transit step's
  `plan_trip` result from a ContextVar filled by the orchestrator, with the trip's own start coordinates (the model copies nothing).
- **Scheduling:** each step awaits only its own dependencies (not a whole "wave"). `_execute_step` never raises; failed steps skip their dependents; one failure
  never hangs the rest. Proven parallel with a barrier test that would deadlock if sequential.
- **One owning task per request** pushes events to a queue; the SSE generator never holds request context across `yield`, and closing the stream (client
  disconnect) cancels the work (tested: no leaked task).
- **Per-request ContextVars** (`plans_var`, `proposals_var`) are isolated between concurrent requests (tested with two simultaneous calendar proposals).
- **Synthesiser guards:** all-steps-failed -> deterministic reply (no model call); a failure can never be dropped from the answer; the approval footer
  comes from the real queue, and a model claim of "waiting for your approval" when nothing is pending is corrected (the Phase 4 finding); if the model call
  fails the specialists' own text is used.
- **Shared budget** (tokens + wall time) across router, agents and synthesiser; exhaustion degrades gracefully (tested).
- **Sessions:** in-memory, last 6 turns per session, 50 sessions. Nothing is persisted. Rejections (with reasons) are added to the router context.
- **Synthesis model = qwen3:8b**, not the 14B planned: it is not installed (9 GB) and I will not download it unasked. Phase 7's model comparison needs a second
  model, so I will ask then.

### Router prompt history (versioned files; v1 and v2 stay on disk)
| Version | Change | Why |
|---|---|---|
| v1 | first prompt, 8 few-shot examples | baseline |
| v2 | "never assume a destination" + a clarify example for the brief's dentist wording; example location changed to one that is not a saved place | the model planned a trip to **Chermside** for a dentist with no location (copied from my own example) |
| v3 | "a location is NOT needed to add an event" + a plain add-event example | v2 over-corrected: it asked "where is the coffee meeting?" for a simple add |

**Eval contamination warning:** the brief's example questions are now few-shot examples. Phase 7 cases must use *different* phrasings for routing accuracy,
or the number is inflated.

### Findings from running it live (all fixed in code, each with regression tests)
1. **Bare router output was wrong:** two-agent request labelled `calendar`, plus a spurious "share your location" question -> intent derived, clarification dropped.
2. **`route_filter=["bus"]`:** the model had no way to say "buses to Chermside" and the tool silently returned nothing, so the answer said "no buses". Added `towards`
   and `modes`; an unknown `route_filter` is now an error that says what to use. (Also case-insensitive.)
3. **"Chermside" resolved too narrowly:** the 680 *terminates* at "Chermside Shopping Centre", outside the 500 m cluster, and my saved nickname
   `chermside -> "Chermside bus station"` narrowed the query further. `towards` now matches the user's own words over a wide (2.5 km) cluster.
4. **Time arithmetic by the model:** it used a slot's END for "15 minutes before the start" -> placeholders (above).
5. **The model copies display strings** (`Thu 19 Nov 14:50`) into time arguments; `parse_when` now accepts them and `now`.
6. **Weekday + a time a minute in the past jumped a week** ("Wednesday 01:35" parsed 1 minute late = next Wednesday) -> 15 minute grace.
7. **Service gaps:** at 1 am `plan_trip`/`next_departures` said "nothing" with no hint when service resumes -> they now report the next departure.
8. **Assumed destination despite an explicit prompt rule:** enforced in code (`ungrounded_places`): a plan naming a saved place the user never said is retried once,
   then replaced by a clarifying question. Only the user's own words ground a place.
9. **Token double counting in trace summaries** (agent span rollup + LLM spans: 7,730 vs a true 4,804). Summaries now count LLM spans only.
10. A test-harness bug (a prompt that mentions another agent confused role detection) was mine, not production's.

### Latency, honestly
On an M4 (24 GB) with qwen3:8b: a single-agent answer takes ~35-50 s; a two-agent answer 80-115 s; a clarification ~10 s; out-of-scope ~2 s. Ollama serves
model calls essentially one at a time, so steps run **concurrently in code but their LLM calls queue**: parallelism helps tool waits, not model time.
This is the price of free + local. Expect the UI to need good progress display (that is what the SSE events are for).

### Verified vs not
- VERIFIED live (real model, real TransLink and Open-Meteo data, real HTTP server): the brief's five question types end to end (single agent; calendar->transit
  with a resolved placeholder; transit->weather; clarify; out-of-scope; add-event proposal), the hostile calendar, the full approve flow over HTTP (wrong hash 409, right hash
  executes once, double approve idempotent), SSE streaming, trace tree, the startup GTFS refresh (304).
- Mutation checks (sabotage a rule, watch the suite fail): skipping dependency checks, trusting model approval claims, trusting model intent, ignoring dependencies,
  not recording proposals.
- NOT verified: routing accuracy as a *number* (Phase 7); long conversations; two users (unsupported); `think=True`; behaviour with the 14B model; a real browser
  consuming the SSE stream (Phase 6); the router on many phrasings (a handful of runs is anecdote, and v1->v3 shows the prompt is brittle).

## Phase 6: web UI

**Stack.** React 19, TypeScript strict (`noUncheckedIndexedAccess`), Vite, Tailwind v4 with CSS-variable tokens,
`react-router-dom` (HashRouter, so it also works from static hosting). No component library: the app has four screens
and the markup is short. Vitest for pure logic, Playwright for flows. Production build: 97 KB gzipped JS.

**Design principles.**
- *The UI never makes up what the system knows.* Approval status, the leave/wait decision, token counts and
  latency come from the backend; the UI only renders them. Cost is shown as "local model, no API cost".
- *Show the safety model.* The approval card shows the **exact** text the system will act on, a before/after,
  and a version number; editing creates a new version and the old one can no longer be approved.
- *Safe rendering.* Answers go through a small markdown parser that produces an AST rendered as React elements.
  There is no `dangerouslySetInnerHTML` anywhere, so model or calendar text cannot inject HTML.
- *Accessible by construction.* Colours are tokens and a test computes WCAG AA contrast for 35 pairs in both
  themes; focus rings, a skip link, labelled controls, `role=status/alert`, reduced motion, 16 px gutters at 390 px.

**Demo mode** (needed for the GitHub Pages plan and for deterministic browser tests). Recorded transcripts are
replayed with the original event timings (capped at 1.2 s per gap) so the UI behaves as it does live. Approval
events are re-created in the *real* gate, so the approve path is genuinely exercised. Everything it writes goes to a
temp directory (tested: no real profile, calendar or database file is created). The recorded frozen inputs are
disclosed in `fixtures/meta.json`, including the one synthetic weather edit.

**Findings from building it (things that were wrong, and how they were caught).**
1. *The answer contradicted the rules.* Recording the "leave now or wait?" scenario, the final text said
   "Leave now" while `commute_advice` said wait until 09:00; the model had copied a transit agent's improvised rain
   remark. Fix: the decision block is now written by **code** from the structured tool output and shown first; the
   specialists' prose is dropped from the synthesis payload whenever structured data exists.
2. *Then the model repeated the decision.* With the block in place, the 8B model still restated it (it ignored the
   "do not repeat" prompt rule), which the screenshots showed. Fix: `drop_decision_repeats` removes restated lines in
   code, a unit test covers it, and the recordings test fails if any shipped answer repeats a line. Re-recorded.
   Lesson, again: a prompt rule is a request, not a guarantee.
3. *State held in effects.* React's newer lint rules rejected `setState` inside effects, `Date.now()` in render and
   a mutable ref passed through context. These were restructured (derived state keyed by the requested id, a setter
   function in context) rather than silenced; the trace page now ignores a late response for an old id.
4. *Test isolation.* One browser test left a pending approval that made a later "Approve" ambiguous. Shared helper
   rejects leftovers after every test.
5. *Formatting.* `en-AU` dates come out as "Thu, 19 Nov"; the formatter builds the string from `Intl` parts to get
   "Thu 19 Nov, 15:00".

**Not done / honest limits.** The UI streams but does not show partial answer text (the synthesiser returns one
block). The trace view shows previews (2,000 characters), not full prompts. Demo mode only answers its seven
recorded questions. Verified in Chromium only (Playwright); not tried in Safari or Firefox.

