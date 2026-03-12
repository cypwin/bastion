"""Tests for S6 session profiles, intent declarations, and /broker/intent API.

Tests cover:
  - SessionProfile and IntentDeclaration Pydantic model parsing
  - POST /broker/intent endpoint (profile-based and ad-hoc)
  - GET /broker/intents listing
  - Priority mapping: council->INTERACTIVE, extraction->PIPELINE, embedding->BACKGROUND
  - Integration with BrokerConfig session_profiles
  - Intent-based priority resolution in the proxy (S11)
  - Intent lifecycle: declare -> active -> complete/delete (S11)
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from bastion.models import (
    BrokerConfig,
    IntentDeclaration,
    IntentResponse,
    ModelInfo,
    PriorityTier,
    SchedulerConfig,
    SessionProfile,
)
from bastion.proxy import OllamaProxy


# ---------------------------------------------------------------------------
# SessionProfile model tests
# ---------------------------------------------------------------------------


class TestSessionProfile:
    def test_basic_profile(self) -> None:
        profile = SessionProfile(
            model_sequence=["qwen3:8b", "mistral-nemo:12b"],
            default_priority=PriorityTier.INTERACTIVE,
            description="Test pipeline",
        )
        assert profile.model_sequence == ["qwen3:8b", "mistral-nemo:12b"]
        assert profile.default_priority == PriorityTier.INTERACTIVE
        assert profile.description == "Test pipeline"

    def test_defaults(self) -> None:
        profile = SessionProfile(model_sequence=["qwen3:8b"])
        assert profile.default_priority == PriorityTier.AGENT
        assert profile.description == ""

    def test_serialization(self) -> None:
        profile = SessionProfile(
            model_sequence=["a", "b"],
            default_priority=PriorityTier.PIPELINE,
        )
        data = profile.model_dump()
        assert data["model_sequence"] == ["a", "b"]
        assert data["default_priority"] == "pipeline"

    def test_council_profile_priority(self) -> None:
        """Council pipeline should map to INTERACTIVE priority."""
        profile = SessionProfile(
            model_sequence=[
                "qwen3:30b-a3b-instruct-2507-q4_K_M",
                "phi4:14b-q4_K_M",
                "mistral-nemo:12b",
                "qwen3:30b-a3b-instruct-2507-q4_K_M",
            ],
            default_priority=PriorityTier.INTERACTIVE,
        )
        assert profile.default_priority == PriorityTier.INTERACTIVE

    def test_extraction_profile_priority(self) -> None:
        """Extraction pipeline should map to PIPELINE priority."""
        profile = SessionProfile(
            model_sequence=["nuextract", "qwen3:8b"],
            default_priority=PriorityTier.PIPELINE,
        )
        assert profile.default_priority == PriorityTier.PIPELINE

    def test_embedding_profile_priority(self) -> None:
        """Embedding pipeline should map to BACKGROUND priority."""
        profile = SessionProfile(
            model_sequence=["nomic-embed-text"],
            default_priority=PriorityTier.BACKGROUND,
        )
        assert profile.default_priority == PriorityTier.BACKGROUND


# ---------------------------------------------------------------------------
# IntentDeclaration model tests
# ---------------------------------------------------------------------------


class TestIntentDeclaration:
    def test_auto_id(self) -> None:
        intent = IntentDeclaration()
        assert len(intent.intent_id) == 12

    def test_unique_ids(self) -> None:
        ids = {IntentDeclaration().intent_id for _ in range(100)}
        assert len(ids) == 100

    def test_profile_based(self) -> None:
        intent = IntentDeclaration(
            profile="council_pipeline",
            client_id="test_client",
        )
        assert intent.profile == "council_pipeline"
        assert intent.model_sequence is None

    def test_ad_hoc_sequence(self) -> None:
        intent = IntentDeclaration(
            model_sequence=["model_a", "model_b"],
            estimated_requests=20,
        )
        assert intent.model_sequence == ["model_a", "model_b"]
        assert intent.estimated_requests == 20
        assert intent.profile is None

    def test_defaults(self) -> None:
        intent = IntentDeclaration()
        assert intent.estimated_requests == 10
        assert intent.client_id == "anonymous"
        assert intent.created_at <= time.time()

    def test_serialization(self) -> None:
        intent = IntentDeclaration(profile="test", client_id="test_client")
        data = intent.model_dump()
        assert data["profile"] == "test"
        assert data["client_id"] == "test_client"
        assert "intent_id" in data
        assert "created_at" in data


# ---------------------------------------------------------------------------
# IntentResponse model tests
# ---------------------------------------------------------------------------


class TestIntentResponse:
    def test_response_fields(self) -> None:
        resp = IntentResponse(
            intent_id="abc123",
            resolved_priority="interactive",
            model_sequence=["model_a"],
            estimated_requests=5,
        )
        assert resp.intent_id == "abc123"
        assert resp.resolved_priority == "interactive"
        assert resp.status == "registered"


# ---------------------------------------------------------------------------
# BrokerConfig session_profiles integration
# ---------------------------------------------------------------------------


class TestConfigSessionProfiles:
    def test_empty_profiles_by_default(self) -> None:
        config = BrokerConfig()
        assert config.session_profiles == {}

    def test_profiles_from_dict(self) -> None:
        config = BrokerConfig(
            session_profiles={
                "council_pipeline": SessionProfile(
                    model_sequence=["qwen3:30b-a3b-instruct-2507-q4_K_M", "phi4:14b-q4_K_M"],
                    default_priority=PriorityTier.INTERACTIVE,
                    description="Council deliberation",
                ),
                "embedding_pipeline": SessionProfile(
                    model_sequence=["nomic-embed-text"],
                    default_priority=PriorityTier.BACKGROUND,
                ),
            }
        )
        assert "council_pipeline" in config.session_profiles
        assert "embedding_pipeline" in config.session_profiles
        assert config.session_profiles["council_pipeline"].default_priority == PriorityTier.INTERACTIVE
        assert config.session_profiles["embedding_pipeline"].default_priority == PriorityTier.BACKGROUND

    def test_profiles_parsed_from_yaml_style_dict(self) -> None:
        """Simulate what YAML parsing produces."""
        raw = {
            "session_profiles": {
                "test_profile": {
                    "model_sequence": ["model_a", "model_b"],
                    "default_priority": "pipeline",
                    "description": "Test",
                }
            }
        }
        config = BrokerConfig(**raw)
        profile = config.session_profiles["test_profile"]
        assert profile.default_priority == PriorityTier.PIPELINE
        assert profile.model_sequence == ["model_a", "model_b"]


# ---------------------------------------------------------------------------
# Endpoint validation (manual, not via TestClient)
# ---------------------------------------------------------------------------


class TestBrokerIntentEndpointValidation:
    """Verify /broker/intent endpoint contract without running the full app."""

    def test_intent_request_validation_with_profile(self) -> None:
        """IntentDeclaration should accept profile-based requests."""
        intent = IntentDeclaration(
            profile="council_pipeline",
            client_id="test",
        )
        assert intent.profile == "council_pipeline"
        assert intent.model_sequence is None

    def test_intent_request_validation_with_ad_hoc(self) -> None:
        """IntentDeclaration should accept ad-hoc sequences."""
        intent = IntentDeclaration(
            model_sequence=["model_x", "model_y"],
            estimated_requests=5,
            client_id="custom",
        )
        assert intent.model_sequence == ["model_x", "model_y"]
        assert intent.estimated_requests == 5
        assert intent.profile is None

    def test_intent_response_includes_all_fields(self) -> None:
        """IntentResponse should include all required fields."""
        resp = IntentResponse(
            intent_id="abc123",
            resolved_priority="interactive",
            model_sequence=["a", "b"],
            estimated_requests=10,
            status="registered",
        )
        assert resp.intent_id == "abc123"
        assert resp.resolved_priority == "interactive"
        assert resp.status == "registered"
        data = resp.model_dump()
        assert "intent_id" in data
        assert "resolved_priority" in data
        assert "model_sequence" in data
        assert "estimated_requests" in data
        assert "status" in data


# ---------------------------------------------------------------------------
# Priority mapping validation
# ---------------------------------------------------------------------------


class TestPriorityMapping:
    """Verify that session profiles map to the correct priority tiers."""

    def test_council_maps_to_interactive(self) -> None:
        profile = SessionProfile(
            model_sequence=["a", "b", "c", "d"],
            default_priority=PriorityTier.INTERACTIVE,
        )
        assert profile.default_priority.value == "interactive"
        assert profile.default_priority.base_priority(
            BrokerConfig().priorities
        ) == 100.0

    def test_extraction_maps_to_pipeline(self) -> None:
        profile = SessionProfile(
            model_sequence=["nuextract"],
            default_priority=PriorityTier.PIPELINE,
        )
        assert profile.default_priority.value == "pipeline"
        assert profile.default_priority.base_priority(
            BrokerConfig().priorities
        ) == 25.0

    def test_embedding_maps_to_background(self) -> None:
        profile = SessionProfile(
            model_sequence=["nomic-embed-text"],
            default_priority=PriorityTier.BACKGROUND,
        )
        assert profile.default_priority.value == "background"
        assert profile.default_priority.base_priority(
            BrokerConfig().priorities
        ) == 10.0


# ---------------------------------------------------------------------------
# Intent-based priority resolution in proxy (S11)
# ---------------------------------------------------------------------------


class TestIntentPriorityResolution:
    """Verify that intent declarations influence proxy priority detection."""

    def _make_proxy_with_intent_lookup(self, lookup_fn):
        """Create an OllamaProxy with an intent lookup function."""
        config = BrokerConfig()
        return OllamaProxy(config, intent_lookup_fn=lookup_fn)

    def _make_request(self, headers=None):
        """Create a mock Request object with the given headers."""
        req = MagicMock()
        req.headers = headers or {}
        return req

    def test_no_intent_header_uses_default(self) -> None:
        """Without X-Broker-Intent, priority defaults to AGENT."""
        proxy = self._make_proxy_with_intent_lookup(lambda _: None)
        req = self._make_request({"user-agent": "python-httpx"})
        tier = proxy._detect_priority(req)
        assert tier == PriorityTier.AGENT

    def test_intent_header_resolves_priority(self) -> None:
        """X-Broker-Intent header resolves to the intent's priority tier."""
        def lookup(intent_id):
            if intent_id == "test-intent-123":
                return (PriorityTier.INTERACTIVE, ["qwen3:8b", "phi4:14b"])
            return None

        proxy = self._make_proxy_with_intent_lookup(lookup)
        req = self._make_request({
            "user-agent": "python-httpx",
            "x-broker-intent": "test-intent-123",
        })
        tier = proxy._detect_priority(req)
        assert tier == PriorityTier.INTERACTIVE

    def test_unknown_intent_falls_through(self) -> None:
        """Unknown intent ID falls through to default priority."""
        proxy = self._make_proxy_with_intent_lookup(lambda _: None)
        req = self._make_request({
            "user-agent": "python-httpx",
            "x-broker-intent": "nonexistent",
        })
        tier = proxy._detect_priority(req)
        assert tier == PriorityTier.AGENT

    def test_explicit_priority_header_overrides_intent(self) -> None:
        """X-Broker-Priority takes precedence over X-Broker-Intent."""
        def lookup(intent_id):
            return (PriorityTier.BACKGROUND, ["model_a"])

        proxy = self._make_proxy_with_intent_lookup(lookup)
        req = self._make_request({
            "x-broker-priority": "interactive",
            "x-broker-intent": "some-intent",
        })
        tier = proxy._detect_priority(req)
        assert tier == PriorityTier.INTERACTIVE

    def test_no_lookup_fn_ignores_intent_header(self) -> None:
        """Without intent_lookup_fn, X-Broker-Intent is ignored."""
        proxy = OllamaProxy(BrokerConfig())
        req = self._make_request({
            "user-agent": "python-httpx",
            "x-broker-intent": "some-intent",
        })
        tier = proxy._detect_priority(req)
        assert tier == PriorityTier.AGENT

    def test_pipeline_intent_gives_pipeline_priority(self) -> None:
        """Pipeline intent resolves to PIPELINE priority tier."""
        def lookup(intent_id):
            return (PriorityTier.PIPELINE, ["nuextract", "qwen3:8b"])

        proxy = self._make_proxy_with_intent_lookup(lookup)
        req = self._make_request({"x-broker-intent": "pipeline-1"})
        tier = proxy._detect_priority(req)
        assert tier == PriorityTier.PIPELINE


# ---------------------------------------------------------------------------
# Intent lifecycle: declare -> active -> complete/delete (S11)
# ---------------------------------------------------------------------------


class TestIntentLifecycle:
    """Test intent lifecycle management via _active_intents and _resolved_intents."""

    def test_declare_stores_intent_and_resolved_data(self) -> None:
        """Declaring an intent stores both the declaration and resolved metadata."""
        from bastion.server import _active_intents, _resolved_intents
        # Clean state
        _active_intents.clear()
        _resolved_intents.clear()

        intent = IntentDeclaration(
            model_sequence=["model_a", "model_b"],
            client_id="test",
        )
        _active_intents[intent.intent_id] = intent
        _resolved_intents[intent.intent_id] = (PriorityTier.AGENT, ["model_a", "model_b"])

        assert intent.intent_id in _active_intents
        assert intent.intent_id in _resolved_intents
        tier, seq = _resolved_intents[intent.intent_id]
        assert tier == PriorityTier.AGENT
        assert seq == ["model_a", "model_b"]

        # Clean up
        _active_intents.clear()
        _resolved_intents.clear()

    def test_complete_removes_intent(self) -> None:
        """Completing an intent removes it from both dicts."""
        from bastion.server import _active_intents, _resolved_intents
        _active_intents.clear()
        _resolved_intents.clear()

        intent = IntentDeclaration(model_sequence=["a"])
        _active_intents[intent.intent_id] = intent
        _resolved_intents[intent.intent_id] = (PriorityTier.AGENT, ["a"])

        # Complete it
        del _active_intents[intent.intent_id]
        _resolved_intents.pop(intent.intent_id, None)

        assert intent.intent_id not in _active_intents
        assert intent.intent_id not in _resolved_intents

    def test_lookup_returns_none_after_completion(self) -> None:
        """After completing an intent, _lookup_intent returns None."""
        from bastion.server import _active_intents, _resolved_intents, _lookup_intent
        _active_intents.clear()
        _resolved_intents.clear()

        intent = IntentDeclaration(model_sequence=["x"])
        _active_intents[intent.intent_id] = intent
        _resolved_intents[intent.intent_id] = (PriorityTier.INTERACTIVE, ["x"])

        # Before completion, lookup works
        result = _lookup_intent(intent.intent_id)
        assert result is not None
        assert result[0] == PriorityTier.INTERACTIVE

        # Complete
        del _active_intents[intent.intent_id]
        _resolved_intents.pop(intent.intent_id, None)

        # After completion, lookup returns None
        result = _lookup_intent(intent.intent_id)
        assert result is None

    def test_intent_influences_subsequent_requests(self) -> None:
        """End-to-end: declared intent -> lookup -> proxy uses resolved priority."""
        from bastion.server import _resolved_intents, _lookup_intent
        _resolved_intents.clear()

        intent_id = "test-e2e-intent"
        _resolved_intents[intent_id] = (PriorityTier.INTERACTIVE, ["model_a"])

        proxy = OllamaProxy(BrokerConfig(), intent_lookup_fn=_lookup_intent)
        req = MagicMock()
        req.headers = {"x-broker-intent": intent_id}

        tier = proxy._detect_priority(req)
        assert tier == PriorityTier.INTERACTIVE

        # Clean up
        _resolved_intents.clear()
