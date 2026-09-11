"""The pluggable model backends."""

from __future__ import annotations

import json

import pytest

import providers
from llm_engine import CLASSIFICATION_SCHEMA, SYSTEM_PROMPT, LLMEngine
from models import Category
from providers import (
    AnthropicProvider,
    Completion,
    GeminiProvider,
    HttpSession,
    ModelChoice,
    OllamaProvider,
    OpenAIProvider,
    ProviderAuthError,
    ProviderError,
    ProviderRefusal,
    build_provider,
    provider_class,
)

VERDICT = {
    "summary": "A recruiter invited you to interview. Book a slot this week.",
    "is_job_related": True,
    "category": "INTERVIEW",
    "other_category": "NOT_APPLICABLE",
    "confidence_score": 0.97,
    "reasoning": "Calendly link plus an explicit invitation.",
}


class FakeSession(HttpSession):
    """Records requests and replays canned JSON responses."""

    def __init__(self, responses=None, handler=None):
        super().__init__(timeout=1.0)
        self.requests = []
        self._responses = list(responses or [])
        self._handler = handler
        self.closed = False

    def post_json(self, url, payload, headers=None, connect_timeout=None):
        self.requests.append({"url": url, "payload": payload,
                              "headers": headers or {},
                              "connect_timeout": connect_timeout})
        if self._handler is not None:
            return self._handler(url, payload, headers or {})
        if not self._responses:
            raise AssertionError("no scripted response left")
        result = self._responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def get_json(self, url, headers=None):
        self.requests.append({"url": url, "get": True})
        return self._responses.pop(0) if self._responses else {}

    def close(self):
        self.closed = True


# ==========================================================================
# Registry
# ==========================================================================
class TestRegistry:
    @pytest.mark.parametrize("name", ["anthropic", "gemini", "openai", "ollama"])
    def test_every_backend_is_registered(self, name):
        assert provider_class(name).name == name

    def test_an_unknown_name_falls_back_to_the_offline_engine(self):
        from providers import RulesProvider

        assert provider_class("skynet") is RulesProvider
        assert provider_class("") is RulesProvider

    def test_a_fresh_install_needs_no_credentials(self):
        assert providers.DEFAULT_PROVIDER == "rules"
        assert provider_class(providers.DEFAULT_PROVIDER).needs_api_key is False

    def test_choices_are_complete(self):
        for name, label, blurb in providers.provider_choices():
            assert name and label and blurb

    def test_the_offline_backends_are_the_expected_two(self):
        local = {c.name for c in providers.PROVIDERS if c.on_device}
        assert local == {"ollama", "rules"}

    def test_the_offline_engine_is_offered_first(self):
        """The one that needs no setup should be the first thing you see."""
        assert providers.PROVIDERS[0].name == "rules"

    def test_the_rule_set_needs_nothing_at_all(self):
        spec = provider_class("rules")
        assert spec.needs_api_key is False
        assert spec.on_device is True
        assert providers.FALLBACK_PROVIDER == "rules"

    def test_the_on_device_backend_needs_no_key(self):
        assert OllamaProvider.needs_api_key is False

    def test_the_gemini_default_is_an_auto_updating_alias(self):
        """Google retires pinned model ids for new users without warning."""
        assert GeminiProvider.default_model.endswith("-latest")

    def test_default_models_are_the_cheap_ones(self):
        """A routine scan should not reach for the most expensive model."""
        assert AnthropicProvider.default_model == "claude-haiku-4-5"
        assert GeminiProvider.default_model == "gemini-flash-lite-latest"
        assert OpenAIProvider.default_model == "gpt-4o-mini"

    def test_build_provider_applies_the_default_model(self):
        assert build_provider("gemini", api_key="k").model == "gemini-flash-lite-latest"

    def test_build_provider_respects_an_explicit_model(self):
        assert build_provider("ollama", model="mistral:7b").model == "mistral:7b"


# ==========================================================================
# Shared behaviour
# ==========================================================================
class TestJsonRecovery:
    @pytest.mark.parametrize(
        "raw",
        [
            '{"a": 1}',
            '```json\n{"a": 1}\n```',
            '```\n{"a": 1}\n```',
            'Here is the result:\n{"a": 1}',
            '{"a": 1}\nHope that helps!',
        ],
    )
    def test_json_is_recovered_from_chatty_output(self, raw):
        """Small local models wrap JSON in fences however firmly you ask them not to."""
        assert json.loads(AnthropicProvider._extract_json(raw)) == {"a": 1}

    def test_unrecoverable_text_is_returned_as_is(self):
        assert AnthropicProvider._extract_json("no json here") == "no json here"


class TestKeyRequirement:
    @pytest.mark.parametrize("name", ["anthropic", "gemini", "openai"])
    def test_a_missing_key_is_a_clear_error(self, name):
        provider = build_provider(name, api_key="")
        with pytest.raises(ProviderAuthError, match="Keychain"):
            provider._require_key()

    def test_the_local_backend_never_asks_for_one(self):
        assert build_provider("ollama")._require_key() == ""


# ==========================================================================
# Gemini
# ==========================================================================
class TestGemini:
    def provider(self, session):
        return GeminiProvider(api_key="AIza-test", session=session)

    def test_a_successful_call(self):
        session = FakeSession([{
            "candidates": [{
                "content": {"parts": [{"text": json.dumps(VERDICT)}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 120},
        }])
        completion = self.provider(session).complete(SYSTEM_PROMPT, "prompt", CLASSIFICATION_SCHEMA)
        assert json.loads(completion.text)["category"] == "INTERVIEW"
        assert (completion.input_tokens, completion.output_tokens) == (900, 120)

    def test_the_key_goes_in_the_header_not_the_url(self):
        """A key in the query string leaks into logs and proxies."""
        session = FakeSession([{"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}])
        self.provider(session).complete(SYSTEM_PROMPT, "prompt", CLASSIFICATION_SCHEMA)
        request = session.requests[0]
        assert request["headers"]["x-goog-api-key"] == "AIza-test"
        assert "AIza-test" not in request["url"]

    def test_the_model_is_in_the_path(self):
        session = FakeSession([{"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}])
        GeminiProvider(api_key="k", model="gemini-2.5-pro", session=session).complete(
            SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA
        )
        assert "gemini-2.5-pro:generateContent" in session.requests[0]["url"]

    def test_the_schema_is_translated_to_geminis_dialect(self):
        session = FakeSession([{"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}])
        self.provider(session).complete(SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA)
        schema = session.requests[0]["payload"]["generationConfig"]["responseSchema"]
        assert schema["type"] == "OBJECT"
        assert "additionalProperties" not in schema          # Gemini rejects it
        assert schema["properties"]["category"]["type"] == "STRING"
        assert schema["properties"]["category"]["enum"] == [c.value for c in Category]

    def test_a_blocked_prompt_becomes_a_refusal(self):
        session = FakeSession([{"promptFeedback": {"blockReason": "SAFETY"}}])
        with pytest.raises(ProviderRefusal):
            self.provider(session).complete(SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA)

    def test_a_safety_finish_becomes_a_refusal(self):
        session = FakeSession([{"candidates": [{"finishReason": "SAFETY", "content": {}}]}])
        with pytest.raises(ProviderRefusal):
            self.provider(session).complete(SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA)

    def test_truncation_is_reported(self):
        session = FakeSession([{
            "candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "{"}]}}]
        }])
        assert self.provider(session).complete(
            SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA
        ).stop_reason == "max_tokens"

    def test_no_candidates_is_an_error(self):
        with pytest.raises(ProviderError):
            self.provider(FakeSession([{}])).complete(SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA)


# ==========================================================================
# OpenAI-compatible
# ==========================================================================
class TestOpenAICompatible:
    def response(self, content=None, finish="stop", **extra):
        message = {"content": json.dumps(content or VERDICT)}
        message.update(extra)
        return {
            "choices": [{"message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 800, "completion_tokens": 90},
        }

    def test_a_successful_call(self):
        session = FakeSession([self.response()])
        completion = OpenAIProvider(api_key="sk-x", session=session).complete(
            SYSTEM_PROMPT, "prompt", CLASSIFICATION_SCHEMA
        )
        assert json.loads(completion.text)["category"] == "INTERVIEW"
        assert completion.input_tokens == 800

    def test_the_key_is_sent_as_a_bearer_token(self):
        session = FakeSession([self.response()])
        OpenAIProvider(api_key="sk-x", session=session).complete(
            SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA
        )
        assert session.requests[0]["headers"]["Authorization"] == "Bearer sk-x"

    def test_strict_json_schema_is_requested(self):
        session = FakeSession([self.response()])
        OpenAIProvider(api_key="sk-x", session=session).complete(
            SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA
        )
        fmt = session.requests[0]["payload"]["response_format"]
        assert fmt["type"] == "json_schema"
        assert fmt["json_schema"]["strict"] is True

    def test_a_gateway_that_rejects_json_schema_is_retried_in_json_mode(self):
        """LM Studio and older gateways accept json_object but not json_schema."""
        calls = []

        def handler(url, payload, headers):
            calls.append(payload["response_format"]["type"])
            if payload["response_format"]["type"] == "json_schema":
                raise ProviderError("host returned HTTP 400: unsupported response_format")
            return self.response()

        provider = OpenAIProvider(api_key="sk-x", session=FakeSession(handler=handler))
        completion = provider.complete(SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA)
        assert calls == ["json_schema", "json_object"]
        assert json.loads(completion.text)["category"] == "INTERVIEW"
        assert any("plain JSON" in note for note in completion.notes)

    def test_a_server_error_is_not_retried_as_json_mode(self):
        def handler(url, payload, headers):
            raise ProviderError("host returned HTTP 500: boom")

        with pytest.raises(ProviderError):
            OpenAIProvider(api_key="sk-x", session=FakeSession(handler=handler)).complete(
                SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA
            )

    def test_a_custom_endpoint_is_used(self):
        session = FakeSession([self.response()])
        OpenAIProvider(
            api_key="", base_url="http://localhost:1234/v1", session=session
        ).complete(SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA)
        assert session.requests[0]["url"] == "http://localhost:1234/v1/chat/completions"

    def test_a_local_endpoint_needs_no_key(self):
        session = FakeSession([self.response()])
        OpenAIProvider(api_key="", base_url="http://localhost:1234/v1", session=session).complete(
            SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA
        )
        assert "Authorization" not in session.requests[0]["headers"]

    def test_a_remote_endpoint_still_needs_a_key(self):
        with pytest.raises(ProviderAuthError):
            OpenAIProvider(api_key="", session=FakeSession([])).complete(
                SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA
            )

    def test_a_refusal_field_becomes_a_refusal(self):
        session = FakeSession([self.response(refusal="I cannot help with that")])
        with pytest.raises(ProviderRefusal):
            OpenAIProvider(api_key="sk-x", session=session).complete(
                SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA
            )

    def test_a_content_filter_becomes_a_refusal(self):
        session = FakeSession([self.response(finish="content_filter")])
        with pytest.raises(ProviderRefusal):
            OpenAIProvider(api_key="sk-x", session=session).complete(
                SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA
            )

    def test_truncation_is_reported(self):
        session = FakeSession([self.response(finish="length")])
        assert OpenAIProvider(api_key="sk-x", session=session).complete(
            SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA
        ).stop_reason == "max_tokens"


# ==========================================================================
# Ollama (on device)
# ==========================================================================
class TestOllama:
    def test_a_successful_call(self):
        session = FakeSession([{
            "message": {"content": json.dumps(VERDICT)},
            "done": True, "prompt_eval_count": 700, "eval_count": 80,
        }])
        completion = OllamaProvider(session=session).complete(
            SYSTEM_PROMPT, "prompt", CLASSIFICATION_SCHEMA
        )
        assert json.loads(completion.text)["category"] == "INTERVIEW"
        assert (completion.input_tokens, completion.output_tokens) == (700, 80)

    def test_it_talks_to_the_loopback_address_by_default(self):
        """127.0.0.1 rather than localhost.

        On macOS "localhost" resolves to ::1 as well, Ollama listens on IPv4
        only, and the wasted attempt is a pause on every single request.
        """
        session = FakeSession([{"message": {"content": "{}"}, "done": True}])
        OllamaProvider(session=session).complete(SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA)
        assert session.requests[0]["url"] == "http://127.0.0.1:11434/api/chat"

    def test_reaching_it_has_a_shorter_leash_than_answering(self):
        """They used to share one timeout, and whichever number was chosen
        was wrong for one of them: an on-device scan gave the model four
        seconds to answer and then reported Ollama was not installed."""
        session = FakeSession([{"message": {"content": "{}"}, "done": True}])
        provider = OllamaProvider(session=session)
        provider.complete(SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA)
        assert session.requests[0]["connect_timeout"] == provider.CONNECT_TIMEOUT
        assert provider.timeout > provider.CONNECT_TIMEOUT * 10

    def test_a_slow_answer_does_not_read_as_a_missing_install(self):
        """"Install it from ollama.com" sent people to reinstall software
        that was working perfectly and merely thinking."""
        def slow(_url, _payload, _headers):
            raise ProviderError("127.0.0.1 timed out after 600s while answering.")

        provider = OllamaProvider(session=FakeSession(handler=slow))
        with pytest.raises(ProviderError) as caught:
            provider.complete(SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA)
        message = str(caught.value)
        assert "did not answer in time" in message
        assert "Install it" not in message

    def test_the_schema_is_passed_straight_through(self):
        session = FakeSession([{"message": {"content": "{}"}, "done": True}])
        OllamaProvider(session=session).complete(SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA)
        assert session.requests[0]["payload"]["format"] == CLASSIFICATION_SCHEMA

    def test_temperature_is_pinned_to_zero(self):
        session = FakeSession([{"message": {"content": "{}"}, "done": True}])
        OllamaProvider(session=session).complete(SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA)
        assert session.requests[0]["payload"]["options"]["temperature"] == 0

    def test_a_missing_server_explains_how_to_fix_it(self):
        def handler(url, payload, headers):
            raise ProviderError("Could not reach localhost: connection refused")

        with pytest.raises(ProviderError, match="ollama.com"):
            OllamaProvider(session=FakeSession(handler=handler)).complete(
                SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA
            )

    def test_a_custom_host_is_honoured(self):
        session = FakeSession([{"message": {"content": "{}"}, "done": True}])
        OllamaProvider(base_url="http://192.168.1.9:11434", session=session).complete(
            SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA
        )
        assert session.requests[0]["url"].startswith("http://192.168.1.9:11434")

    def test_installed_models_can_be_listed(self):
        session = FakeSession([{"models": [{"name": "llama3.2:3b"}, {"name": "qwen2.5:7b"}]}])
        assert OllamaProvider(session=session).installed_models() == ["llama3.2:3b", "qwen2.5:7b"]


# ==========================================================================
# The engine, driven through each backend
# ==========================================================================
class TestEngineWithProviders:
    def test_the_engine_selects_the_named_backend(self):
        engine = LLMEngine(api_key="k", provider="gemini")
        assert engine.provider.name == "gemini"
        assert engine.model == "gemini-flash-lite-latest"

    def test_a_local_backend_reports_zero_cost(self):
        engine = LLMEngine(provider="ollama")
        assert engine.usage.on_device is True
        assert engine.usage.estimated_cost_usd == 0.0

    def test_classification_works_through_a_non_claude_backend(self):
        engine = LLMEngine(api_key="AIza", provider="gemini")
        engine.provider._session = FakeSession([{
            "candidates": [{
                "content": {"parts": [{"text": json.dumps(VERDICT)}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 120},
        }])
        from models import EmailMessage

        result = engine.classify(EmailMessage(uid="1", subject="Interview"))
        assert result.category is Category.INTERVIEW
        assert result.model == "gemini-flash-lite-latest"
        assert engine.usage.input_tokens == 900

    def test_the_domain_guards_still_apply_to_every_backend(self):
        engine = LLMEngine(api_key="AIza", provider="gemini")
        payload = dict(VERDICT, is_job_related=False, category="INTERVIEW", other_category="FINANCE")
        engine.provider._session = FakeSession([{
            "candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]},
                            "finishReason": "STOP"}],
        }])
        from models import EmailMessage

        result = engine.classify(EmailMessage(uid="1"))
        assert result.category is Category.UNCLASSIFIED_OTHER
        assert result.adjustments

    def test_a_refusal_from_any_backend_routes_to_review(self):
        engine = LLMEngine(api_key="AIza", provider="gemini", fallback_to_rules=False)
        engine.provider._session = FakeSession([{"promptFeedback": {"blockReason": "SAFETY"}}])
        from models import EmailMessage

        result = engine.classify(EmailMessage(uid="1"))
        assert not result.ok
        assert "declined" in result.error

    def test_closing_the_engine_closes_the_session(self):
        engine = LLMEngine(api_key="k", provider="ollama")
        session = FakeSession([])
        engine.provider._session = session
        engine.close()
        assert session.closed is True

    def test_switching_backend_switches_the_rate_card(self):
        opus = LLMEngine(api_key="k", provider="anthropic", model="claude-opus-5")
        flash = LLMEngine(api_key="k", provider="gemini")
        for engine in (opus, flash):
            engine.usage.add(1_000_000, 100_000)
        assert flash.usage.estimated_cost_usd < opus.usage.estimated_cost_usd


# ==========================================================================
# The HTTP transport, against a real local server
# ==========================================================================
class TestHttpSession:
    """Exercised against a throwaway localhost server rather than mocks,
    this is the transport for three of the four backends."""

    @pytest.fixture
    def server(self):
        import http.server
        import threading

        state = {"status": 200, "body": '{"ok": true}', "requests": [], "delay": 0.0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence the default stderr logging
                pass

            def _respond(self):
                import time

                if state["delay"]:
                    time.sleep(state["delay"])
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                state["requests"].append({
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": raw.decode("utf-8") if raw else "",
                })
                payload = state["body"].encode("utf-8")
                self.send_response(state["status"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_POST = _respond
            do_GET = _respond

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        state["url"] = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            yield state
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_a_successful_post(self, server):
        session = HttpSession(timeout=5)
        assert session.post_json(f"{server['url']}/v1/chat", {"hello": "world"}) == {"ok": True}
        request = server["requests"][0]
        assert request["path"] == "/v1/chat"
        assert json.loads(request["body"]) == {"hello": "world"}
        assert request["headers"]["Content-Type"] == "application/json"

    def test_custom_headers_are_sent(self, server):
        HttpSession(timeout=5).post_json(server["url"], {}, {"x-goog-api-key": "AIza"})
        assert server["requests"][0]["headers"]["x-goog-api-key"] == "AIza"

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_statuses_raise_an_auth_error(self, server, status):
        server["status"] = status
        with pytest.raises(ProviderAuthError):
            HttpSession(timeout=5).post_json(server["url"], {})

    @pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
    def test_other_error_statuses_carry_the_code(self, server, status):
        server["status"] = status
        with pytest.raises(ProviderError) as excinfo:
            HttpSession(timeout=5).post_json(server["url"], {})
        assert getattr(excinfo.value, "status_code", None) == status
        assert str(status) in str(excinfo.value)

    def test_a_non_json_body_is_reported_clearly(self, server):
        server["body"] = "<html>gateway error</html>"
        with pytest.raises(ProviderError, match="not JSON"):
            HttpSession(timeout=5).post_json(server["url"], {})

    def test_an_unreachable_host_is_reported(self):
        with pytest.raises(ProviderError, match="Could not reach"):
            HttpSession(timeout=2).post_json("http://127.0.0.1:1/x", {})

    def test_a_timeout_is_reported(self, server):
        server["delay"] = 1.5
        with pytest.raises(ProviderError, match="timed out"):
            HttpSession(timeout=0.3).post_json(server["url"], {})

    def test_an_unsupported_scheme_is_rejected(self):
        with pytest.raises(ProviderError, match="scheme"):
            HttpSession().post_json("ftp://example.com/x", {})

    def test_a_closed_session_refuses_new_requests(self, server):
        session = HttpSession(timeout=5)
        session.close()
        with pytest.raises(ProviderError, match="closed"):
            session.post_json(server["url"], {})

    def test_closing_mid_flight_aborts_the_request(self, server):
        """This is what makes Stop All feel instant on the HTTP backends."""
        import threading
        import time

        server["delay"] = 5.0
        session = HttpSession(timeout=30)
        errors = []

        def call():
            try:
                session.post_json(server["url"], {})
            except ProviderError as exc:
                errors.append(exc)

        worker = threading.Thread(target=call, daemon=True)
        started = time.monotonic()
        worker.start()
        time.sleep(0.4)          # let the request reach the server
        session.close()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert errors, "the in-flight request should have failed, not completed"
        assert time.monotonic() - started < 4.0   # well before the 5s server delay

    def test_get_json(self, server):
        server["body"] = '{"models": []}'
        assert HttpSession(timeout=5).get_json(f"{server['url']}/api/tags") == {"models": []}

    def test_get_json_reports_errors(self, server):
        server["status"] = 500
        with pytest.raises(ProviderError):
            HttpSession(timeout=5).get_json(server["url"])

    def test_connections_are_not_leaked(self, server):
        session = HttpSession(timeout=5)
        for _ in range(5):
            session.post_json(server["url"], {})
        assert session._live == []


# ==========================================================================
# Live model discovery, a hard-coded catalogue goes stale
# ==========================================================================
class TestModelDiscovery:
    def test_which_backends_can_list_their_models(self):
        listing = {c.name for c in providers.PROVIDERS if c.can_list_models}
        assert listing == {"gemini", "openai", "ollama"}
        assert provider_class("anthropic").can_list_models is False
        assert provider_class("rules").can_list_models is False

    def test_a_backend_that_cannot_list_says_so(self):
        with pytest.raises(ProviderError, match="cannot list"):
            build_provider("anthropic", api_key="k").list_models()

    def test_gemini_lists_text_models_only(self):
        """Image, TTS and robotics variants are not useful for triage."""
        session = FakeSession([{
            "models": [
                {"name": "models/gemini-3.5-flash-lite",
                 "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-flash-latest",
                 "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3-pro-image",
                 "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-2.5-flash-preview-tts",
                 "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.5-transcribe",
                 "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-embedding", "supportedGenerationMethods": ["embedContent"]},
                {"name": "models/other-model", "supportedGenerationMethods": ["generateContent"]},
            ]
        }])
        found = GeminiProvider(api_key="AIza", session=session).list_models()
        assert found == ["gemini-3.5-flash-lite", "gemini-flash-latest"]

    def test_gemini_listing_sends_the_key_in_a_header(self):
        session = FakeSession([{"models": []}])
        GeminiProvider(api_key="AIza-secret", session=session).list_models()
        assert "AIza-secret" not in session.requests[0]["url"]

    def test_openai_lists_model_ids(self):
        session = FakeSession([{"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4.1"}]}])
        assert OpenAIProvider(api_key="sk-x", session=session).list_models() == [
            "gpt-4.1", "gpt-4o-mini"
        ]

    def test_ollama_lists_what_has_been_pulled(self):
        session = FakeSession([{"models": [{"name": "qwen2.5:7b"}, {"name": "llama3.2:3b"}]}])
        provider = OllamaProvider(session=session)
        assert provider.list_models() == ["llama3.2:3b", "qwen2.5:7b"]
        assert provider.installed_models == provider.list_models

    def test_unknown_pricing_shows_no_price_rather_than_a_wrong_one(self):
        """Rates for models released after this build are simply not known."""
        provider = build_provider("gemini", api_key="k", model="gemini-flash-lite-latest")
        assert provider.rate() == (0.0, 0.0)


# ==========================================================================
# A backend that is not there must fail fast, once
# ==========================================================================
class TestUnreachableBackend:
    """Ollama absent used to cost 6.7 s per message: a connect timeout plus the
    full retry ladder, repeated for every email in the scan."""

    def refusing_session(self):
        def handler(url, payload, headers):
            raise ProviderError("Could not reach localhost: connection refused")

        return FakeSession(handler=handler)

    def test_the_first_failure_is_marked_permanent(self):
        provider = OllamaProvider(session=self.refusing_session())
        with pytest.raises(ProviderError) as excinfo:
            provider.complete(SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA)
        assert excinfo.value.permanent is True
        assert "ollama.com" in str(excinfo.value)

    def test_later_calls_fail_immediately(self):
        session = self.refusing_session()
        provider = OllamaProvider(session=session)
        with pytest.raises(ProviderError):
            provider.complete(SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA)
        calls = len(session.requests)
        for _ in range(5):
            with pytest.raises(ProviderError):
                provider.complete(SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA)
        assert len(session.requests) == calls, "it kept trying a server it knows is absent"

    def test_a_permanent_error_is_not_retried(self):
        from llm_engine import _is_retryable

        assert _is_retryable(ProviderError("gone", permanent=True)) is False
        assert _is_retryable(ProviderError("Could not reach host")) is True

    def test_a_whole_scan_falls_back_promptly(self):
        import time

        from llm_engine import LLMEngine
        from models import EmailMessage

        engine = LLMEngine(provider="ollama", fallback_to_rules=True, sleep=lambda _: None)
        engine.provider._session = self.refusing_session()
        messages = [
            EmailMessage(uid=str(i), subject="Update on your application",
                         body_text="We have decided to move forward with other candidates.")
            for i in range(20)
        ]
        began = time.monotonic()
        results = engine.classify_many(messages)
        assert time.monotonic() - began < 3.0
        assert all(r.ok for r in results)
        assert engine.fallback_count == 20


# ==========================================================================
# Security properties
# ==========================================================================
class TestTransportSafety:
    """Every request carries the text of somebody's email."""

    @pytest.mark.parametrize("url", [
        "http://api.openai.com/v1/chat/completions",
        "http://example.com/v1",
        "http://8.8.8.8/api",
    ])
    def test_plain_http_to_a_remote_host_is_refused(self, url):
        with pytest.raises(ProviderError, match="plain HTTP"):
            HttpSession(timeout=1).post_json(url, {"body": "private"})

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "192.168.1.9", "ollama.local"])
    def test_plain_http_on_this_machine_or_lan_is_allowed(self, host):
        """It never leaves the machine, so there is nothing to protect."""
        with pytest.raises(ProviderError) as excinfo:
            HttpSession(timeout=1).post_json(f"http://{host}:1/api", {"x": 1})
        assert "plain HTTP" not in str(excinfo.value)

    def test_https_is_always_allowed(self):
        with pytest.raises(ProviderError) as excinfo:
            HttpSession(timeout=1).post_json("https://127.0.0.1:1/x", {"x": 1})
        assert "plain HTTP" not in str(excinfo.value)

    def test_a_server_error_does_not_echo_the_whole_response(self):
        from providers import _summarise

        assert len(_summarise("x" * 5000)) <= 245
        assert _summarise("line one\nline two") == "line one line two"

    def test_an_api_key_never_appears_in_an_error(self):
        session = FakeSession(handler=lambda url, payload, headers: (_ for _ in ()).throw(
            ProviderError("host returned HTTP 500: upstream failure")
        ))
        provider = OpenAIProvider(api_key="sk-super-secret-value", session=session)
        with pytest.raises(ProviderError) as excinfo:
            provider.complete(SYSTEM_PROMPT, "p", CLASSIFICATION_SCHEMA)
        assert "sk-super-secret-value" not in str(excinfo.value)
