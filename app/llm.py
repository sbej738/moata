"""AI 제공자 공통 계층: 작업별 순서(예: Groq → Gemini)로 부르고, 실패하면 호출한 쪽이 통계·알고리즘으로 대체한다.

- LLMProvider: 모든 제공자가 따르는 모양. generate_json(system, payload, schema) → dict
- GeminiProvider: 일시 오류(500/503/504)는 1.5초 뒤 한 번 재시도, 한도 초과(429)는 재시도 없이 대체 모델 한 번
- GroqProvider: OpenAI 호환 chat/completions + JSON 스키마 응답(지원 안 하면 JSON 모드로 자동 전환)
- AIRouter: 제공자를 차례로 시도한다. 429나 연속 실패가 나면 그 제공자를 AI_COOLDOWN_SEC 동안 쉰다(서킷 브레이커).
  운영 화면용으로 제공자별 성공·실패 수와 마지막 오류를 모은다.

키와 요청 헤더는 오류 메시지·로그·통계에 넣지 않는다.
"""
from __future__ import annotations

import copy
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

import httpx

log = logging.getLogger("moata.llm")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GROQ_BASE = "https://api.groq.com/openai/v1"
RETRY_DELAY_SEC = 1.5
TRANSIENT = (500, 503, 504)       # 잠깐 쉬고 한 번 더
RATE_LIMIT = 429                   # 한도 초과: 다시 불러도 소용없으니 바로 다음으로
SKIP_MODEL = (400, 403, 404)       # 모델 이름이 없거나 이 키로 못 쓰는 모델


class LLMError(Exception):
    """제공자 한 곳의 실패. status는 HTTP 상태(없으면 None)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AllProvidersFailed(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors) or "사용할 수 있는 AI 제공자가 없습니다")
        self.errors = errors


@dataclass
class AIResult:
    data: dict
    provider: str            # "gemini" | "groq"
    model: str


class LLMProvider(Protocol):
    name: str
    model: str

    def generate_json(self, system: str, payload: dict, schema: dict) -> tuple[dict, str]:
        """JSON 응답과 실제로 응답한 모델 이름을 돌려준다. 실패하면 LLMError."""


# ---------------------------------------------------------------------------
# 스키마 변환: 표준 JSON Schema(소문자 type) → Gemini responseSchema(대문자 type)
# ---------------------------------------------------------------------------
def to_gemini_schema(schema: dict) -> dict:
    s = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            node.pop("additionalProperties", None)
            if isinstance(node.get("type"), str):
                node["type"] = node["type"].upper()
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(s)
    return s


def _http_error(resp: httpx.Response) -> LLMError:
    detail = ""
    try:
        err = resp.json().get("error", "")
        detail = (err.get("message") or err.get("status") or "") if isinstance(err, dict) else str(err)
    except (ValueError, AttributeError):
        detail = resp.text
    return LLMError(f"HTTP {resp.status_code}: {str(detail)[:200]}", resp.status_code)


def _parse_json_text(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("JSON 객체가 아닙니다")
    return data


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str, fallback_model: str = "", timeout: float = 8.0,
                 client: httpx.Client | None = None):
        self.model = model
        self.fallback_model = fallback_model
        self.headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        self.client = client or httpx.Client(base_url=GEMINI_BASE, timeout=timeout)

    def _call(self, model: str, body: dict) -> httpx.Response:
        try:
            return self.client.post(f"/models/{model}:generateContent", headers=self.headers, json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"연결 오류: {type(exc).__name__}") from exc

    def generate_json(self, system: str, payload: dict, schema: dict) -> tuple[dict, str]:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json",
                                 "responseSchema": to_gemini_schema(schema)},
        }
        used = self.model
        resp = self._call(used, body)
        if resp.status_code in TRANSIENT:
            time.sleep(RETRY_DELAY_SEC)
            resp = self._call(used, body)
        if resp.status_code >= 400 and self.fallback_model and resp.status_code in TRANSIENT + SKIP_MODEL + (RATE_LIMIT,):
            alt = self._call(self.fallback_model, body)
            if alt.status_code < 400:
                resp, used = alt, self.fallback_model
            else:
                log.info("Gemini 대체 모델 %s 실패: HTTP %s", self.fallback_model, alt.status_code)
        if resp.status_code >= 400:
            raise _http_error(resp)
        try:
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            return _parse_json_text(text), used
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise LLMError(f"응답 형식 오류: {type(exc).__name__}") from exc

    def list_models(self) -> list[str]:
        resp = self.client.get("/models", headers=self.headers, params={"pageSize": 1000})
        if resp.status_code >= 400:
            raise _http_error(resp)
        return [m["name"].removeprefix("models/") for m in resp.json().get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])]


# ---------------------------------------------------------------------------
# Groq (OpenAI 호환, 무료 플랜 있음)
# ---------------------------------------------------------------------------
class GroqProvider:
    """Groq chat/completions. 모델이 JSON 스키마 응답(json_schema)을 지원하지 않으면
    JSON 모드(json_object) + 프롬프트에 스키마를 적는 방식으로 자동 전환하고 기억한다."""

    name = "groq"
    SKIP = ("whisper", "guard", "tts", "playai", "vision", "distil", "prompt-guard", "compound")
    PREFER = ("llama-3.3-70b", "gpt-oss-120b", "gpt-oss-20b", "llama-3.1-8b", "versatile", "instant")

    def __init__(self, api_key: str, model: str, timeout: float = 8.0, client: httpx.Client | None = None,
                 reasoning_effort: str = "low"):
        self.model = model
        self.mode = "json_schema"
        # gpt-oss 같은 추론 모델은 '생각' 토큰을 많이 써서 무료 분당 한도에 빨리 걸린다. low면 토큰 약 40% 절약
        self.reasoning_effort = reasoning_effort or None
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.client = client or httpx.Client(base_url=GROQ_BASE, timeout=timeout)

    def _body(self, system: str, payload: dict, schema: dict) -> dict:
        if self.mode == "json_schema":
            fmt = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}}
        else:
            fmt = {"type": "json_object"}
            system = (f"{system}\n\n응답은 반드시 JSON 객체 하나만 출력한다. 다른 글은 쓰지 않는다. "
                      f"JSON 스키마: {json.dumps(schema, ensure_ascii=False)}")
        body = {"model": self.model, "temperature": 0.2, "response_format": fmt,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]}
        if self.reasoning_effort and "gpt-oss" in self.model:
            body["reasoning_effort"] = self.reasoning_effort
        return body

    def _post(self, body: dict) -> httpx.Response:
        try:
            return self.client.post("/chat/completions", headers=self.headers, json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"연결 오류: {type(exc).__name__}") from exc

    def generate_json(self, system: str, payload: dict, schema: dict) -> tuple[dict, str]:
        resp = self._post(self._body(system, payload, schema))
        if resp.status_code in TRANSIENT:
            time.sleep(RETRY_DELAY_SEC)
            resp = self._post(self._body(system, payload, schema))
        text = resp.text.lower()
        if resp.status_code == 400 and "reasoning_effort" in text and self.reasoning_effort:
            self.reasoning_effort = None          # 이 모델은 생각 강도 설정을 받지 않는다
            resp = self._post(self._body(system, payload, schema))
            text = resp.text.lower()
        if resp.status_code == 400 and self.mode == "json_schema" and ("response_format" in text or "json_schema" in text
                                                                         or "structured" in text):
            log.info("Groq 모델 %s는 JSON 스키마 응답을 지원하지 않아 JSON 모드로 바꿉니다", self.model)
            self.mode = "json_object"
            resp = self._post(self._body(system, payload, schema))
            text = resp.text.lower()
        if resp.status_code in (400, 404) and "model" in text and ("not" in text or "decommission" in text):
            # 모델 이름이 없어졌거나 틀렸으면, 이 키로 쓸 수 있는 모델을 찾아 한 번 다시 시도한다
            alt = self._pick_available_model()
            if alt and alt != self.model:
                log.warning("Groq 모델 %s를 쓸 수 없어 %s로 바꿉니다", self.model, alt)
                self.model, self.mode = alt, "json_schema"
                resp = self._post(self._body(system, payload, schema))
                if resp.status_code == 400 and "response_format" in resp.text.lower():
                    self.mode = "json_object"
                    resp = self._post(self._body(system, payload, schema))
        if resp.status_code >= 400:
            raise _http_error(resp)
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return _parse_json_text(content), data.get("model") or self.model
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise LLMError(f"응답 형식 오류: {type(exc).__name__}") from exc

    def _pick_available_model(self) -> str | None:
        """대화용 모델 중 PREFER 순서로 고른다(음성·안전 필터 모델 제외)."""
        try:
            resp = self.client.get("/models", headers=self.headers)
            ids = [m.get("id", "") for m in resp.json().get("data", [])] if resp.status_code < 400 else []
        except (httpx.HTTPError, ValueError, AttributeError):
            return None
        ids = [i for i in ids if i and not any(b in i.lower() for b in self.SKIP)]
        for want in self.PREFER:
            hits = sorted(i for i in ids if want in i)
            if hits:
                return hits[0]
        return sorted(ids)[0] if ids else None


# ---------------------------------------------------------------------------
# 라우터: 순서대로 시도 + 서킷 브레이커 + 통계
# ---------------------------------------------------------------------------
@dataclass
class ProviderStats:
    calls: int = 0
    ok: int = 0
    failed: int = 0
    consecutive_fail: int = 0
    open_until: float = 0.0
    last_error: str | None = None
    last_error_at: float | None = None
    last_ok_at: float | None = None
    recent: deque = field(default_factory=lambda: deque(maxlen=200))  # (시각, 성공 여부)


class AIRouter:
    def __init__(self, providers: list[LLMProvider], cooldown_sec: int = 60, clock=time.time):
        self.providers = providers
        self.cooldown_sec = cooldown_sec
        self.clock = clock
        self.stats: dict[str, ProviderStats] = {p.name: ProviderStats() for p in providers}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.providers)

    def is_open(self, name: str) -> bool:
        return self.stats[name].open_until > self.clock()

    def ordered(self, order: list[str] | None) -> list[LLMProvider]:
        """작업별 순서(예: ["groq", "gemini"]). 순서에 없는 제공자는 뒤에 붙인다. 키가 없는 제공자는 건너뛴다."""
        if not order:
            return list(self.providers)
        by_name = {p.name: p for p in self.providers}
        first = [by_name[n] for n in order if n in by_name]
        return first + [p for p in self.providers if p not in first]

    def generate_json(self, system: str, payload: dict, schema: dict, order: list[str] | None = None) -> AIResult:
        errors: list[str] = []
        for p in self.ordered(order):
            st = self.stats[p.name]
            if self.is_open(p.name):
                errors.append(f"{p.name}: 잠시 쉬는 중({int(st.open_until - self.clock())}초 남음)")
                continue
            with self._lock:
                st.calls += 1
            try:
                data, model = p.generate_json(system, payload, schema)
            except LLMError as exc:
                self._fail(p.name, exc)
                errors.append(f"{p.name}: {exc}")
                continue
            with self._lock:
                st.ok += 1
                st.consecutive_fail = 0
                st.last_ok_at = self.clock()
                st.recent.append((self.clock(), True))
            return AIResult(data, p.name, model)
        raise AllProvidersFailed(errors)

    def _fail(self, name: str, exc: LLMError) -> None:
        with self._lock:
            st = self.stats[name]
            st.failed += 1
            st.consecutive_fail += 1
            st.last_error = str(exc)[:300]
            st.last_error_at = self.clock()
            st.recent.append((self.clock(), False))
            if exc.status == RATE_LIMIT or st.consecutive_fail >= 2:
                st.open_until = self.clock() + self.cooldown_sec
                log.warning("AI 제공자 %s를 %d초 쉽니다: %s", name, self.cooldown_sec, exc)

    def status(self) -> list[dict]:
        now = self.clock()
        out = []
        for p in self.providers:
            st = self.stats[p.name]
            last10 = [ok for t, ok in st.recent if now - t <= 600]
            out.append({
                "provider": p.name, "model": p.model, "calls": st.calls, "ok": st.ok, "failed": st.failed,
                "success_rate_10m": round(sum(last10) / len(last10), 3) if last10 else None,
                "cooling_down_sec": max(0, int(st.open_until - now)), "last_error": st.last_error,
            })
        return out


def parse_order(text: str) -> list[str]:
    return [x.strip().lower() for x in (text or "").split(",") if x.strip()]


def make_router(cfg) -> AIRouter:
    providers: list[LLMProvider] = []
    if cfg.gemini_api_key:
        providers.append(GeminiProvider(cfg.gemini_api_key, cfg.gemini_model, cfg.gemini_fallback_model,
                                        timeout=cfg.ai_timeout_sec))
    if cfg.groq_api_key:
        providers.append(GroqProvider(cfg.groq_api_key, cfg.groq_model, timeout=cfg.ai_timeout_sec,
                                      reasoning_effort=cfg.groq_reasoning_effort))
    return AIRouter(providers, cooldown_sec=cfg.ai_cooldown_sec)
