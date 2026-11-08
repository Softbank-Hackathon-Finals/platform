"""출발지 실제 설정과의 대조(T46 spec 완성 기준 12).

입력은 AWS 읽기 전용 API 출력을 그대로 저장한 JSON이다.
- 이벤트 소스 매핑: `aws lambda get-event-source-mapping --uuid ...`
- 함수 설정: `aws lambda get-function-configuration --function-name ...`

출발지에서 켜 둔 기능을 실행기가 재현하지 못하거나 설정 값이 다르면, 실행기를 시작하지 않고 리허설도 FAIL로 끝낸다.
이 모듈은 AWS에 접속하지 않는다. 수집은 읽기 전용 수집 역할이 맡는다.
"""

from __future__ import annotations

from .config import Binding


def check_source(binding: Binding, mapping: dict, function: dict) -> list[str]:
    name = binding.function_name
    problems: list[str] = []

    queue = str(mapping.get("EventSourceArn", "")).rsplit(":", 1)[-1]
    if not queue:
        problems.append(f"{name}: 출발지 매핑에 EventSourceArn이 없다")
    elif queue != binding.queue_name:
        problems.append(f"{name}: 출발지 큐({queue})와 설정 큐({binding.queue_name})가 다르다")
    if queue.endswith(".fifo"):
        problems.append(f"{name}: 지원 안 함 — 출발지가 FIFO 큐")
    if (mapping.get("FilterCriteria") or {}).get("Filters"):
        problems.append(f"{name}: 지원 안 함 — 출발지 매핑에 이벤트 필터가 있다")
    if "ReportBatchItemFailures" in (mapping.get("FunctionResponseTypes") or []):
        problems.append(f"{name}: 지원 안 함 — 출발지 매핑이 배치 일부 실패 응답을 쓴다")
    if int(mapping.get("MaximumBatchingWindowInSeconds") or 0) > 0:
        problems.append(f"{name}: 지원 안 함 — 출발지 매핑의 배치 대기 시간이 0이 아니다")
    if mapping.get("DestinationConfig", {}).get("OnFailure", {}).get("Destination"):
        problems.append(f"{name}: 지원 안 함 — 출발지 매핑에 실패 목적지가 있다")
    if int(mapping.get("BatchSize") or 10) != binding.batch_size:
        problems.append(f"{name}: 배치 크기가 다르다(출발지 {mapping.get('BatchSize')}, 설정 {binding.batch_size})")
    limit = (mapping.get("ScalingConfig") or {}).get("MaximumConcurrency")
    if limit is not None and int(limit) != binding.max_concurrency:
        problems.append(f"{name}: 동시 실행 상한이 다르다(출발지 {limit}, 설정 {binding.max_concurrency})")

    runtime = str(function.get("Runtime", ""))
    if function.get("PackageType", "Zip") != "Zip" or not runtime.startswith("python3"):
        problems.append(f"{name}: 지원 안 함 — Python zip 함수가 아니다({function.get('PackageType')}, {runtime})")
    if function.get("Handler") != binding.handler:
        problems.append(f"{name}: 핸들러가 다르다(출발지 {function.get('Handler')}, 설정 {binding.handler})")
    if float(function.get("Timeout", 3)) != binding.timeout_s:
        problems.append(f"{name}: 제한 시간이 다르다(출발지 {function.get('Timeout')}초, 설정 {binding.timeout_s:g}초)")
    if int(function.get("MemorySize", 128)) != binding.memory_mb:
        problems.append(f"{name}: 메모리 표시 값이 다르다(출발지 {function.get('MemorySize')}, 설정 {binding.memory_mb})")
    if function.get("Layers"):
        problems.append(f"{name}: 지원 안 함 — 레이어는 함수 이미지에 포함해야 한다")
    return problems
