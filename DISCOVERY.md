# Discovery — where AI actually helps in this dashboard

This document answers four questions: what AI-driven opportunities exist in the
coffee-chain dashboard, how to prioritize them, what the top 3 would look like
architecturally, and what I would deliberately *not* build with AI.

The guiding principle: an LLM earns its place where language is the raw material
or the output — unstructured text in, narrative judgment out. For anything that
is a number computed from structured data, deterministic code is cheaper, faster,
and verifiable. The best features here are hybrids: SQL computes the facts, the
model interprets them.

---

## 1. Opportunities

### O1. Review theme digest (per shop, per period)
- **What it does:** turns a pile of free-text reviews into a short structured
  brief: recurring themes, their sentiment and severity, representative quotes,
  and suggested actions. "Riverside: 13 reviews in Oct–Dec mention milk quality —
  investigate dairy supplier" instead of reading 200 reviews.
- **Signal / data:** `reviews` (text + rating + timestamp + shop_id), optionally
  joined with transaction trends for the same period.
- **Value:** the only channel where customers explain *why* numbers move. Catches
  emerging quality issues weeks before they show up in revenue. Cheap to run
  (small input), output is easy to verify against source quotes.

### O2. Weekly KPI narrative with anomaly callouts
- **What it does:** a plain-language summary of the week's sales per shop —
  "Campus Grounds revenue fell 21% over 6 months, accelerating in April" — with
  the model cross-referencing reviews, item mix, and shifts to suggest drivers.
- **Signal / data:** `/api/stats/daily`, `/api/stats/items`, `/api/stats/barista`,
  review digest output from O1.
- **Value:** the owner currently reads raw tables. One screened paragraph beats
  scanning 1,800 day-rows. The deterministic layer (stats endpoints) already
  exists; the model adds interpretation, not computation.

### O3. Natural-language query layer ("ask the dashboard")
- **What it does:** owner types "which barista sells the most pastries?" and gets
  an answer plus the SQL that produced it.
- **Signal / data:** the whole schema; the existing stats endpoints as validated
  query templates.
- **Value:** removes the need to pre-build every cut of the data. Highest
  flexibility, but also the highest hallucination surface — mitigated by
  read-only execution, schema whitelisting, and showing the SQL.

### O4. Review response drafting
- **What it does:** drafts a reply to a negative review in the shop's tone; the
  owner approves/edits before anything is sent.
- **Signal / data:** review text, shop context, a short tone guide.
- **Value:** saves owner time on an emotionally draining task. Decent, but
  low-stakes-volume at 5 shops — below the cut.

### O5. Demand forecast for staffing and ordering
- **What it does:** per-shop, per-day predicted transactions; feeds shift
  planning and pastry prep quantities.
- **Signal / data:** transactions (time series), seasonality, day-of-week,
  school calendar for the campus shop.
- **Value:** real money (labor + waste). But the right tool is classical
  time-series ML, not an LLM — noted here and deliberately excluded from the
  LLM shortlist (see section 4).

### O6. Menu engineering assistant
- **What it does:** basket analysis ("iced latte + blueberry muffin" pairs),
  margin-based recommendations, seasonal menu suggestions.
- **Signal / data:** transaction_items, menu margins.
- **Value:** moderate. Most of this is deterministic SQL + well-known heuristics;
  the LLM part (creative seasonal suggestions) is nice-to-have, not core.

### O7. Cross-sell prompts at POS
- **What it does:** "customer ordered an americano — suggest a croissant."
- **Signal / data:** co-occurrence in transaction_items.
- **Value:** real but marginal at this scale; mostly a deterministic
  association-rules problem with a thin generative layer.

---

## 2. Prioritization

Criteria, in order of weight:

1. **Verifiability** — can a human check the output against ground truth in
   seconds? Hallucination is the dominant failure mode of LLM features, so
   features whose outputs carry their own evidence (quotes, SQL, cited numbers)
   rank highest.
2. **Value density** — impact on the owner's decisions × how often the decision
   recurs. A weekly digest beats a once-a-quarter menu brainstorm.
3. **Data readiness** — does the signal already exist in the DB in usable form?
4. **Cost & latency fit** — interactive UI features need < ~3 s; batch features
   can be slow but shouldn't cost more than the coffee they help sell.
5. **Blast radius of being wrong** — a wrong theme label is embarrassing; a wrong
   auto-reply is public; a wrong reorder is money lost.

Scoring (H/M/L against the five criteria):

| Opportunity | Verifiable | Value density | Data ready | Cost/latency | Safe-if-wrong | Total |
|---|---|---|---|---|---|---|
| O1 Review digest | H (quotes) | H (weekly, all shops) | H | H (small input, batch) | H | **Top** |
| O2 KPI narrative | M (numbers cited from SQL) | H (weekly) | H | M | M–H | **Top** |
| O3 NL queries | M (SQL shown) | M–H | M (needs mapping layer) | M (interactive) | M (read-only) | **Top** |
| O4 Review replies | M | M (low volume) | H | H | L (public text) | 4th |
| O6 Menu engineering | M | M | H | H | H | 5th |
| O7 POS cross-sell | H | M | H | M (in-lane latency) | M | 6th |
| O5 Forecast | H | H | M | M | M | build with ML, not LLM |

**Top 3: O1, O2, O3.** They form a coherent ladder — O1 interprets the
unstructured channel, O2 narrates the structured channels, O3 generalizes both
into free-form exploration. O1 is implemented in Part 2.

---

## 3. Architectural sketch of the top 3

### O1 — Review theme digest

```
SQLite ──(SQL: reviews + stats per shop/period)──▶ Prompt assembly
                                                        │  system prompt (versioned .md)
                                                        ▼
                                                   LLM API (structured JSON)
                                                        │
                                                        ▼
                                              Validation & grounding checks
                                              (JSON schema, quote-id cross-check)
                                                        │
                                    cache (in-mem, keyed by shop+dates+prompt ver)
                                                        │
                                                        ▼
                                              GET /api/insights/review-digest
```

- **Deterministic:** data fetching, aggregation (review counts, avg rating by
  shop/period), prompt assembly, JSON-schema validation, checking that every
  quoted `review_id` actually exists in the input batch, caching.
- **Model-based:** theme extraction, severity judgment, action phrasing.
- **Data required:** reviews for the period (cap input size), shop list,
  optional revenue trend for context.
- **Caching / guardrails:** in-process TTL cache keyed by (shop, dates, prompt
  version); schema validation with hard failure → 502 rather than showing
  unvalidated text; grounding check drops hallucinated quote references;
  degraded mode returns the deterministic stats with an explicit "AI unavailable"
  flag if the provider errors.

### O2 — Weekly KPI narrative

- Same hub-and-spoke: all numbers come from the *existing* stats endpoints,
  serialized into the prompt as labeled facts. **Nothing numeric is generated.**
- **Deterministic:** every statistic; diffing week-over-week; anomaly detection
  (z-score on daily revenue) that decides *what deserves a mention*.
- **Model-based:** ordering, phrasing, connecting review themes (from O1's
  cached output) to KPI movements with hedged language ("possibly linked to").
- **Guardrails:** numbers injected as data, not prose, so the model cannot
  invent them; template asks for a fixed section structure; render numbers in
  the UI from the SQL response, not from model text, so a hallucinated figure
  never reaches the screen.

### O3 — Natural-language query layer

- **Deterministic:** schema introspection limited to a whitelist of tables/columns;
  the model may only pick from pre-built parameterized query templates plus a
  constrained SQL dialect; execution in a read-only connection with a row cap
  and a statement timeout.
- **Model-based:** intent → template/SQL mapping, and answer phrasing over the
  returned rows.
- **Guardrails:** SQL parsed and rejected on anything but SELECT; column/table
  names validated against the whitelist; results always shown with the generated
  SQL so the user can audit; refusal path ("I can't answer that with this data")
  when confidence is low. This is the riskiest of the three, which is why it
  ships last, after O1/O2 have established the provider, caching, and
  eval infrastructure.

---

## 4. What I would deliberately NOT build with AI

1. **Any raw metric on the dashboard.** Revenue, avg ticket, units sold — these
   are `SUM`/`AVG` over indexed tables. An LLM version is slower, costs money,
   and can be confidently wrong. Failure mode: a fluent paragraph containing a
   hallucinated number that the owner quotes to their accountant. Deterministic
   SQL forever.
2. **LLM-based demand forecasting.** Time-series extrapolation from 12 months of
   daily data with strong weekly/seasonal structure is a solved problem for
   classical methods ( Holt-Winters / gradient-boosted trees). An LLM adds
   variance, not accuracy, and its errors are unbounded. If O5 gets built, it
   gets a forecaster, not a chatbot. Failure mode: overfit narrative — the model
   "explains" noise, and the owner cuts a profitable shift.
3. **Fully automated review replies.** A hallucinated apology ("we fired the
   barista involved") or tone-deaf reply is public and permanent; volume at
   5 shops doesn't justify the risk. Keep O4 human-in-the-loop, or don't build
   it. Failure mode: brand damage at zero review-volume savings.
4. **Automatic purchasing / reorder decisions.** Hallucinated par levels become
   spoiled milk — literally, given the data. Cost-minimizing loops belong to
   deterministic inventory math with human sign-off. Failure mode: compounding
   monetary loss from an error nobody reads.
5. **Barista performance scoring by AI.** Ranking people from noisy, confounded
   data (avg ticket conflates shift, location, and menu mix) is a fairness and
   trust failure, and the model will happily rationalize whatever pattern it
   finds. Failure mode: demoralized staff + unfalsifiable "AI says you're slow".
6. **Chatbots for customers.** Nothing in this product is customer-facing; a
   support bot trained on 200 templated reviews would be a toy. Failure mode:
   answering questions the data can't support, in public.

The common thread: AI is a **judgment layer over evidence**, never the evidence
itself. Where output can't be checked against the database, or where being wrong
is expensive and irreversible, the feature either gets deterministic code or
gets cut.
