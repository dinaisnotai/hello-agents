# Constraint-driven planning

The planning pipeline is:

```text
TripRequest
  -> ConstraintExtractor
  -> ConstraintSet
  -> deterministic planner
  -> ConstraintValidationEngine
  -> ValidationResult
  -> violation-driven repair (maximum 3 iterations)
```

Traveler labels are consumed only by the extractor. Presets translate labels
such as `elderly`, `child`, and `wheelchair` into ordinary constraints. The
planner, validators, and repair policy do not branch on traveler identity.

## Module layout

```text
app/constraints/
  schema.py
  extractor.py
  presets.py
  validators/
    time_validator.py
    walking_validator.py
    transport_validator.py
    activity_validator.py
    budget_validator.py
    coverage_validator.py
    engine.py
```

## Supported constraint types

- `daily_end_time`
- `walking_distance`
- `walking_duration`
- `time_window`
- `avoid_activity`
- `prefer_activity`
- `daily_load`
- `accessible_route`
- `transportation`
- `budget`
- `coverage`
- `must_visit`
- `attraction_count`
- `max_areas`
- `avoid_category`

## Case 2: four-day Beijing trip with an elderly traveler

Input:

```text
老人同行，不能长时间步行，中午需要休息
09:00-18:00
每日步行不超过 5km
公共交通
```

Before the constraint architecture, persona requirements remained strings and
could produce late returns, high-intensity attractions, or no explicit rest.

After normalization:

```json
[
  {"type": "walking_duration", "operator": "max", "value": 20, "unit": "minutes"},
  {"type": "avoid_activity", "operator": "avoid", "value": "high_intensity"},
  {"type": "daily_load", "operator": "eq", "value": "low"},
  {"type": "daily_end_time", "operator": "max", "value": "18:00"},
  {"type": "walking_distance", "operator": "max", "value": 5, "unit": "km"},
  {
    "type": "time_window",
    "operator": "require",
    "value": 60,
    "unit": "minutes",
    "start": "12:00",
    "end": "14:00",
    "reason": "rest"
  }
]
```

The deterministic offline regression produces four non-empty days, returns by
18:00, includes a 12:00-13:00 rest block every day, excludes high-intensity
POIs, stays within the combined route and internal walking limit, and uses no
driving segments. The generic `ValidationResult.valid` is `true`.

## Candidate-pool completeness

An LLM attraction specialist result is a recall hint, not a complete planning
pool. For multi-day trips, if it returns fewer than two candidates per day,
the LangGraph deterministic-planning node merges in `POICollector` results
before day allocation. The expanded pool is persisted as
`planning_candidates`, so repair iterations can fill empty days from the same
auditable set.

If hard violations remain after three repair attempts, the graph skips soft
review and finalizes the result as non-executable with an explicit
`failure_reason`. A soft reviewer is never allowed to turn missing-day
coverage into a warning-only success.
