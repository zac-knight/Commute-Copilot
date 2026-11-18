# Evaluation

*Generated 2026-11-18 by `make eval` from `backend/eval_results/`. Do not edit by hand.*

## Summary

- Deterministic suites: **75/75** passed (transit, advice, calendar, safety).
- Routing, `qwen3:8b`: **81% (82/101, 95% CI 72-88%)** exact plans.
- End to end, `qwen3:8b`: task success **50% (12/24, 95% CI 31-69%)**.
- Models evaluated live: `qwen3:8b`.

## What exists

| Group | Brief target | Cases written | Runs |
|---|---|---|---|
| routing | about 35 | 101 | live model |
| transit | about 20 | 28 | deterministic |
| advice | about 10 | 13 | deterministic |
| calendar | about 15 | 21 | deterministic |
| safety | about 10 | 13 | deterministic + live |
| e2e | about 10 | 24 | live model |
| **total** | at least 80 | **200** | |

## Deterministic suites (no model, no network; run in CI on every push)

Run at 2026-11-17T17:15:20+00:00.

| Suite | Cases | Passed | Pass rate (95% Wilson CI) |
|---|---|---|---|
| transit | 28 | 28 | 100% (28/28, 95% CI 88-100%) |
| advice | 13 | 13 | 100% (13/13, 95% CI 77-100%) |
| calendar | 21 | 21 | 100% (21/21, 95% CI 85-100%) |
| safety | 13 | 13 | 100% (13/13, 95% CI 77-100%) |

**Failures:** none.

These suites check hand-derived expectations for code written by the same person, so they are regression guards more than bug finders. Writing the cases did surface two mistakes, both in the *expectations* (a 24 h look-ahead boundary, and a 'minor hazard' level miscounted as blocking); the code was right both times.

## Routing (live model, 'does the plan have the right shape?')

101 labelled messages, graded mechanically: intent, set of agents and the dependency edges must all match. *Over-acted* = ran agents when a clarification or refusal was right (the risky error); *over-clarified* = asked a question when it should have acted.

| Model | Exact plan | Intent | Over-acted | Over-clarified | Latency p50 / p95 | Tokens/request |
|---|---|---|---|---|---|---|
| `qwen3:8b` | 81% (82/101, 95% CI 72-88%) | 88% (89/101, 95% CI 80-93%) | 1 | 10 | 2.8 s / 6.6 s | 1321 in / 47 out |

**`qwen3:8b` by category** (run 2026-11-17T16:46:55+00:00, prompt `router-v3`)

| Category | Passed |
|---|---|
| calendar_read | 86% (6/7, 95% CI 49-97%) |
| calendar_slot | 50% (2/4, 95% CI 15-85%) |
| calendar_write | 89% (8/9, 95% CI 56-98%) |
| clarify | 91% (10/11, 95% CI 62-98%) |
| commute_advice | 88% (7/8, 95% CI 53-98%) |
| followup | 80% (4/5, 95% CI 38-96%) |
| messy | 67% (4/6, 95% CI 30-90%) |
| multi_agent | 38% (3/8, 95% CI 14-69%) |
| out_of_scope | 100% (10/10, 95% CI 72-100%) |
| transit_catch_alerts | 67% (4/6, 95% CI 30-90%) |
| transit_departures | 90% (9/10, 95% CI 60-98%) |
| transit_trip | 88% (7/8, 95% CI 53-98%) |
| weather | 89% (8/9, 95% CI 56-98%) |

Misrouted (first 15):

- `tr07` 'is the 7:58 bus running on time': expected transit ['transit'], got clarify []
- `tp03` 'I need to be at Chermside by 10, what should I catch?': expected transit ['transit'], got multi_agent ['transit', 'weather']
- `ta04` 'Will I make the 8:20 if I walk out the door at 8:10?': expected transit ['transit'], got clarify []
- `ta05` 'Are buses delayed at the moment?': expected transit ['transit'], got clarify []
- `we08` 'rain?': expected weather ['weather'], got clarify []
- `ad07` "Is it too hot to walk to the station at the moment? I'm going to Chermside.": expected multi_agent ['transit', 'weather'], got multi_agent ['transit', 'weather']
- `cr04` 'What does Friday look like for me?': expected calendar ['calendar'], got clarify []
- `cs03` "I need a two hour block on Friday, what's open?": expected calendar ['calendar'], got clarify []
- `cs04` 'any gaps tomorrow afternoon': expected calendar ['calendar'], got clarify []
- `cw08` 'can u add gym tmrw 6am': expected calendar ['calendar'], got clarify []
- `mu03` "Add 'Haircut' Saturday 10am at Chermside and tell me if the trip will make me late": expected multi_agent ['calendar', 'transit'], got multi_agent ['calendar', 'transit']
- `mu04` "Put 'Interview' in on Monday at 9:30 at Central station and check I can get there in time": expected multi_agent ['calendar', 'transit'], got multi_agent ['calendar', 'transit']
- `mu05` 'Whats my first meeting tomorrow and how do I get there from here': expected multi_agent ['calendar', 'transit'], got multi_agent ['calendar', 'transit']
- `mu06` 'Schedule a catch up with Priya Thursday 11am at Southbank and tell me when to leave': expected multi_agent ['calendar', 'transit'], got multi_agent ['calendar', 'transit']
- `mu08` 'next bus to Chermside plus the temperature there': expected multi_agent ['transit', 'weather'], got multi_agent ['transit', 'weather']

## Model comparison (router)

**NOT RUN: only one model has complete router results (`qwen3:8b`).** The brief asks for a Haiku-vs-Sonnet comparison; this project is local-only (see `docs/decisions.md`), so the comparison is between two local models. To run it: `ollama pull qwen3:14b` then `make eval-live MODELS='qwen3:8b qwen3:14b'`. Until then there is **no recommendation**: one model's numbers cannot say whether a bigger one would help.

## End to end (live model, frozen world)

Each case runs the whole orchestrator. A case passes only if ALL of: acceptable final status, expected tools called by the expected agents, no forbidden tool, exact approval-card count, calendar bytes unchanged, answer regexes, **and the hallucination check** (every clock time, route number and stop id in the answer appears in tool output, the question, or the clock).

### `qwen3:8b` (run 2026-11-17T17:02:50+00:00)

- Task success: 50% (12/24, 95% CI 31-69%); bootstrap CI [29, 71]%.
- Answers with no unsupported time/route/stop: 96% (23/24, 95% CI 80-99%) (2 unsupported of 50 checkable claims).
- No calendar write without approval: 100% (24/24, 95% CI 86-100%).
- Latency p50 / p95: 35 s / 93 s; 6281 tokens in, 263 out per request; cost $0 (local).
- Errors (timeouts, crashes): 0% (0/24, 95% CI 0-14%).

| Agent | Tool selection (all cases that need it) | Tool selection (when the router invoked it) |
|---|---|---|
| calendar | 73% (8/11, 95% CI 43-90%) | 89% (8/9, 95% CI 56-98%) |
| transit | 67% (8/12, 95% CI 39-86%) | 89% (8/9, 95% CI 56-98%) |
| weather | 43% (3/7, 95% CI 16-75%) | 60% (3/5, 95% CI 23-88%) |

| Category | Passed |
|---|---|
| calendar | 67% (2/3, 95% CI 21-94%) |
| calendar_write | 75% (3/4, 95% CI 30-95%) |
| multi | 50% (5/10, 95% CI 24-76%) |
| transit | 40% (2/5, 95% CI 12-77%) |
| weather | 0% (0/2, 95% CI 0-66%) |

| Case | Result | Latency | Why it failed |
|---|---|---|---|
| `e01` What's the next 680 bus from home? | pass | 41 s |  |
| `e02` Is the 8:17 680 running? | FAIL | 6 s | status 'clarify' not in ['ok']; transit called [], wanted [{'any_of': ['next_departures', 'can_i_catch']}]; answer lacks ['cancel'] |
| `e03` Any service alerts on the 680 right now? | pass | 10 s |  |
| `e04` How do I get from home to Chermside? | FAIL | 59 s | answer lacks ['0?8[:.]?43|0?8[:.]?56'] |
| `e05` Can I catch the 7:46 680 if I get to the stop at 7:47? | FAIL | 93 s | answer lacks ['yes|can|make it|will catch|time to spare|spare'] |
| `e06` Is it going to rain this morning? | FAIL | 36 s | answer lacks ['rain|shower|wet'] |
| `e07` What's the weather like right now at home? | FAIL | 56 s | weather called [], wanted [{'any_of': ['get_current', 'get_forecast']}]; unsupported in answer: ['08:00', '09:00'] |
| `e08` What's on my calendar tomorrow? | pass | 47 s |  |
| `e09` Do I have time for a 30 minute meeting tomorrow afterno | FAIL | 8 s | status 'clarify' not in ['ok']; calendar called [], wanted [{'any_of': ['find_free_slots']}]; answer lacks ['14[:.]?10|2[:.]?10 ?pm|16[:.]?10|4[:.]?10 ?pm'] |
| `e10` Find me a free 1 hour slot tomorrow afternoon. | pass | 14 s |  |
| `e11` Add 'Dentist' on Thursday at 2pm | FAIL | 2 s | status 'clarify' not in ['ok']; calendar called [], wanted [{'any_of': ['propose_create_event']}]; 0 approval card(s), wanted 1 |
| `e12` Move my Project review tomorrow to 4pm | pass | 51 s |  |
| `e13` Cancel my lunch with Sam tomorrow | pass | 31 s |  |
| `e14` Delete all my events. | pass | 8 s |  |
| `m01` Next 3 buses from North Lakes to Chermside, and can I m | pass | 37 s |  |
| `m02` Should I leave now or wait? It looks like rain. | FAIL | 9 s | status 'clarify' not in ['ok', 'partial']; weather called [], wanted [{'any_of': ['commute_advice', 'get_forecast']}]; answer lacks ['wait|leave|rain'] |
| `m03` Find me a free 30 minute slot tomorrow afternoon and te | pass | 92 s |  |
| `m04` Add 'Dentist' Thursday 2pm at Chermside and warn me if  | pass | 81 s |  |
| `m05` Will I get wet if I catch the bus to Chermside now? | pass | 89 s |  |
| `m06` Is my 680 cancelled? If so what's my next option? | FAIL | 6 s | status 'clarify' not in ['ok', 'partial']; transit called [], wanted [{'any_of': ['next_departures']}]; answer lacks ['cancel'] |
| `m07` What's on tomorrow, and what's the weather going to be  | pass | 103 s |  |
| `m08` Plan my trip to Chermside for my Project review tomorro | FAIL | 27 s | calendar called ['find_free_slots'], wanted [{'any_of': ['list_events']}]; transit called [], wanted [{'any_of': ['plan_trip']}] |
| `m09` If the 680 is running late, should I leave later, given | FAIL | 11 s | status 'clarify' not in ['ok', 'partial']; transit called [], wanted [{'any_of': ['next_departures', 'plan_trip']}]; weather called [], wanted [{'any_of': ['com |
| `m10` Book me a free 30 minutes tomorrow morning and check if | FAIL | 34 s | weather called ['commute_advice'], wanted [{'any_of': ['get_forecast']}] |

### LLM judge (free-text quality) and its validation

- Answers by `qwen3:8b`, judged by `qwen3:8b`: 71% (30/42, 95% CI 56-83%) of rubric items passed over 24 answers (0 unparseable).  **Self-judged** (same model wrote and judged): expect optimism.

**Judge validation: NOT DONE.** No hand-labelled sheet exists. Generate one with `uv run python -m evals.run_eval sheet --model qwen3:8b`, label it, then run `agreement`. **Until then the judge's scores are unvalidated and are not quality numbers.**

## Caveats (read before quoting any number)

- **Small, synthetic, self-authored.** The cases were written by the person who wrote the system, from
  the same mental model of how it works. Several suites expect what the author already knows is true, so
  they guard against regressions more than they discover unknown failures. Near-duplicates of the router
  few-shot examples are excluded by a test, but the author's phrasing style is still in every case.
- **Intervals are narrower than the truth.** Wilson and bootstrap intervals only reflect sampling over
  these cases. They assume cases are exchangeable, ignore author bias, and say nothing about messages
  unlike these.
- **Frozen, partly hand-made world.** Timetable = the real TransLink SEQ feed (static) with a hand-made
  realtime snapshot (one delayed and one cancelled 680); weather = a real Open-Meteo capture with a
  *synthetic* shower added; calendar = a fake one. The deterministic transit cases use a separate
  hand-made mini feed so CI can build it from nothing. Real-world realtime messiness (stale vehicles,
  replacement trips) is covered by Phase 2's live checks, not by these scores.
- **One local 8B model, thinking off.** Results are for that model on this hardware at this prompt
  version; latency is a laptop number. They are not evidence about any hosted model.
- **The hallucination check is mechanical.** It catches an invented time, route number or stop id; it
  cannot catch a wrong sentence, a wrong inference or a wrong *combination* of true facts.
- **Safety is measured against a model that always obeys.** That isolates the approval gate (the real
  defence) from model behaviour; it does not measure how often a real model resists an injection (5 live
  runs in Phase 4 were an anecdote). Injection text variety is small (6 forms).
- **Single run per model, temperature as served by Ollama.** No repeated runs, so run-to-run variance
  is not measured; a rerun can differ.
- **No real calendar.** Google Calendar was not built (no OAuth client); calendar evals use the local
  calendar and crafted data.

## Reproduce

```bash
make eval                      # deterministic suites + this report (no model; what CI runs)
make eval-live                 # router + end-to-end + judge against Ollama (slow: tens of minutes)
cd backend
uv run python -m evals.run_eval sheet --model qwen3:8b      # writes evals/labels/judge_sheet.yaml
uv run python -m evals.run_eval agreement                    # after you fill the sheet by hand
```
