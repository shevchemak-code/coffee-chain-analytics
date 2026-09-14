# EVALS — measuring production quality of the review digest

If this shipped tomorrow, "is it still working?" has three different meanings.
Each gets its own signal, because no single number covers all three.

---

## 1. Is the plumbing alive? (liveness)

**What breaks:** provider outage, auth expiry, rate limits, timeouts.

**Metric & detection:**
- `degraded_response_ratio` — share of `/api/insights/review-digest` responses
  with `degraded=true`, broken down by `reason`. Alert if > 5% over an hour
  (excluding the expected "no reviews in period" reason, which is data, not failure).
- `p95_latency` of the endpoint. Alert > 10 s.
- Synthetic canary: a scheduled job calls the endpoint with a fixed shop/period
  (shop 2, Oct–Dec 2025) every 30 minutes and asserts `degraded=false` and
  `digest.themes != null`. This is the "is the loop closed right now" heartbeat,
  and it runs against a frozen period so drift in live data can't mask outages.

## 2. Is the output structurally sound? (schema / grounding invariants)

These are deterministic — checked on **every** response, not sampled:

- JSON parses, schema-validates, and uses only allowed enum values
  (else the code degrades; we already count this in `validation_notes`).
- 100% of `review_ids` exist in the input batch; 100% of `evidence` quotes are
  verbatim substrings (normalized). Target: 100%. This is enforced, not measured —
  but the *rate at which themes get dropped by grounding* is a leading indicator
  of model quality drift: if it rises after a model swap, the new model hallucinates
  more. Track `themes_dropped_by_grounding / themes_returned` per model version.

## 3. Is the analysis actually good? (semantic quality)

This is the part that needs a dataset and humans (or a grader model with care).

### Dataset I would build

Golden set: ~60 digest "tasks" = (shop, period, input reviews) → expected output,
built three ways:

1. **12 seeded-scenario cases** — periods engineered to contain a known planted
   signal (the Riverside milk cluster, the Campus Grounds decline window, quiet
   healthy periods, a single health-safety review). Expected output is written
   once by a human who read the reviews. These test *recall of what matters*.
2. **~40 stratified real cases** — random (shop, 90-day) windows from production
   data, labelled by the ops owner: which themes matter, severity calibration.
   These test precision on unremarkable data (most of reality is unremarkable).
3. **~10 adversarial cases** — foreign-language reviews, emoji/gibberish spam,
   one-word reviews, a period with 200 identical copy-paste reviews, reviews that
   contradict the rating ("love this place" ★1). These test robustness.

Labels store: theme labels (accepting near-duplicates via a mapping table),
severity, and a flag "must-mention" / "must-not-appear".

### Metrics

- **Theme recall on must-mention cases**: did the digest surface the planted
  signal? Target ≥ 0.9 on seeded scenarios (with the shop+period given).
- **Theme precision on stratified cases**: of reported themes, share a reviewer
  accepts as real and correctly evidenced. Target ≥ 0.8.
- **Severity calibration**: share of themes where reviewer agrees with severity
  ±1 level. Target ≥ 0.85.
- **Groundedness** (from section 2) on golden runs: must be 1.0 by construction;
  any drop is a release blocker, not a metric to optimize.
- **Actionability**: on a 1–5 rubric, "would you act on this?" Target mean ≥ 3.5.

### Regression gates (model or prompt changes)

Every candidate prompt version (`review_digest_vN`) or model swap runs the golden
set offline before deploy:

1. Same input fixtures, provider switched to the candidate → run all cases,
   record metrics per case.
2. Block release if: theme recall drops > 5 pts vs `v(N-1)` baseline on
   seeded scenarios, groundedness < 100%, or any adversarial case crashes the
   pipeline (non-degraded 500).
3. Prompt diffs are code-reviewed like code; the version id in the front-matter
   and the cache key mean old and new prompts can be A/B'd in production by
   routing a fraction of requests to each and comparing `themes_dropped_by_grounding`
   and canary outcomes.
4. Keep a shadow log: every production response stores `{prompt_version, model,
   usage, validation_notes}` (the endpoint already returns/records these), so any
   metric can be sliced by exact prompt+model pair. A month after ship, "is it
   still working" = re-run the golden set against the current pinned version and
   diff the metrics — plus watch the canary and degraded-ratio dashboards.

### Human-in-the-loop signal that costs nothing to add

The UI renders evidence quotes next to every theme. Adding a one-click
"evidence wrong" flag per theme would turn owner usage into a continuous
label stream for the stratified dataset — the cheapest possible eval data
collection, and it directly measures the failure mode that matters.

---

## Cost & latency budget (per digest call)

Provider: **Anthropic Claude Haiku** (default; OpenAI `gpt-4o-mini` is a
drop-in via `LLM_PROVIDER=openai`). Rationale: the task is structured extraction
+ short synthesis over ≤ 150 short reviews — a small, fast model with forced
structured output; a frontier model adds cost and latency with no measurable
quality gain at this input size.

- Cost: input ≈ 2–4k tokens, output ≈ 300–500 tokens → roughly **$0.001–0.002
  per digest** on Haiku pricing; ~$0.002–0.004 on gpt-4o-mini.
- Latency: p50 ≈ 2–4 s from the API; acceptable for an on-demand dashboard
  action, and the 15-minute cache plus degraded mode mean a slow model never
  blocks the page.
- Trade-off in one sentence: Haiku is ~10–30× cheaper per call than a frontier
  model and fast enough for interactive use, and its quality deficit on this
  narrow task is absorbed by the deterministic grounding checks — so the risk
  of a cheap model (looser adherence) is paid for in validation, not in price.
