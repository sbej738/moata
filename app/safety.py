"""신고 탭과 카테고리(신고하기_화면.md). 화면은 /api/config로 이 목록을 받아 그린다."""
from __future__ import annotations

REPORT_TABS = [
    {"kind": "emergency", "label": "긴급·안전", "categories": [
        {"code": "accident", "label": "사고가 났어요", "icon": "car-crash"},
        {"code": "violence", "label": "위협·폭력을 당했어요", "icon": "shield-alert"},
        {"code": "sexual", "label": "성희롱·성추행", "icon": "user-x"},
        {"code": "sick", "label": "몸이 아파요", "icon": "heart-pulse"},
    ]},
    {"kind": "driving", "label": "운행", "categories": [
        {"code": "reckless", "label": "난폭·과속 운전", "icon": "gauge"},
        {"code": "route_deviation", "label": "경로를 벗어났어요", "icon": "route"},
        {"code": "rude_driver", "label": "기사님이 불친절해요", "icon": "message-x"},
        {"code": "vehicle", "label": "차량 상태가 나빠요", "icon": "car"},
    ]},
    {"kind": "fare", "label": "요금", "categories": [
        {"code": "fare_mismatch", "label": "요금이 예상과 달라요", "icon": "receipt"},
        {"code": "meter_off", "label": "미터기를 안 켰어요", "icon": "meter"},
        {"code": "toll", "label": "통행료가 이상해요", "icon": "toll"},
        {"code": "payment", "label": "송금에 문제가 있어요", "icon": "wallet"},
    ]},
    {"kind": "companion", "label": "동승자", "categories": [
        {"code": "no_show", "label": "동승자가 안 나왔어요", "icon": "user-clock"},
        {"code": "harassment", "label": "불쾌한 말·행동", "icon": "message-alert"},
        {"code": "late", "label": "너무 늦게 왔어요", "icon": "clock"},
        {"code": "unpaid", "label": "정산 금액을 안 보내요", "icon": "wallet-x"},
    ]},
]

CATEGORIES = {t["kind"]: {c["code"]: c["label"] for c in t["categories"]} for t in REPORT_TABS}
KIND_LABEL = {t["kind"]: t["label"] for t in REPORT_TABS}
DEDUPE_SEC = 60   # 같은 신고를 1분 안에 다시 보내면 기존 신고를 돌려준다(긴급·안전 제외)


def category_label(kind: str, code: str) -> str:
    return CATEGORIES.get(kind, {}).get(code, code)
