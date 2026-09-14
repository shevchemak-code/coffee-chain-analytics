"""Test fixtures: in-memory FakeProvider + TestClient with the provider injected.

No network, no API keys: the FakeProvider stands in for Anthropic/OpenAI and
lets each test script the model's behaviour — including its failure modes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from ai.providers import ConfigError, LLMProvider, LLMResponse, ProviderError  # noqa: E402


class FakeProvider(LLMProvider):
    """Scripted provider. `handler(system, user)` returns response text or raises."""

    name = "fake"

    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.model = "fake-model"

    def complete(self, system, user, **kwargs):
        self.calls.append({"system": system, "user": user, "kwargs": kwargs})
        out = self.handler(system, user)
        if isinstance(out, Exception):
            raise out
        return LLMResponse(text=out, model=self.model, usage={"tokens": 1})

    def describe(self):
        return "fake"


def static_provider(text):
    return FakeProvider(lambda system, user: text)


@pytest.fixture()
def client():
    """Fresh TestClient with a provider the test can replace via client.app.state."""
    main._provider = None
    main._digest_cache.clear()
    with TestClient(main.app) as c:
        yield c
    main._provider = None
    main._digest_cache.clear()


def set_provider(provider):
    main._provider = provider
