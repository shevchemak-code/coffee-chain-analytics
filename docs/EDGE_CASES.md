# Edge cases — what breaks the naive version, and how this one handles it

The naive version of this feature is: "dump reviews into the prompt, print the
model's reply." Every row below is a way that breaks, and the mechanism that
catches it. Anything marked *(tested)* has an automated test in
`tests/test_review_digest.py`.

| # | Input / failure | Naive version breaks by... | Handling in this implementation |
|---|---|---|---|
| 1 | **Empty period** (no reviews match shop/date filter) | Paying an LLM call to summarize nothing; inventing themes | No model call is made; response is `degraded=true`, reason `no reviews in the selected period`. *(tested)* |
| 2 | **Unknown shop_id** | Silently treating it as "all shops" or returning an empty digest | HTTP 404 before any work. *(tested)* |
| 3 | **Inverted date range** (`from > to`) | Silly window or SQL error | HTTP 400 from `DigestError`. *(tested)* |
| 4 | **No API key / provider unconfigured** | Crash on first request | `NoProvider` sentinel → `degraded=true` with the config reason; deterministic stats still returned. *(tested)* |
| 5 | **Provider error / timeout / 5xx** | 500 to the user, no data at all | Caught as `ProviderError` → `degraded=true` + reason; stats still delivered. *(tested)* |
| 6 | **Model returns prose instead of JSON** | `json.loads` crash | `extract_json` tolerates code fences and surrounding prose; one explicit retry with a correction nudge; then degraded. *(tested)* |
| 7 | **Model JSON missing fields / wrong enums** (e.g. `severity: "critical"`) | Partially-rendered UI, or invalid data stored | Schema validation against `OUTPUT_SCHEMA`; any problem → degraded with the full problem list in `validation_notes`. *(tested)* |
| 8 | **Hallucinated citations** (review ids never in the input) | Owner trusts a quote nobody wrote — the worst failure mode for this feature | Grounding pass: ids not in the batch are dropped and reported by value; evidence quotes must be verbatim (whitespace/case-normalized) substrings. *(tested)* |
| 9 | **Fully ungrounded output** (every theme invented) | Confident fiction presented as analysis | If no theme survives grounding in a non-trivial batch, response degrades with `no theme survived grounding checks`. *(tested)* |
| 10 | **Large review volume** | Token overflow, slow calls, high cost | Input capped at `max_input_reviews` (150, from the prompt's front-matter); truncation flagged in `data_notes`/response. *(tested at unit level via `truncated` flag)* |
| 11 | **All-positive period** | Model inventing problems to seem useful | Prompt instructs calibration ("if the period was mostly fine, say so"); empty `themes` is a valid output the UI renders explicitly. |
| 12 | **Non-English / gibberish reviews** | Mixed-language output, nonsense themes | Prompt: respond in English, note the mismatch in `data_notes`; adversarial cases like this belong to the golden set in EVALS.md. |
| 13 | **Single review describing food safety** | Rule "themes need ≥2 reviews" hiding the most urgent signal | Prompt allows a single-review theme when it alleges health/safety issues. |
| 14 | **Repeat calls for the same period** | Paying for the same digest on every page load | In-process TTL cache (15 min) keyed by (shop, dates, prompt version); `refresh=true` bypasses. *(tested)* |
| 15 | **Prompt edited in production** | Silent behaviour change, no way to trace old answers | Version id lives in front-matter, is returned in every response, and is part of the cache key; eval runs are labelled by it. |
| 16 | **Long-running server, cache growth** | Unbounded memory | Cache entries are small (one JSON per distinct filter combo); a production version would add an LRU bound — noted as known limitation. |

## Known limitations (deliberate, documented)

- The grounding check is substring-based. A model that *paraphrases* a real
  review's words slightly will have that evidence dropped. That's the safe
  direction (false negative beats false positive here), and the theme survives
  as long as at least one id or one exact quote checks out.
- The cache is per-process. Multiple uvicorn workers would each hold their own
  copy — acceptable at this scale; a shared cache belongs with the production
  deploy, not this codebase.
- Degraded responses are still cached for the TTL, so a 5-minute provider outage
  doesn't hammer the API on every click.
