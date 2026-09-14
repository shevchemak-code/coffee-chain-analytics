"""Tests for the review digest feature.

Coverage map (see docs/EDGE_CASES.md for the full list):
  happy path, hallucinated evidence filtering, invalid JSON + one retry,
  schema violations, empty periods, bad inputs (404/400), provider failure,
  degraded mode without a provider, response caching, and the unit-level
  helpers (extract_json, ground_themes, validate_schema, load_prompt).
"""

from __future__ import annotations

import json

from ai import digest as d
from ai.providers import ConfigError, ProviderError
from conftest import FakeProvider, set_provider, static_provider

# Shop 2, Oct–Dec 2025: the seeded milk-quality cluster (see DISCOVERY.md).
MILK_PERIOD = {"shop_id": 2, "date_from": "2025-10-01", "date_to": "2025-12-31"}

VALID_DIGEST = json.dumps({
    "overall_sentiment": "negative",
    "summary": "A recurring complaint about milk quality dominated the period.",
    "themes": [
        {
            "theme": "milk-quality",
            "title": "Milk quality complaints",
            "sentiment": "negative",
            "severity": "high",
            "review_ids": [2, 23],
            "evidence": ["the milk tastes strange"],
            "explanation": "Multiple reviewers reported off-tasting milk in lattes "
                           "and cappuccinos, several mentioning repeat visits.",
        }
    ],
    "recommended_actions": ["Check the dairy supplier's delivery temperature."],
    "data_notes": [],
})


# ---------------------------------------------------------------- happy path

def test_happy_path_returns_grounded_digest(client):
    provider = static_provider(VALID_DIGEST)
    set_provider(provider)
    r = client.get("/api/insights/review-digest", params=MILK_PERIOD)
    assert r.status_code == 200
    body = r.json()
    assert body["degraded"] is False
    assert body["digest"]["themes"][0]["theme"] == "milk-quality"
    assert body["digest"]["themes"][0]["review_ids"] == [2, 23]
    assert body["prompt_version"] == "review_digest_v1"
    assert body["model"] == "fake-model"
    # deterministic stats ride along
    assert body["period"]["stats"]["review_count"] > 0
    # the prompt sent to the model is the versioned file body
    assert "You are an analyst assistant" in provider.calls[0]["system"]
    assert provider.calls[0]["kwargs"]["json_mode"] is True
    assert provider.calls[0]["kwargs"]["json_schema"]["type"] == "object"


def test_digest_cites_only_reviews_present_in_input(client):
    provider = static_provider(VALID_DIGEST)
    set_provider(provider)
    r = client.get("/api/insights/review-digest", params=MILK_PERIOD)
    digest_ids = [rid for t in r.json()["digest"]["themes"]
                  for rid in t["review_ids"]]
    assert digest_ids == [2, 23]
    user_payload = json.loads(provider.calls[0]["user"])
    input_ids = {rv["id"] for rv in user_payload["reviews"]}
    assert set(digest_ids) <= input_ids


# ------------------------------------------------------- grounding / repair

def test_hallucinated_review_ids_are_dropped_and_noted(client):
    bad = json.dumps({
        "overall_sentiment": "negative",
        "summary": "Milk complaints, plus invented staffing claims.",
        "themes": [
            {
                "theme": "milk-quality",
                "title": "Milk quality complaints",
                "sentiment": "negative",
                "severity": "high",
                "review_ids": [2, 999999],
                "evidence": ["the milk tastes strange"],
                "explanation": "Recurring milk complaints.",
            },
            {
                "theme": "ghost-theme",
                "title": "Invented theme",
                "sentiment": "negative",
                "severity": "high",
                "review_ids": [777777],
                "evidence": ["something nobody wrote"],
                "explanation": "Unsupported by any input review.",
            },
        ],
        "recommended_actions": [],
        "data_notes": [],
    })
    provider = static_provider(bad)
    set_provider(provider)
    body = client.get("/api/insights/review-digest", params=MILK_PERIOD).json()
    themes = body["digest"]["themes"]
    assert [t["theme"] for t in themes] == ["milk-quality"]
    assert themes[0]["review_ids"] == [2]  # 999999 filtered out
    notes = " ".join(body["validation_notes"])
    assert "ghost-theme" in notes and "999999" in notes


def test_fully_ungrounded_output_is_degraded(client):
    bad = json.dumps({
        "overall_sentiment": "negative",
        "summary": "Everything is bad.",
        "themes": [{
            "theme": "ghost", "title": "Ghost", "sentiment": "negative",
            "severity": "high", "review_ids": [424242],
            "evidence": ["no such quote"], "explanation": "x",
        }],
        "recommended_actions": [], "data_notes": [],
    })
    set_provider(static_provider(bad))
    body = client.get("/api/insights/review-digest", params=MILK_PERIOD).json()
    assert body["degraded"] is True
    assert body["digest"] is None
    assert "grounding" in body["reason"]


def test_invalid_json_is_retried_once_then_degraded(client):
    provider = static_provider("I cannot do that, sorry.")
    set_provider(provider)
    body = client.get("/api/insights/review-digest", params=MILK_PERIOD).json()
    assert len(provider.calls) == 2  # original + one repair attempt
    assert body["degraded"] is True
    assert "unusable" in body["reason"]


def test_schema_violation_is_degraded_with_notes(client):
    bad = json.dumps({"overall_sentiment": "negative"})  # missing most fields
    set_provider(static_provider(bad))
    body = client.get("/api/insights/review-digest", params=MILK_PERIOD).json()
    assert body["degraded"] is True
    assert "schema" in body["reason"]
    assert any("summary" in n for n in body["validation_notes"])


def test_bad_severity_enum_is_degraded(client):
    data = json.loads(VALID_DIGEST)
    data["themes"][0]["severity"] = "critical"
    set_provider(static_provider(json.dumps(data)))
    body = client.get("/api/insights/review-digest", params=MILK_PERIOD).json()
    assert body["degraded"] is True


# ------------------------------------------------------------- empty inputs

def test_empty_period_does_not_call_llm_and_degrades(client):
    provider = static_provider(VALID_DIGEST)
    set_provider(provider)
    r = client.get("/api/insights/review-digest",
                   params={"shop_id": 4, "date_from": "2025-01-01",
                           "date_to": "2025-01-05"})
    body = r.json()
    assert body["degraded"] is True
    assert "no reviews" in body["reason"]
    assert provider.calls == []  # no money spent on empty input


# ------------------------------------------------------------ bad requests

def test_unknown_shop_is_404(client):
    set_provider(static_provider(VALID_DIGEST))
    r = client.get("/api/insights/review-digest", params={"shop_id": 99})
    assert r.status_code == 404


def test_inverted_date_range_is_400(client):
    set_provider(static_provider(VALID_DIGEST))
    r = client.get("/api/insights/review-digest",
                   params={"shop_id": 2, "date_from": "2026-01-01",
                           "date_to": "2025-01-01"})
    assert r.status_code == 400


# ------------------------------------------------------- provider failures

def test_provider_error_degrades_gracefully(client):
    set_provider(FakeProvider(lambda s, u: ProviderError("HTTP 500: boom")))
    r = client.get("/api/insights/review-digest", params=MILK_PERIOD)
    assert r.status_code == 200
    body = r.json()
    assert body["degraded"] is True and "boom" in body["reason"]
    # deterministic stats still delivered
    assert body["period"]["stats"]["review_count"] > 0


def test_no_provider_configured_degrades(client):
    set_provider(FakeProvider(lambda s, u: ConfigError("ANTHROPIC_API_KEY is not set")))
    body = client.get("/api/insights/review-digest", params=MILK_PERIOD).json()
    assert body["degraded"] is True
    assert "API_KEY" in body["reason"]


# ------------------------------------------------------------------ caching

def test_cache_hits_do_not_recall_llm(client):
    provider = static_provider(VALID_DIGEST)
    set_provider(provider)
    client.get("/api/insights/review-digest", params=MILK_PERIOD)
    client.get("/api/insights/review-digest", params=MILK_PERIOD)
    assert len(provider.calls) == 1
    r = client.get("/api/insights/review-digest",
                   params={**MILK_PERIOD, "refresh": True})
    assert r.status_code == 200
    assert len(provider.calls) == 2


# -------------------------------------------------------------- unit tests

def test_extract_json_handles_fences_and_prose():
    payload = {"a": 1}
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    assert d.extract_json(fenced) == payload
    wrapped = "Sure! Here it is: " + json.dumps(payload) + " — done."
    assert d.extract_json(wrapped) == payload
    try:
        d.extract_json("no json at all")
        assert False, "should have raised"
    except d.DigestError:
        pass


def test_validate_schema_flags_problems():
    assert d.validate_schema(json.loads(VALID_DIGEST)) == []
    problems = d.validate_schema({"overall_sentiment": "wrong", "themes": []})
    assert any("overall_sentiment" in p for p in problems)
    assert any("missing" in p for p in problems)
    assert any("unexpected" in p for p in
               d.validate_schema({**json.loads(VALID_DIGEST), "extra": 1}))


def test_ground_themes_normalizes_quotes():
    reviews = [{"id": 1, "text": "The  milk  tasted STRANGE today."}]
    digest = {"themes": [{
        "theme": "milk", "title": "t", "sentiment": "negative",
        "severity": "high", "review_ids": [1],
        "evidence": ["The milk tasted strange today."],  # case/space-insensitive
        "explanation": "x",
    }]}
    out, notes = d.ground_themes(digest, reviews)
    assert out["themes"][0]["evidence"] == ["The milk tasted strange today."]
    assert notes == []


def test_load_prompt_returns_version_and_body():
    prompt = d.load_prompt()
    assert prompt["version"] == "review_digest_v1"
    assert "analyst assistant" in prompt["body"]
    assert prompt["meta"]["max_input_reviews"] > 0
