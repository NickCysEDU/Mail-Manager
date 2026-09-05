"""Pluggable model backends.

The expensive, valuable parts of classification - the prompt, the schema, the
validation guards, the routing rules, the retry policy - are provider
independent. Only the transport differs, so that is all this module supplies.

Four backends ship:

* ``anthropic``  - Claude, via the official SDK. Best accuracy; costs money.
* ``gemini``     - Google AI Studio. Gemini Flash is very cheap and very fast.
* ``openai``     - OpenAI, and anything that speaks its ``/chat/completions``
                   shape: OpenRouter, Groq, Together, LM Studio, vLLM.
* ``ollama``     - a model running **on this Mac**. No API key, no network, no
                   cost, and nothing about your mail leaves the machine.

A note on Chrome's built-in AI (Gemini Nano): it is reachable only from
JavaScript inside a web page (``LanguageModel`` / ``window.ai``). There is no
local endpoint a native application can call, so it cannot be used from here.
Ollama and LM Studio are the equivalent for a desktop app - genuinely on-device,
free, and private - and both are supported below.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import socket
import ssl
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 90.0


def _summarise(text: str, limit: int = 240) -> str:
    """A short, single-line excerpt of a server response for an error message."""
    collapsed = " ".join((text or "").split())
    return collapsed[:limit] + ("..." if len(collapsed) > limit else "")


class ProviderError(RuntimeError):
    """The backend could not be reached, or returned something unusable.

    ``permanent`` marks a failure that retrying cannot fix - a local server
    that is not installed, say. Without it, every message in a scan would sit
    through the full retry ladder for the same known-hopeless reason.
    """

    def __init__(self, *args, permanent: bool = False) -> None:
        super().__init__(*args)
        self.permanent = permanent


class ProviderAuthError(ProviderError):
    """The API key is missing, malformed, or rejected."""


class ProviderRefusal(ProviderError):
    """The model declined to answer (safety stop)."""

    def __init__(self, message: str, category: str = "unspecified") -> None:
        super().__init__(message)
        self.category = category


@dataclass
class Completion:
    """One model response, normalised across backends."""

    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = "end_turn"
    notes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelChoice:
    """One entry in the model dropdown."""

    value: str
    label: str
    note: str = ""


# ==========================================================================
# HTTP plumbing (stdlib only - keeps the .app small and dependency-free)
# ==========================================================================
class HttpSession:
    """A tiny JSON-over-HTTPS client with abortable connections.

    Tracks live connections so :meth:`close` can drop them from another thread,
    which is what makes the app's Stop button feel instant on every backend
    rather than only on the one with a fancy SDK.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = float(timeout)
        self._live: List[http.client.HTTPConnection] = []
        self._lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            live, self._live = list(self._live), []
        for connection in live:
            # shutdown() before close(): closing the descriptor alone does not
            # wake a thread already blocked in recv(), so the request would run
            # to its full timeout and Stop All would appear to hang.
            sock = getattr(connection, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            try:
                connection.close()
            except Exception:  # pragma: no cover - teardown is best effort
                pass

    def post_json(
        self,
        url: str,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        if self._closed:
            raise ProviderError("The connection was closed.")

        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            raise ProviderError(f"Unsupported URL scheme in {url!r}.")
        host = parsed.hostname or ""
        # Plain HTTP is fine to a server on this machine and nowhere else:
        # every request carries the text of an email.
        if parsed.scheme == "http" and not _is_local(host):
            raise ProviderError(
                f"Refusing to send message text to {host} over plain HTTP. "
                "Use https:// for a remote endpoint.",
                permanent=True,
            )
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        body = json.dumps(payload).encode("utf-8")
        request_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Content-Length": str(len(body)),
            "User-Agent": "iCloud-Job-Triage/1.0",
        }
        request_headers.update(headers or {})

        if parsed.scheme == "https":
            connection = http.client.HTTPSConnection(
                host, port, timeout=self.timeout, context=ssl.create_default_context()
            )
        else:
            connection = http.client.HTTPConnection(host, port, timeout=self.timeout)

        with self._lock:
            if self._closed:
                raise ProviderError("The connection was closed.")
            self._live.append(connection)
        try:
            connection.request("POST", path, body=body, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        except (socket.timeout, TimeoutError) as exc:
            raise ProviderError(f"{host} timed out after {self.timeout:.0f}s.") from exc
        except (OSError, http.client.HTTPException) as exc:
            if self._closed:
                raise ProviderError("Cancelled.") from exc
            raise ProviderError(f"Could not reach {host}: {exc}") from exc
        finally:
            with self._lock:
                if connection in self._live:
                    self._live.remove(connection)
            try:
                connection.close()
            except Exception:
                pass

        text = raw.decode("utf-8", "replace")
        if status in (401, 403):
            raise ProviderAuthError(f"{host} rejected the API key (HTTP {status}). {text[:200]}")
        if status >= 400:
            # Only the status and a short excerpt: a full body can echo the
            # request, and the request contains the email.
            error = ProviderError(f"{host} returned HTTP {status}: {_summarise(text)}")
            error.status_code = status  # type: ignore[attr-defined]
            raise error
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"{host} returned a body that is not JSON: {_summarise(text)}"
            ) from exc

    def get_json(self, url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        connection = (
            http.client.HTTPSConnection(host, port, timeout=self.timeout,
                                        context=ssl.create_default_context())
            if parsed.scheme == "https"
            else http.client.HTTPConnection(host, port, timeout=self.timeout)
        )
        try:
            connection.request("GET", path, headers=dict(headers or {}))
            response = connection.getresponse()
            raw = response.read()
            if response.status >= 400:
                raise ProviderError(f"{host} returned HTTP {response.status}.")
            return json.loads(raw.decode("utf-8", "replace") or "{}")
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            raise ProviderError(f"Could not reach {host}: {exc}") from exc
        finally:
            try:
                connection.close()
            except Exception:
                pass


# ==========================================================================
# Base
# ==========================================================================
class Provider:
    """A model backend."""

    #: Stable identifier stored in settings.
    name: str = ""
    #: Shown in the settings dropdown.
    label: str = ""
    #: One line explaining the trade-off.
    blurb: str = ""
    #: Whether an API key is required.
    needs_api_key: bool = True
    #: Where to get a key.
    key_url: str = ""
    #: What a valid key looks like, shown when one is rejected.
    key_hint: str = ""
    #: Whether the endpoint can be overridden.
    supports_base_url: bool = False
    #: Runs on this machine; nothing leaves it.
    on_device: bool = False
    default_model: str = ""
    models: Tuple[ModelChoice, ...] = ()
    #: USD per million tokens (input, output). Zero for local models.
    pricing: Dict[str, Tuple[float, float]] = {}

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
        base_url: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        effort: str = "medium",
        session: Optional[HttpSession] = None,
        **_ignored: Any,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip() or self.default_model
        self.base_url = (base_url or "").strip().rstrip("/")
        self.timeout = timeout
        self.effort = effort
        self._session = session or HttpSession(timeout)
        self.notes: List[str] = []

    # -- interface -------------------------------------------------------
    def complete(
        self,
        system: str,
        prompt: str,
        schema: Dict[str, Any],
        message: Any = None,
    ) -> Completion:
        """Classify one email.

        ``message`` is the structured :class:`~models.EmailMessage`. Model
        backends work from ``prompt`` and ignore it; the local rules engine
        uses it directly, since re-parsing its own rendered prompt would be
        absurd.
        """
        raise NotImplementedError

    def close(self) -> None:
        self._session.close()

    def rate(self) -> Tuple[float, float]:
        return self.pricing.get(self.model, (0.0, 0.0))

    #: Whether :meth:`list_models` can ask the service what it actually serves.
    can_list_models: bool = False

    def list_models(self) -> List[str]:
        """Model ids the service currently offers. Raises if unsupported."""
        raise ProviderError(f"{self.label} cannot list its models.")

    def describe(self) -> str:
        return f"{self.label} · {self.model}"

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)

    # -- helpers ---------------------------------------------------------
    def _require_key(self) -> str:
        if self.needs_api_key and not self.api_key:
            raise ProviderAuthError(
                f"{self.label} needs an API key. Add one in Settings - it is stored "
                "in your macOS Keychain."
            )
        return self.api_key

    @staticmethod
    def _extract_json(text: str) -> str:
        """Pull a JSON object out of a response that may be fenced or padded.

        Small local models frequently wrap JSON in ``` fences or add a sentence
        of preamble even when told not to. Recovering here costs nothing and
        turns a hard failure into a usable result.
        """
        stripped = (text or "").strip()
        if stripped.startswith("```"):
            stripped = stripped.split("```", 2)[1] if stripped.count("```") >= 2 else stripped[3:]
            if stripped.lstrip().lower().startswith("json"):
                stripped = stripped.lstrip()[4:]
            stripped = stripped.strip().rstrip("`").strip()
        # Slice to the outermost braces: this also drops a trailing "Hope that
        # helps!", which small models add as readily as they add a preamble.
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end > start:
            return stripped[start:end + 1]
        return stripped


# ==========================================================================
# Anthropic (official SDK)
# ==========================================================================
class AnthropicProvider(Provider):
    name = "anthropic"
    label = "Claude (Anthropic)"
    blurb = "Highest accuracy. Costs money per email."
    key_url = "https://console.anthropic.com"
    key_hint = "Anthropic keys start with “sk-ant-”."
    default_model = "claude-haiku-4-5"
    models = (
        ModelChoice("claude-haiku-4-5", "Claude Haiku 4.5", "fastest and cheapest - a good default"),
        ModelChoice("claude-sonnet-5", "Claude Sonnet 5", "balanced"),
        ModelChoice("claude-sonnet-4-6", "Claude Sonnet 4.6", "previous generation"),
        ModelChoice("claude-opus-5", "Claude Opus 5", "most accurate"),
        ModelChoice("claude-opus-4-8", "Claude Opus 4.8", "previous generation"),
    )
    pricing = {
        "claude-haiku-4-5": (1.00, 5.00),
        "claude-sonnet-5": (2.00, 10.00),
        "claude-sonnet-4-6": (3.00, 15.00),
        "claude-opus-5": (5.00, 25.00),
        "claude-opus-4-8": (5.00, 25.00),
    }
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, *args, client: Any = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._client = client
        self._lock = threading.Lock()
        self._closed = False
        self._use_beta = True
        self._use_thinking = not self.model.startswith("claude-haiku")
        self._use_effort = not self.model.startswith("claude-haiku")

    def client(self) -> Any:
        if self._closed:
            raise ProviderError("The classifier was closed.")
        if self._client is not None:
            return self._client
        self._require_key()
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise ProviderError("The 'anthropic' package is not installed.") from exc
        with self._lock:
            if self._client is None:
                self._client = anthropic.Anthropic(
                    api_key=self.api_key, timeout=self.timeout, max_retries=0
                )
        return self._client

    def close(self) -> None:
        super().close()
        with self._lock:
            client, self._client, self._closed = self._client, None, True
        if client is None:
            return
        closer = getattr(client, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # pragma: no cover
                pass

    def _kwargs(self, system: str, prompt: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        output_config: Dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self._use_effort:
            output_config["effort"] = self.effort
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": 16000,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": prompt}],
            "output_config": output_config,
        }
        if self._use_thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        return kwargs

    def complete(self, system: str, prompt: str, schema: Dict[str, Any],
                 message: Any = None) -> Completion:
        client = self.client()
        kwargs = self._kwargs(system, prompt, schema)

        if self._use_beta:
            try:
                response = client.beta.messages.create(
                    betas=[self.FALLBACK_BETA], fallbacks="default", **kwargs
                )
                return self._read(response)
            except TypeError as exc:
                self._use_beta = False
                self.note(f"server-side refusal fallbacks unavailable in this SDK ({exc})")
            except Exception as exc:
                if _mentions(exc, ("fallback", "beta")) and _status(exc) in (400, 404):
                    self._use_beta = False
                    self.note("server-side refusal fallbacks were rejected; continuing without")
                else:
                    raise

        try:
            return self._read(client.messages.create(**kwargs))
        except Exception as exc:
            if self._use_thinking and _mentions(exc, ("thinking",)) and _status(exc) in (400, 404):
                self._use_thinking = False
                self.note(f"{self.model} rejected adaptive thinking; disabled")
                return self.complete(system, prompt, schema, message)
            if self._use_effort and _mentions(exc, ("effort",)) and _status(exc) in (400, 404):
                self._use_effort = False
                self.note(f"{self.model} rejected output_config.effort; disabled")
                return self.complete(system, prompt, schema, message)
            raise

    def _read(self, response: Any) -> Completion:
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0) + int(
            getattr(usage, "cache_read_input_tokens", 0) or 0
        )
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        stop_reason = getattr(response, "stop_reason", None) or "end_turn"

        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise ProviderRefusal(
                "Claude declined to classify this message.",
                str(getattr(details, "category", None) or "unspecified"),
            )

        text = ""
        for block in getattr(response, "content", None) or []:
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", "") or ""
                break
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "") or ""
                break
        return Completion(text, input_tokens, output_tokens, stop_reason, tuple(self.notes))


# ==========================================================================
# Google Gemini
# ==========================================================================
class GeminiProvider(Provider):
    name = "gemini"
    label = "Gemini (Google AI Studio)"
    blurb = "Very cheap and very fast. Flash-Lite costs a few cents per thousand emails."
    key_url = "https://aistudio.google.com/apikey"
    key_hint = "Google AI Studio keys start with “AIza”."
    can_list_models = True
    #: An alias rather than a pinned version: Google retires specific model ids
    #: for new users, and a hard-coded list goes stale without warning.
    default_model = "gemini-flash-lite-latest"
    models = (
        ModelChoice("gemini-flash-lite-latest", "Gemini Flash-Lite (latest)",
                    "cheapest, always current - a good default"),
        ModelChoice("gemini-flash-latest", "Gemini Flash (latest)", "balanced, always current"),
        ModelChoice("gemini-pro-latest", "Gemini Pro (latest)", "most accurate, always current"),
        ModelChoice("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite", "pinned version"),
        ModelChoice("gemini-3.5-flash", "Gemini 3.5 Flash", "pinned version"),
        ModelChoice("gemini-2.5-flash", "Gemini 2.5 Flash", "previous generation"),
        ModelChoice("gemini-2.5-pro", "Gemini 2.5 Pro", "previous generation"),
    )
    #: Only rates that are actually known. An unknown rate shows no price
    #: rather than a made-up one.
    pricing = {
        "gemini-2.5-flash": (0.30, 2.50),
        "gemini-2.5-pro": (1.25, 10.00),
    }
    ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"

    def list_models(self) -> List[str]:
        key = self._require_key()
        base = self.base_url or self.ENDPOINT
        data = self._session.get_json(f"{base}?pageSize=200", {"x-goog-api-key": key})
        found = []
        for entry in data.get("models") or []:
            name = str(entry.get("name", "")).replace("models/", "")
            methods = entry.get("supportedGenerationMethods") or []
            if not name.startswith("gemini") or "generateContent" not in methods:
                continue
            # Skip the modality-specific variants; this app sends text.
            if any(tag in name for tag in ("image", "tts", "transcribe", "robotics",
                                           "computer-use", "omni")):
                continue
            found.append(name)
        return sorted(found)

    def complete(self, system: str, prompt: str, schema: Dict[str, Any],
                 message: Any = None) -> Completion:
        key = self._require_key()
        url = f"{self.base_url or self.ENDPOINT}/{self.model}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json",
                "responseSchema": _to_gemini_schema(schema),
            },
        }
        data = self._session.post_json(url, payload, {"x-goog-api-key": key})

        usage = data.get("usageMetadata") or {}
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = (data.get("promptFeedback") or {}).get("blockReason")
            if feedback:
                raise ProviderRefusal(f"Gemini blocked this message ({feedback}).", str(feedback))
            raise ProviderError("Gemini returned no candidates.")

        candidate = candidates[0]
        finish = str(candidate.get("finishReason") or "STOP")
        if finish in ("SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT"):
            raise ProviderRefusal(f"Gemini declined to classify this message ({finish}).", finish)

        text = "".join(
            part.get("text", "")
            for part in (candidate.get("content") or {}).get("parts") or []
        )
        return Completion(
            text=self._extract_json(text),
            input_tokens=int(usage.get("promptTokenCount") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or 0),
            stop_reason="max_tokens" if finish == "MAX_TOKENS" else "end_turn",
            notes=tuple(self.notes),
        )


def _to_gemini_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a JSON Schema to the subset Gemini accepts."""
    allowed = {"type", "properties", "required", "items", "enum", "description",
               "nullable", "format", "minimum", "maximum", "propertyOrdering"}
    def convert(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        out: Dict[str, Any] = {}
        for key, value in node.items():
            if key not in allowed:
                continue  # Gemini rejects additionalProperties, $schema, ...
            if key == "type" and isinstance(value, str):
                out[key] = value.upper()
            elif key == "properties" and isinstance(value, dict):
                out[key] = {name: convert(child) for name, child in value.items()}
            elif key == "items":
                out[key] = convert(value)
            else:
                out[key] = value
        if out.get("type") == "OBJECT" and "properties" in out:
            out.setdefault("propertyOrdering", list(out["properties"]))
        return out
    return convert(schema)


# ==========================================================================
# OpenAI and anything that speaks its shape
# ==========================================================================
class OpenAIProvider(Provider):
    name = "openai"
    label = "OpenAI-compatible"
    blurb = "OpenAI, OpenRouter, Groq, Together, LM Studio, vLLM - anything with a /chat/completions endpoint."
    key_url = "https://platform.openai.com/api-keys"
    key_hint = "OpenAI keys start with “sk-”. Other gateways use their own format."
    supports_base_url = True
    default_model = "gpt-4o-mini"
    models = (
        ModelChoice("gpt-4.1-nano", "GPT-4.1 nano", "cheapest"),
        ModelChoice("gpt-4o-mini", "GPT-4o mini", "cheap and quick - a good default"),
        ModelChoice("gpt-4.1-mini", "GPT-4.1 mini", "a step up"),
        ModelChoice("gpt-4.1", "GPT-4.1", "most accurate"),
        ModelChoice("gpt-4o", "GPT-4o", "previous generation"),
    )
    pricing = {
        "gpt-4.1-nano": (0.10, 0.40),
        "gpt-4o-mini": (0.15, 0.60),
        "gpt-4.1-mini": (0.40, 1.60),
        "gpt-4.1": (2.00, 8.00),
        "gpt-4o": (2.50, 10.00),
    }
    can_list_models = True
    ENDPOINT = "https://api.openai.com/v1"

    def list_models(self) -> List[str]:
        base = self.base_url or self.ENDPOINT
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = self._session.get_json(f"{base}/models", headers)
        return sorted(
            str(entry.get("id", "")) for entry in (data.get("data") or []) if entry.get("id")
        )

    def complete(self, system: str, prompt: str, schema: Dict[str, Any],
                 message: Any = None) -> Completion:
        base = self.base_url or self.ENDPOINT
        url = f"{base}/chat/completions" if not base.endswith("/chat/completions") else base
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif "localhost" not in base and "127.0.0.1" not in base:
            self._require_key()

        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "email_triage", "strict": True, "schema": schema},
            },
        }
        try:
            data = self._session.post_json(url, payload, headers)
        except ProviderError as exc:
            # Local servers and older gateways often reject json_schema but
            # accept plain JSON mode; retry once rather than failing the scan.
            if _status(exc) in (400, 404, 422):
                self.note("endpoint rejected json_schema; retried in plain JSON mode")
                payload["response_format"] = {"type": "json_object"}
                data = self._session.post_json(url, payload, headers)
            else:
                raise

        usage = data.get("usage") or {}
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("The endpoint returned no choices.")
        choice = choices[0]
        finish = str(choice.get("finish_reason") or "stop")
        if finish == "content_filter":
            raise ProviderRefusal("The endpoint's content filter blocked this message.", "content_filter")
        message = choice.get("message") or {}
        if message.get("refusal"):
            raise ProviderRefusal(str(message["refusal"]), "refusal")
        return Completion(
            text=self._extract_json(message.get("content") or ""),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            stop_reason="max_tokens" if finish == "length" else "end_turn",
            notes=tuple(self.notes),
        )


# ==========================================================================
# Ollama - on this Mac
# ==========================================================================
class OllamaProvider(Provider):
    name = "ollama"
    label = "On this Mac (Ollama)"
    blurb = "Runs locally. No API key, no cost, and no email leaves your machine."
    needs_api_key = False
    supports_base_url = True
    on_device = True
    key_url = "https://ollama.com/download"
    default_model = "llama3.2:3b"
    models = (
        ModelChoice("llama3.2:3b", "Llama 3.2 3B", "fast, ~2 GB - start here"),
        ModelChoice("qwen2.5:3b", "Qwen 2.5 3B", "~1.9 GB"),
        ModelChoice("gemma3:4b", "Gemma 3 4B", "~3.3 GB"),
        ModelChoice("qwen2.5:7b", "Qwen 2.5 7B", "noticeably better, ~4.7 GB"),
        ModelChoice("llama3.1:8b", "Llama 3.1 8B", "~4.7 GB"),
        ModelChoice("gemma3:12b", "Gemma 3 12B", "best local accuracy, ~8 GB"),
    )
    ENDPOINT = "http://localhost:11434"
    #: A local server either answers immediately or is not running.
    CONNECT_TIMEOUT = 4.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        #: Set once the server has proved unreachable, so the rest of a scan
        #: fails instantly instead of waiting out the timeout per message.
        self._unreachable: Optional[str] = None

    def base(self) -> str:
        return self.base_url or self.ENDPOINT

    def complete(self, system: str, prompt: str, schema: Dict[str, Any],
                 message: Any = None) -> Completion:
        if self._unreachable:
            raise ProviderError(self._unreachable, permanent=True)
        payload = {
            "model": self.model,
            "stream": False,
            "format": schema,          # Ollama takes a JSON schema directly
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        session = self._session
        if session.timeout > self.CONNECT_TIMEOUT and not self._session._live:
            # Nothing has connected yet: give the first attempt a short leash.
            session = HttpSession(self.CONNECT_TIMEOUT)
        try:
            data = session.post_json(f"{self.base()}/api/chat", payload)
        except ProviderError as exc:
            text = str(exc)
            if "Could not reach" in text or "timed out" in text:
                self._unreachable = (
                    f"Could not reach Ollama at {self.base()}. Install it from "
                    f"ollama.com, then run:  ollama pull {self.model}"
                )
                raise ProviderError(self._unreachable, permanent=True) from exc
            if "not found" in text.lower() and self.model in text:
                raise ProviderError(
                    f"Ollama does not have “{self.model}”. Run:  ollama pull {self.model}"
                ) from exc
            raise

        message = data.get("message") or {}
        return Completion(
            text=self._extract_json(message.get("content") or ""),
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            stop_reason="end_turn" if data.get("done") else "max_tokens",
            notes=tuple(self.notes),
        )

    can_list_models = True

    def list_models(self) -> List[str]:
        """Ask the local server which models have actually been pulled."""
        data = self._session.get_json(f"{self.base()}/api/tags")
        return sorted(str(m.get("name", "")) for m in (data.get("models") or []) if m.get("name"))

    #: Kept as the original name used elsewhere.
    installed_models = list_models


# ==========================================================================
# Local rules - no model at all
# ==========================================================================
# ==========================================================================
# Local rules - no model at all
# ==========================================================================
class RulesProvider(Provider):
    """The hand-built classifier in :mod:`rules_engine`.

    Instant, free, offline, and completely deterministic. It is markedly less
    capable than any of the model backends at reading intent, so it leans on
    the confidence threshold: uncertain mail goes to Needs Review rather than
    into a folder.
    """

    name = "rules"
    label = "Local rules (no AI)"
    blurb = (
        "Instant, free, offline and deterministic. Less capable than a model, "
        "so it sends more mail to Needs Review - and never sends any of it anywhere."
    )
    needs_api_key = False
    on_device = True
    default_model = "rules-v1"
    models = (
        ModelChoice("rules-v1", "Built-in rule set", "≈300 weighted signals, no network"),
    )

    def __init__(self, *args, ruleset: str = "general", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ruleset = ruleset or "general"
        self._classifier = None

    def classifier(self):
        if self._classifier is None:
            from rules_engine import RuleClassifier

            self._classifier = RuleClassifier(ruleset=self.ruleset)
        return self._classifier

    def describe(self) -> str:
        import rulesets as _rulesets

        return f"{self.label} · {_rulesets.get(self.ruleset).label}"

    def complete(self, system: str, prompt: str, schema: Dict[str, Any],
                 message: Any = None) -> Completion:
        import json as _json

        if message is None:
            raise ProviderError(
                "The local rules engine needs the parsed message, not a rendered prompt."
            )
        verdict = self.classifier().classify(
            subject=getattr(message, "subject", "") or "",
            body=getattr(message, "body_text", "") or "",
            sender=getattr(message, "sender_display", "") or "",
            links=getattr(message, "links", ()) or (),
            list_unsubscribe=getattr(message, "list_unsubscribe", "") or "",
            truncated=bool(getattr(message, "truncated", False)),
        )
        return Completion(
            text=_json.dumps(verdict.to_payload()),
            input_tokens=0,
            output_tokens=0,
            stop_reason="end_turn",
            notes=tuple(self.notes),
        )

    def close(self) -> None:
        self._classifier = None
        super().close()



# ==========================================================================
# Registry
# ==========================================================================

#: Ordered the way they are offered: the one that needs no setup first.
PROVIDERS: Tuple[type, ...] = (
    RulesProvider, OllamaProvider, GeminiProvider, OpenAIProvider, AnthropicProvider,
)

#: The backend used when a model backend is unavailable and fallback is on.
FALLBACK_PROVIDER = RulesProvider.name
PROVIDERS_BY_NAME: Dict[str, type] = {cls.name: cls for cls in PROVIDERS}

#: What a fresh install uses: no key, no network, no cost, and nothing to set
#: up before the first scan. Swap to a model backend in Settings when you want
#: the extra accuracy.
DEFAULT_PROVIDER = RulesProvider.name


def provider_class(name: str) -> type:
    return PROVIDERS_BY_NAME.get((name or "").strip().lower(), RulesProvider)


def build_provider(name: str, **kwargs) -> Provider:
    return provider_class(name)(**kwargs)


def provider_choices() -> List[Tuple[str, str, str]]:
    """``(name, label, blurb)`` for the settings dropdown."""
    return [(cls.name, cls.label, cls.blurb) for cls in PROVIDERS]


def models_for(name: str) -> Tuple[ModelChoice, ...]:
    return provider_class(name).models


def default_model_for(name: str) -> str:
    return provider_class(name).default_model


#: Hosts that never leave the machine.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"})


def _is_local(host: str) -> bool:
    host = (host or "").strip().strip("[]").lower()
    if host in _LOCAL_HOSTS or host.endswith(".local"):
        return True
    # Private ranges, for an Ollama box on the same network.
    return bool(re.match(r"^(?:10\.|127\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)", host))


def _status(exc: BaseException) -> Optional[int]:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    import re

    match = re.search(r"HTTP (\d{3})", str(exc))
    return int(match.group(1)) if match else None


def _mentions(exc: BaseException, keywords: Sequence[str]) -> bool:
    text = str(exc).lower()
    return any(keyword in text for keyword in keywords)
