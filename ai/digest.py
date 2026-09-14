"""Review theme digest — deterministic assembly + model interpretation.

Pipeline for GET /api/insights/review-digest:

    SQL (reviews + stats for period and previous period)
        -> prompt assembly (versioned prompt file + bounded input)
        -> LLM call (forced structured output)
        -> JSON extraction + schema validation
        -> grounding checks (review ids exist, quotes are verbatim substrings)
        -> DigestResult (digest + meta, or degraded with reason)

Everything except the model call itself is deterministic and unit-tested.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml  # provided by the fastapi dependency chain

from ai.providers import LLMProvider, ProviderError, ConfigError

PROMPT_PATH = Path(__file__).parent / "prompts" / "review_digest_v1.md"

SEVERITIES = ("high", "medium", "low")
SENTIMENTS = ("positive", "neutral", "negative", "mixed")
MAX_EVIDENCE_QUOTES = 4

# Contract mirrored from the prompt file. The validator owns the schema;
# the prompt only names the fields.
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["overall_sentiment", "summary", "themes",
                 "recommended_actions", "data_notes"],
    "properties": {
        "overall_sentiment": {"type": "string", "enum": list(SENTIMENTS)},
        "summary": {"type": "string", "minLength": 1},
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["theme", "title", "sentiment", "severity",
                             "review_ids", "evidence", "explanation"],
                "properties": {
                    "theme": {"type": "string"},
                    "title": {"type": "string"},
                    "sentiment": {"type": "string", "enum": list(SENTIMENTS[:3])},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "review_ids": {"type": "array", "items": {"type": "integer"}},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "explanation": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "recommended_actions": {"type": "array", "items": {"type": "string"}},
        "data_notes": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


class DigestError(Exception):
    """Fatal for this request: input invalid or output unusable even after repair."""


@dataclass
class DigestResult:
    digest: Optional[dict]
    degraded: bool = False
    reason: Optional[str] = None
    validation_notes: list = field(default_factory=list)
    prompt_version: str = "unknown"
    model: str = "none"
    usage: dict = field(default_factory=dict)

    def to_response(self) -> dict:
        return {
            "digest": self.digest,
            "degraded": self.degraded,
            "reason": self.reason,
            "validation_notes": self.validation_notes,
            "prompt_version": self.prompt_version,
            "model": self.model,
        }


# ---------------------------------------------------------------- loading

_prompt_cache: Optional[dict] = None


def load_prompt(path: Path = PROMPT_PATH) -> dict:
    """Load the versioned prompt file: YAML front-matter + markdown body."""
    global _prompt_cache
    if _prompt_cache is not None and path == PROMPT_PATH:
        return _prompt_cache
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.DOTALL)
    if not m:
        raise DigestError(f"prompt file {path} has no YAML front-matter")
    meta = yaml.safe_load(m.group(1))
    body = m.group(2).strip()
    prompt = {"meta": meta, "body": body, "version": meta.get("version", "unknown")}
    if path == PROMPT_PATH:
        _prompt_cache = prompt
    return prompt


# ---------------------------------------------------------------- data assembly

def _period_bounds(date_from: Optional[str], date_to: Optional[str],
                   latest_ts: str) -> tuple[str, str]:
    """Resolve (from, to) for the current period; default = last 90 days of data."""
    to = date_to or latest_ts[:10]
    if date_from:
        return date_from, to
    to_d = date.fromisoformat(to)
    return (to_d - timedelta(days=90)).isoformat(), to


def fetch_review_batch(conn, shop_id: Optional[int], date_from: Optional[str],
                       date_to: Optional[str]) -> dict:
    """Deterministic input: reviews + aggregate stats for period & previous period."""
    latest = conn.execute("SELECT MAX(ts) FROM reviews").fetchone()[0]
    if latest is None:
        return {"reviews": [], "stats": {}, "previous_stats": {},
                "period": {"from": date_from, "to": date_to}, "truncated": False}

    d_from, d_to = _period_bounds(date_from, date_to, latest)
    if d_from > d_to:
        raise DigestError(f"date_from ({d_from}) is after date_to ({d_to})")

    span_days = max(1, (date.fromisoformat(d_to)
                        - date.fromisoformat(d_from)).days + 1)
    prev_to = (date.fromisoformat(d_from) - timedelta(days=1)).isoformat()
    prev_from = (date.fromisoformat(prev_to)
                 - timedelta(days=span_days - 1)).isoformat()

    def stats(f, t):
        where = "ts >= ? AND ts <= ?"
        params: list = [f, t]
        if shop_id is not None:
            where += " AND shop_id = ?"
            params.append(shop_id)
        row = conn.execute(
            f"""SELECT COUNT(*) AS n, ROUND(AVG(rating), 2) AS avg_rating,
                       SUM(CASE WHEN rating <= 2 THEN 1 ELSE 0 END) AS low
                FROM reviews WHERE {where}""", params).fetchone()
        return {"review_count": row["n"], "avg_rating": row["avg_rating"],
                "low_rating_count": row["low"]}

    where = "r.ts >= ? AND r.ts <= ?"
    params: list = [d_from, d_to]
    if shop_id is not None:
        where += " AND r.shop_id = ?"
        params.append(shop_id)
    rows = conn.execute(
        f"""SELECT r.id, r.shop_id, s.name AS shop_name, r.ts, r.rating, r.text
            FROM reviews r JOIN shops s ON s.id = r.shop_id
            WHERE {where} ORDER BY r.ts DESC""", params).fetchall()

    prompt_cfg = load_prompt()["meta"]
    cap = int(prompt_cfg.get("max_input_reviews", 150))
    truncated = len(rows) > cap
    reviews = [
        {"id": r["id"], "shop": r["shop_name"], "ts": r["ts"],
         "rating": r["rating"], "text": r["text"]}
        for r in rows[:cap]
    ]
    return {
        "reviews": reviews,
        "stats": stats(d_from, d_to),
        "previous_stats": stats(prev_from, prev_to),
        "period": {"from": d_from, "to": d_to},
        "truncated": truncated,
    }


def build_user_message(batch: dict, shop_label: str) -> str:
    payload = {
        "shop": shop_label,
        "period": batch["period"],
        "stats_this_period": batch["stats"],
        "stats_previous_period": batch["previous_stats"],
        "input_truncated_to_most_recent": batch["truncated"],
        "reviews": batch["reviews"],
    }
    return json.dumps(payload, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- output repair

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict:
    """Tolerate ```json fences and leading/trailing prose around the JSON object."""
    candidate = text.strip()
    m = _FENCE_RE.search(candidate)
    if m:
        candidate = m.group(1).strip()
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            candidate = candidate[start:end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise DigestError(f"model output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise DigestError("model output is not a JSON object")
    return data


def _type_ok(value, spec) -> bool:
    t = spec.get("type")
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    return True


def validate_schema(data: dict, schema: dict = OUTPUT_SCHEMA) -> list:
    """Structural validation returning a list of problems ([] == valid)."""
    problems = []
    for key in schema.get("required", []):
        if key not in data:
            problems.append(f"missing required field: {key}")
    props = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        for key in data:
            if key not in props:
                problems.append(f"unexpected field: {key}")
    for key, spec in props.items():
        if key not in data:
            continue
        value = data[key]
        if not _type_ok(value, spec):
            problems.append(f"field {key!r} has wrong type")
            continue
        if "enum" in spec and value not in spec["enum"]:
            problems.append(f"field {key!r}={value!r} not in {spec['enum']}")
        if spec.get("type") == "string" and spec.get("minLength") \
                and not value.strip():
            problems.append(f"field {key!r} is empty")
        if spec.get("type") == "array" and "items" in spec:
            for i, item in enumerate(value):
                if spec["items"].get("type") == "object" and isinstance(item, dict):
                    for p in validate_schema(item, spec["items"]):
                        problems.append(f"{key}[{i}]: {p}")
                elif not _type_ok(item, spec["items"]):
                    problems.append(f"{key}[{i}] has wrong type")
    return problems


def _normalize(s: str) -> str:
    return " ".join(s.lower().split())


def ground_themes(digest: dict, reviews: list) -> tuple[dict, list]:
    """Drop hallucinated evidence. Every review_id must exist in the input batch;
    every evidence quote must be a verbatim (normalized) substring of some input
    review. Themes that end with no valid evidence are removed and noted."""
    known = {r["id"]: r["text"] for r in reviews}
    corpus = [_normalize(r["text"]) for r in reviews]
    notes = []
    kept = []
    dropped = 0
    for theme in digest.get("themes", []):
        valid_ids = [rid for rid in theme.get("review_ids", []) if rid in known]
        bad_id_list = [rid for rid in theme.get("review_ids", []) if rid not in known]
        valid_ev, seen = [], set()
        for q in theme.get("evidence", [])[:MAX_EVIDENCE_QUOTES]:
            nq = _normalize(q)
            if nq and nq in corpus and nq not in seen:
                valid_ev.append(q)
                seen.add(nq)
        if bad_id_list:
            notes.append(
                f"theme '{theme.get('theme')}': dropped review id(s) not present "
                f"in the input batch: {bad_id_list}")
        if not valid_ids and not valid_ev:
            dropped += 1
            notes.append(
                f"theme '{theme.get('theme')}': removed — no verifiable evidence "
                f"in the input batch")
            continue
        theme["review_ids"] = valid_ids
        theme["evidence"] = valid_ev
        kept.append(theme)
    digest["themes"] = kept
    if dropped and not kept:
        notes.append("all themes removed by grounding checks")
    return digest, notes


# ---------------------------------------------------------------- orchestration

def generate_digest(conn, provider: LLMProvider, *, shop_id: Optional[int],
                    date_from: Optional[str], date_to: Optional[str]) -> DigestResult:
    prompt = load_prompt()
    version = prompt["version"]

    batch = fetch_review_batch(conn, shop_id, date_from, date_to)
    shop_label = ("all shops" if shop_id is None
                  else (conn.execute("SELECT name FROM shops WHERE id = ?",
                                     (shop_id,)).fetchone() or [None])[0])
    if shop_id is not None and shop_label is None:
        raise DigestError(f"shop {shop_id} does not exist")

    # Edge: nothing to summarize -> don't spend an LLM call on empty input.
    if not batch["reviews"]:
        return DigestResult(
            digest=None, degraded=True,
            reason="no reviews in the selected period",
            prompt_version=version, model="none")

    if batch["truncated"]:
        note = (f"input truncated to the {len(batch['reviews'])} most recent "
                f"reviews")
    else:
        note = None

    try:
        resp = provider.complete(
            prompt["body"],
            build_user_message(batch, shop_label),
            json_mode=True, json_schema=OUTPUT_SCHEMA,
        )
    except ConfigError as exc:
        return DigestResult(digest=None, degraded=True, reason=str(exc),
                            prompt_version=version,
                            model=provider.describe())
    except ProviderError as exc:
        return DigestResult(digest=None, degraded=True, reason=str(exc),
                            prompt_version=version, model=resp_model(provider))

    # One repair attempt for unparseable JSON, then give up degraded.
    try:
        data = extract_json(resp.text)
    except DigestError as first_exc:
        try:
            retry = provider.complete(
                prompt["body"],
                build_user_message(batch, shop_label)
                + "\n\nYour previous reply was not a single valid JSON object. "
                  "Reply with ONLY the JSON object.",
                json_mode=True, json_schema=OUTPUT_SCHEMA,
            )
            data = extract_json(retry.text)
            resp = retry
        except (DigestError, ProviderError, ConfigError) as exc:
            return DigestResult(digest=None, degraded=True,
                                reason=f"model output unusable: {exc}",
                                prompt_version=version, model=resp.model)

    problems = validate_schema(data)
    if problems:
        return DigestResult(digest=None, degraded=True,
                            reason="model output failed schema validation",
                            validation_notes=problems,
                            prompt_version=version, model=resp.model)

    data, notes = ground_themes(data, batch["reviews"])
    if note:
        notes.append(note)
    if not data.get("themes") and batch["stats"]["review_count"] >= 5:
        # Nothing survived grounding in a non-trivial batch: treat as ungrounded.
        return DigestResult(digest=None, degraded=True,
                            reason="no theme survived grounding checks",
                            validation_notes=notes,
                            prompt_version=version, model=resp.model)

    return DigestResult(digest=data, degraded=False, validation_notes=notes,
                        prompt_version=version, model=resp.model,
                        usage=getattr(resp, "usage", {}) or {})


def resp_model(provider: LLMProvider) -> str:
    return getattr(provider, "model", None) or provider.describe()
