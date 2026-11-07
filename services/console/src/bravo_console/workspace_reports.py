"""Allowlisted presentation of existing owner-produced documents, never a gate."""

import math
import re
from decimal import Decimal, InvalidOperation

SECRET = re.compile(
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|sk-(?:ant-)?[A-Za-z0-9_-]{16,}|-----BEGIN |://[^/\s@]+:[^/\s@]*@|(?i:password|secret|token|api_key)\s*[=:]\s*[^\s,;]{8,}"
)
FEATURES = (
    "inventory",
    "recommendations",
    "exit",
    "costs",
    "ai",
    "rehearsal",
    "traffic",
    "monitoring",
    "audit",
)


def text(value, limit=512):
    if value is None:
        return "미확인"
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if type(value) in (float, int):
        if not math.isfinite(value):
            raise ValueError("non_finite_value")
        return str(value)
    if (
        type(value) is not str
        or len(value) > limit
        or any(ord(c) < 32 and c not in "\n\t" for c in value)
    ):
        raise ValueError("invalid_display_text")
    return SECRET.sub("[REDACTED]", value)


def table(title, columns, rows):
    if len(rows) > 200:
        raise ValueError("workspace_row_limit")
    return {
        "title": title,
        "columns": columns,
        "rows": [[text(cell) for cell in row] for row in rows],
    }


def series(values, limit=200):
    if type(values) is not list or len(values) > limit or any(type(v) is not dict for v in values):
        raise ValueError("invalid_workspace_rows")
    return values


def project_inventory(d, plan):
    from bravo_inventory.report import _digest, validate_inventory

    validate_inventory(d)
    if d["digest"] != _digest({k: v for k, v in d.items() if k not in {"generated_at", "digest"}}):
        raise ValueError("inventory_digest_mismatch")
    if (
        d["app"]["id"] != plan["workload_id"]
        or d["source"]["env_id"] != plan["source_environment_id"]
    ):
        raise ValueError("inventory_scope_mismatch")
    rows = [
        [
            v["id"],
            v["role"],
            v["service"],
            v["grade"],
            v["found_in"],
            v["rationale"],
            v["agent_needed"],
        ]
        for v in series(d["items"])
    ]
    candidates = [
        [
            v["env_id"],
            v["verdict"],
            ", ".join(v.get("missing_capabilities", [])),
            ", ".join(v.get("residual_dependencies", [])),
        ]
        for v in series(d["candidates"])
    ]
    return [
        table(
            "의존성 분석",
            ["ID", "역할", "서비스", "변환 등급", "발견 위치", "근거", "추가 검토"],
            rows,
        ),
        table("등록 후보의 제공 기능", ["환경", "적합성", "빠진 기능", "남는 의존성"], candidates),
    ]


def project_recommendations(d, plan):
    from bravo_recommend.recommend import _validator

    _validator().validate(d)
    if d["app_id"] != plan["workload_id"]:
        raise ValueError("recommendation_scope_mismatch")
    rows = [
        [
            v["env_id"],
            v["verdict"],
            v["rule_rank"],
            v.get("ai_rank"),
            v.get("ai_reason"),
            ", ".join(v.get("residual_dependencies", [])),
        ]
        for v in series(d["candidates"])
    ]
    excluded = [
        [v["env_id"], v["reason"], ", ".join(v.get("missing_capabilities", []))]
        for v in series(d["excluded"])
    ]
    return [
        table(
            "후보 비교 · AI는 규칙의 불가 판정을 변경하지 않음",
            ["환경", "적합성", "규칙 순위", "AI 순위", "AI 근거", "잔존 의존성"],
            rows,
        ),
        table("제외된 후보", ["환경", "제외 근거", "빠진 기능"], excluded),
    ]


def project_exit(d, plan):
    if d.get("app") != plan["workload_id"] or d.get("candidate") != plan["target_environment_id"]:
        raise ValueError("exit_scope_mismatch")
    rows = [
        [v["name"], v["category"], v["grade"], v["target_impl"], v["status"], v["check_verdict"]]
        for v in series(d["entries"])
    ]
    return [
        table(
            "탈출 리허설 · 남은 출발지 의존성",
            ["구성 요소", "분류", "변환 등급", "목적지 구현", "잔존 상태", "시험 결과"],
            rows,
        ),
        table(
            "기록된 판정",
            ["리허설 결과", "후보 적합성", "인벤토리 digest"],
            [[d["drill_verdict"], d["candidate_verdict"], d["inventory_digest"]]],
        ),
    ]


def _money(d):
    if type(d) is not dict or type(d.get("observed")) is not bool:
        raise ValueError("invalid_cost_amount")
    if not d["observed"]:
        if d.get("usd") is not None or d.get("krw_estimate") is not None:
            raise ValueError("unobserved_cost_has_value")
        return ["미관측", "미관측"]
    out = []
    for key in ("usd", "krw_estimate"):
        try:
            value = Decimal(d[key])
        except (KeyError, TypeError, InvalidOperation):
            raise ValueError("invalid_cost_amount") from None
        if not value.is_finite():
            raise ValueError("invalid_cost_amount")
        out.append(str(value))
    return out


def project_costs(d, plan):
    if (
        d.get("scope") != "account"
        or d.get("project") != "bravo-platform"
        or d.get("metric") != "UnblendedCost"
    ):
        raise ValueError("unsupported_cost_scope")
    if any(type(d.get(key)) is not bool for key in ("attribution_complete", "estimated")):
        raise ValueError("invalid_cost_attribution")
    rows = [[key, *_money(d["components"][key])] for key in ("source", "platform", "target")]
    rows += [
        ["미귀속 비용", *_money(d["project_unattributed"])],
        ["다른 프로젝트", *_money(d["other_projects"])],
        ["계정 전체", *_money(d["account_total"])],
    ]
    return [
        table(
            "계정 비용 · 앱별 비용이 아님 · 원화는 참고 환율 추정",
            ["범위", "USD", "KRW 추정"],
            rows,
        ),
        table(
            "청구 집계 범위",
            ["시작일(포함)", "종료일(제외)", "태그 귀속 완료", "청구 추정치"],
            [
                [
                    d["period"]["start_inclusive"],
                    d["period"]["end_exclusive"],
                    d["attribution_complete"],
                    d["estimated"],
                ]
            ],
        ),
        table(
            "예산·환율 기준 · 자동 과금 차단 한도가 아님",
            ["월 예산 KRW", "참고 환율", "환율 기준일", "출처"],
            [
                [
                    d.get("fx", {}).get(k)
                    for k in ("monthly_budget_krw", "krw_per_usd", "fx_date", "fx_source")
                ]
            ],
        ),
    ]


def project_ai(d, plan):
    # The host registers the existing T10/T15 receipts, not raw prompts/model output.
    if d.get("workload_id") != plan["workload_id"]:
        raise ValueError("proposal_scope_mismatch")
    return [
        table(
            "AI 제안·실행 산출물",
            ["요청", "상태", "런타임 소스", "제안 digest", "이미지 digest"],
            [
                [
                    d.get(k)
                    for k in (
                        "request_id",
                        "status",
                        "runtime_source_sha",
                        "proposal_digest",
                        "image_digest",
                    )
                ]
            ],
        ),
        table(
            "검토할 변경",
            ["파일", "변경 종류", "검토 내용"],
            [[v["path"], v["kind"], v["summary"]] for v in series(d.get("changes", []))],
        ),
    ]


def project_rehearsal(d, plan):
    return [
        table(
            "리허설 실행·독립 검증",
            ["실행 ID", "시도", "상태", "검증 ID", "이미지 digest"],
            [
                [
                    v.get(k)
                    for k in (
                        "rehearsal_id",
                        "attempt",
                        "status",
                        "verification_id",
                        "image_digest",
                    )
                ]
                for v in series(d.get("attempts", []))
            ],
        )
    ]


def project_traffic(d, plan):
    weights = d.get("weights")
    if weights is not None:
        if (
            type(weights) is not dict
            or set(weights) != {"source", "target"}
            or any(type(v) is not int or not 0 <= v <= 100 for v in weights.values())
            or sum(weights.values()) != 100
        ):
            raise ValueError("invalid_traffic_weights")
    pause = d.get("write_pause_ms")
    if pause is not None and (type(pause) is not int or pause < 0):
        raise ValueError("invalid_write_pause")
    return [
        table(
            "경로 반영 실측 · 계획 단계로 추정하지 않음",
            ["출발 가중치 %", "목적 가중치 %", "쓰기 정지 ms", "관측 시각", "영수증"],
            [
                [
                    weights and weights["source"],
                    weights and weights["target"],
                    pause,
                    d.get("observed_at"),
                    d.get("receipt_id"),
                ]
            ],
        ),
        table(
            "SHIFT·WATCH 판정 기록",
            ["단계", "판정", "창 ID", "기준 버전", "판정 시각"],
            [
                [
                    v.get(k)
                    for k in ("phase", "decision", "window_id", "profile_version", "evaluated_at")
                ]
                for v in series(d.get("judgements", []))
            ],
        ),
    ]


def project_monitoring(d, plan):
    tables = []
    for side in ("source", "target"):
        item = d[side]
        if item["environment_id"] != plan[side + "_environment_id"]:
            raise ValueError("monitoring_scope_mismatch")
        http, db, pool = item.get("http", {}), item.get("database", {}), item.get("pgbouncer", {})
        tables.append(
            table(
                "출발지" if side == "source" else "목적지",
                [
                    "HTTP 상태",
                    "RPS",
                    "오류율",
                    "p95 초",
                    "마지막 수신 지연 초",
                    "DB 상태",
                    "풀러 상태",
                ],
                [
                    [
                        http.get(k)
                        for k in (
                            "state",
                            "requests_per_second",
                            "error_ratio",
                            "p95_seconds",
                            "last_received_seconds_ago",
                        )
                    ]
                    + [db.get("state"), pool.get("state")]
                ],
            )
        )
        tables.append(
            table(
                "DB 인스턴스",
                ["DB 상태", "클러스터", "Pod", "역할", "응답 확인"],
                [
                    [db.get("state"), v.get("cluster"), v.get("pod"), v.get("role"), v.get("up")]
                    for v in series(db.get("instances", []))
                ],
            )
        )
        tables.append(
            table(
                "DB 복제",
                ["DB 상태", "클러스터", "슬롯", "복제 지연 bytes", "슬롯 활성"],
                [
                    [
                        db.get("state"),
                        v.get("cluster"),
                        v.get("slot"),
                        v.get("lag_bytes"),
                        v.get("active"),
                    ]
                    for v in series(db.get("replication_slots", []))
                ],
            )
        )
        pools = [
            [
                v.get("pooler"),
                v.get("up"),
                x.get("name"),
                x.get("paused"),
                x.get("clients_active"),
                x.get("clients_waiting"),
                x.get("servers_active"),
            ]
            for v in series(pool.get("poolers", []))
            for x in series(v.get("databases", []))
        ]
        tables.append(
            table(
                "PgBouncer",
                ["풀러", "상태", "DB", "PAUSE", "활성 연결", "대기 연결", "서버 연결"],
                pools,
            )
        )
    tables.append(
        table(
            "관측 경보",
            ["환경", "경보", "심각도", "상태"],
            [
                [v.get(k) for k in ("environment", "alertname", "severity", "state")]
                for v in series(d.get("alerts", []))
            ],
        )
    )
    return tables


def project_audit(d, plan):
    # PlanAudit performs the T24 chain/provenance checks separately.
    if (
        d.get("kind") != "console-agent-audit.v1"
        or d.get("plan_id") != plan["plan_id"]
        or d.get("plan_digest") != plan["plan_digest"]
    ):
        raise ValueError("audit_scope_mismatch")
    return [
        table(
            "버킷 삭제 시도 차단 · 에이전트 도구 정책 (AWS IAM 시험과 구분)",
            ["기록 ID", "시도", "도구", "결과", "근거", "시각"],
            [
                [
                    v.get(k)
                    for k in ("record_id", "attempt", "tool", "outcome", "reason", "recorded_at")
                ]
                for v in series(d["rows"])
            ],
        ),
        table(
            "감사 체인",
            ["실행 ID", "기록 수", "체인 digest", "보존 검증"],
            [[d["run_id"], d["record_count"], d["chain_digest"], d["retention_verified"]]],
        ),
    ]


PROJECTORS = {name: globals()["project_" + name] for name in FEATURES}
