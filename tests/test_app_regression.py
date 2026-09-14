"""Regression guard for the pre-existing (deterministic) API surface."""

from __future__ import annotations


def test_shops_endpoint_shape(client):
    shops = client.get("/api/shops").json()
    assert len(shops) == 5
    assert {"id", "name", "location", "seats", "opened_on"} <= set(shops[0])


def test_stats_daily_filters(client):
    rows = client.get("/api/stats/daily",
                      params={"shop_id": 3, "date_from": "2026-03-01",
                              "date_to": "2026-03-31"}).json()
    assert rows and all(r["shop_id"] == 3 for r in rows)
    assert {"day", "tx_count", "revenue", "avg_ticket"} <= set(rows[0])


def test_stats_items_and_baristas(client):
    items = client.get("/api/stats/items").json()
    assert len(items) == 30
    baristas = client.get("/api/stats/barista").json()
    assert len(baristas) == 15


def test_index_page_serves(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Coffee Chain Analytics" in r.text
    # the AI feature is reachable from the UI
    assert "Insights" in r.text
    assert "loadInsights" in r.text
