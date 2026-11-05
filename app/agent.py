"""AI 에이전트: 계산은 코드와 OR-Tools가 하고, AI는 '판단'만 한다.

- MatchAgent: OR-Tools가 만든 최적 계획과 후보 목록을 보고, 각 그룹을 지금 제안할지(propose) 조금 더 기다릴지(wait)
  판단하고 이유를 붙인다. 최적 계획 대신 다른 후보를 고를 수도 있다. 후보는 이미 규칙을 통과한 것만 준다.
  결과는 서비스가 다시 검증한다(모르는 후보, 겹치는 그룹, 마감 임박 대기는 실행하지 않거나 바로잡는다).
  여러 번의 도구 호출(list_candidates → evaluate_group → decide)을 한 번의 구조화 응답으로 합쳐 호출 수를 줄였다.
- DispatchAgent: 기사님에게 보여 줄 배차 추천 이유를 쓴다.
- OpsSummarizer: 운영 지표를 보고 한 시간마다 이상 징후를 요약한다(경보 판정은 규칙으로 한다).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .llm import AIResult, AIRouter

MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string", "description": "후보 id(예: C3)"},
                    "action": {"type": "string", "enum": ["propose", "wait"]},
                    "reason": {"type": "string", "description": "운영자가 읽을 한국어 한 문장"},
                },
                "required": ["candidate_id", "action", "reason"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string", "description": "이번 판단 전체를 한국어 한두 문장으로"},
    },
    "required": ["decisions", "summary"],
    "additionalProperties": False,
}

MATCH_SYSTEM = """너는 택시 동승 서비스 '모아타'의 매칭 판단 에이전트다.
서버가 규칙(같은 성별, 정원 3명, 시간대 겹침, 출발지 1km 이내, 같은 방향, 우회 한도)을 모두 통과한 후보 그룹과,
수리 최적화(OR-Tools)가 고른 최적 계획(optimal_plan)을 준다. 너는 계산하지 말고 판단만 한다.

할 일: optimal_plan의 각 그룹, 또는 그 대신 고를 다른 후보에 대해 action을 정한다.
- propose: 지금 승객들에게 동승을 제안한다.
- wait: 조금 더 기다리면 더 좋은 조합(예: 2명 → 3명)이 생길 가능성이 높을 때만. 근거가 약하면 propose.
규칙:
- candidate_id는 반드시 주어진 candidates 목록의 id만 쓴다. 새 조합을 만들지 않는다.
- propose로 고른 그룹끼리는 같은 승객(rid)이 겹치면 안 된다.
- 어떤 승객이든 minutes_to_deadline이 5분 이하이면 그 승객이 든 그룹은 wait하지 않는다.
- 빠른 매칭(quick) 승객은 몇 분 안에 출발하려는 사람이라 오래 기다리게 하지 않는다. 예약(reserved)은 여유가 있다.
- 여러 번 거절당한 승객(rejected_count가 큼)과 마감이 가까운 승객을 먼저 챙긴다.
- 특별한 이유가 없으면 optimal_plan을 그대로 propose한다.
- reason과 summary는 운영자가 읽는 쉬운 한국어로, 주어진 숫자를 근거로 쓴다."""

DISPATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "description": "기사님에게 보여 줄 추천 이유 한국어 한두 문장"},
    },
    "required": ["reason"],
    "additionalProperties": False,
}

DISPATCH_SYSTEM = """너는 택시 동승 서비스 '모아타'의 배차 에이전트다.
수리 최적화가 이 기사님에게 이 동승 그룹을 배정했다. 기사님이 운전 중에 한눈에 읽을 수 있게
왜 이 배차가 좋은지 한국어 한두 문장으로 쓴다. 픽업까지 거리, 총 거리, 예상 요금, 승객 수 같은 주어진 숫자만 근거로 쓴다.
지어낸 정보(교통 상황, 날씨 등)는 쓰지 않는다. 존댓말로 짧게."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "group": {"type": "string", "description": "그룹 id(예: G12)"},
                    "driver": {"type": "string", "description": "고른 기사님 id(예: D3). 그 그룹의 후보 중에서만"},
                    "reason": {"type": "string", "description": "기사님에게 보여 줄 추천 이유 한국어 한두 문장(존댓말)"},
                },
                "required": ["group", "driver", "reason"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string", "description": "운영자가 읽을 이번 배차 판단 요약 한 문장"},
    },
    "required": ["assignments", "summary"],
    "additionalProperties": False,
}

JUDGE_SYSTEM = """너는 택시 동승 서비스 '모아타'의 배차 판단 에이전트다.
수리 최적화(OR-Tools)가 픽업 거리만 보고 정한 배정(optimal)과, 그룹마다 갈 수 있는 후보 기사님 목록을 준다.
너는 거리 말고도 아래를 함께 보고 그룹마다 기사님 한 명을 고른다.
- 공정성: 오늘 운행이 적거나 오래 쉰 기사님(idle_min이 큼)에게 기회를 준다.
- 예약 연결: next_reservation_min이 작으면(곧 예약 운행이 있으면) 멀리 가는 배차를 피한다.
  drop_to_next_reservation_km가 작으면 하차 뒤 예약 출발지로 가기 좋다.
- 승객 대기: 픽업 거리(pickup_km)가 크게 늘면 승객이 오래 기다린다. 최적보다 크게 멀어지면 고르지 않는다.
규칙:
- driver는 반드시 그 그룹의 candidates 안에서만 고른다. 한 기사님을 두 그룹에 주지 않는다.
- 특별한 이유가 없으면 optimal을 그대로 따른다.
- reason은 기사님이 운전 중에 읽을 짧은 존댓말 문장으로, 주어진 숫자만 근거로 쓴다(교통·날씨를 지어내지 않는다)."""

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "지난 한 시간 운영 상황 한국어 두세 문장"},
        "concerns": {"type": "array", "items": {"type": "string"}, "description": "운영자가 확인할 점(없으면 빈 배열)"},
    },
    "required": ["summary", "concerns"],
    "additionalProperties": False,
}

SUMMARY_SYSTEM = """너는 택시 동승 서비스 '모아타'의 운영 분석가다. 서버가 계산한 지난 한 시간 지표를 보고
운영자에게 상황을 두세 문장으로 요약하고, 확인할 점을 짧게 나열한다. 주어진 숫자만 근거로 쓴다."""


@dataclass
class MatchDecision:
    propose: list[str]                 # 제안할 후보 id
    wait: list[str]                    # 기다리기로 한 후보 id
    reasons: dict[str, str]
    summary: str
    provider: str
    model: str
    raw: dict = field(default_factory=dict)
    reused: bool = False


class MatchAgent:
    REUSE_SEC = 90   # 후보 구성이 같으면 90초 동안 같은 판단을 다시 쓴다(호출 수 줄이기)

    def __init__(self, router: AIRouter, clock=time.time, order: list[str] | None = None):
        self.router = router
        self.clock = clock
        self.order = order
        self._last: tuple[tuple, float, MatchDecision] | None = None

    @property
    def enabled(self) -> bool:
        return self.router.enabled

    def decide(self, payload: dict, signature: tuple) -> MatchDecision:
        if self._last and self._last[0] == signature and self.clock() - self._last[1] < self.REUSE_SEC:
            d = self._last[2]
            return MatchDecision(d.propose, d.wait, d.reasons, d.summary, d.provider, d.model, d.raw, reused=True)
        ai: AIResult = self.router.generate_json(MATCH_SYSTEM, payload, MATCH_SCHEMA, self.order)
        propose, wait, reasons = [], [], {}
        for item in ai.data.get("decisions", []):
            cid = str(item.get("candidate_id", "")).strip()
            action = item.get("action")
            if not cid or action not in ("propose", "wait"):
                raise ValueError("판단 항목 형식이 올바르지 않습니다")
            (propose if action == "propose" else wait).append(cid)
            reasons[cid] = str(item.get("reason", ""))[:200]
        decision = MatchDecision(propose, wait, reasons, str(ai.data.get("summary", ""))[:300],
                                 ai.provider, ai.model, ai.data)
        self._last = (signature, self.clock(), decision)
        return decision


class DispatchAgent:
    def __init__(self, router: AIRouter, order: list[str] | None = None):
        self.router = router
        self.order = order

    def explain(self, payload: dict) -> AIResult:
        return self.router.generate_json(DISPATCH_SYSTEM, payload, DISPATCH_SCHEMA, self.order)

    def judge(self, payload: dict) -> AIResult:
        """OR-Tools가 낸 후보 기사님 중에서 그룹마다 한 명을 고른다(배차 판단)."""
        return self.router.generate_json(JUDGE_SYSTEM, payload, JUDGE_SCHEMA, self.order)


class OpsSummarizer:
    def __init__(self, router: AIRouter, order: list[str] | None = None):
        self.router = router
        self.order = order

    def summarize(self, metrics: dict) -> AIResult:
        return self.router.generate_json(SUMMARY_SYSTEM, metrics, SUMMARY_SCHEMA, self.order)
