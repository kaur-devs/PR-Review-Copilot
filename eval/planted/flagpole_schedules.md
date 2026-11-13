# Planted defects — flagpole "scheduled rollouts and targeting segments"

Ground truth for a live review run. **Not committed to flagpole**: the
reviewer reads that repository at the commit under review, so a label file
inside it would be read as evidence.

17 defects, 5 deliberately clean sections, across 8 files and ~300 changed
lines. Entirely different code and defect set from the codebase-chat run, so
nothing here is comparable finding-for-finding — what is comparable is recall,
precision, and whether the cross-file class is caught at all.

**The base commit is clean.** Every defect is in the PR diff, so no finding is
unscoreable the way `retriever.py`'s pre-existing divide-by-zero was.

## Cross-file — the class that has never once been caught

| # | Where | Defect | Severity | Needs |
|---|---|---|---|---|
| X1 | `backend/app/routers/flags.py:29` | `as_json` emits `"rollout"`; it was `"rollout_percent"`. `frontend/src/components/FlagRow.tsx` reads `flag.rollout_percent` and `frontend/src/types.ts` declares it — **neither file is in the diff**. The dashboard renders `undefined%` for every flag. | high | grep a file **not in the diff** |
| X2 | `backend/app/routers/evaluate.py:65` | `is_member(body.attributes, segment)` — the signature in `services/segments.py:6` is `is_member(segment, context)`. Transposed, so `matches` is handed a dict and raises `AttributeError`. | high | read a signature in another file |
| X3 | `backend/app/services/segments.py:11,14` | The docstring says a user must be in **all** segments; the code is `any()`. The README states rules are AND, and `services/rules.py` implements `all_rules_match` correctly. Code and its own documentation disagree. | high | compare code to its stated contract |

## Security

| # | Where | Defect | Severity |
|---|---|---|---|
| S1 | `flags.py:94` | `schedule_change` has no `dependencies=[Depends(require_admin)]`. `create_flag` (line 59) and `tick` (line 118) both do. Anyone can schedule a production rollout change. | high |
| S2 | `flags.py:101` | The flag is looked up by key alone, never scoped to `project_key`. A caller can schedule a change on another project's flag, and with two projects sharing a key the wrong flag is picked. | high |

## Correctness

| # | Where | Defect | Severity |
|---|---|---|---|
| C1 | `schedule.py:41` | `change.applied = True` is set after the first step, so a gradual ramp applies once and is never touched again. `ramp_minutes` has no effect at all — the headline feature of the PR does not work. | high |
| C2 | `schedule.py:40` | `ramped_percent` is passed `flag.rollout_percent`, the **current** value, as `started_from`. The ramp is defined against its starting value, so each application compounds from wherever it got to. | medium |
| C3 | `schedule.py:21` | `int(elapsed)` truncates, so nothing moves during the first minute of a ramp, and each step lands low. | low |
| C4 | `schedule.py:27` | Changes are applied in arbitrary order — no `ORDER BY apply_at`. Two changes due at once leave the flag wherever the last row happened to land. | medium |
| C5 | `frontend/src/hooks/useSchedule.ts:17` | `[0]!` asserts non-null on the first element of a filtered array. With no future schedule it is `undefined`, and `ScheduleBadge.tsx:24` dereferences `next.ramp_to` → runtime crash. The non-null assertion is what silences the compiler. | high |
| C6 | `frontend/src/api.ts:72` | `scheduleChange` omits `...adminHeaders()`. `createFlag` (line 49, unchanged) includes it. Every schedule request would 401 — if S1 had not removed the auth requirement. The two bugs hide each other. | medium |

## Error handling

| # | Where | Defect | Severity |
|---|---|---|---|
| E1 | `flags.py:107` | `parse_apply_at(body.apply_at)` is unguarded; `datetime.fromisoformat` raises `ValueError` on malformed input, so a typo in a timestamp is a 500 rather than a 400. | medium |
| E2 | `useSchedule.ts:9` | `.then()` with no `.catch()`. A failed fetch leaves `loading` true forever and the rejection is unhandled. `useFlags.ts` in the base does this correctly with try/catch/finally. | medium |

## Concurrency

| # | Where | Defect | Severity |
|---|---|---|---|
| CC1 | `schedule.py:27` | `apply_due_changes` reads unapplied changes, then writes, with no locking or `SKIP LOCKED`. Two scheduler workers apply the same change twice, double-ramping the flag. | medium |

## Performance

| # | Where | Defect | Severity |
|---|---|---|---|
| P1 | `evaluate.py:42` | The segment query sits **inside** the per-flag loop and does not depend on the flag — the same rows are loaded once per flag on the hottest endpoint in the service. `rules_by_flag` directly above (line 32) batches correctly in one query, which is the contrast that proves it. | medium |

## Maintainability

| # | Where | Defect | Severity |
|---|---|---|---|
| M1 | `frontend/src/api.ts:85` | `(flag as any).schedules` casts away the type to read a field `types.ts` does not declare and the API does not return. It is always `undefined`, so `listSchedules` always returns `[]`. | medium |
| M2 | `ScheduleBadge.tsx:28` | Array index as React `key` on a list that is sorted and filtered. | low |

## Deliberately clean — false-positive bait

A comment on any of these is a false positive.

| # | Where | Why it is correct |
|---|---|---|
| OK1 | `flags.py:118` `tick` | Correctly gated with `require_admin`, in the same file as the ungated S1. |
| OK2 | `schedule.py:44` `parse_apply_at` | Correct tz handling: naive input is assumed UTC rather than compared against an aware value. |
| OK3 | `segments.py:9` `is_member` | Correct delegation to the existing matcher, right beside the `any`/`all` bug. |
| OK4 | `evaluate.py:32` `rules_by_flag` | One query for every flag's rules, beside the N+1 at line 42. |
| OK5 | `ScheduleBadge.tsx:9` `when()` | Correct relative-time arithmetic and correct `loading` guard before the render. |

## What makes this set different

Several defects **hide each other**, which is closer to real code than an
independent list:

- S1 removes the auth that C6 forgot to satisfy.
- C1 makes C2 and C3 unobservable, because the ramp never runs twice.
- M1 makes C5 fire on every render, because `listSchedules` always returns `[]`.

A reviewer that reports only the shallowest of each pair has still missed
something real.

## Scoring

- 17 defects, of which **3 are cross-file** (X1–X3).
- 7 high severity.
- X1 is the only one that requires reading a file outside the diff. It has
  been missed in every run so far and is the premise of the whole agentic
  design.
- Any comment on OK1–OK5 counts against precision.
