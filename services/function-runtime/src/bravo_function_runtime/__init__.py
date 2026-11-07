"""SQS → Lambda 호환 런타임 (T46, ADR-0041).

세 부품으로 나눈다. 2단계(공통 인터페이스)로 갈 때 queue와 event만 바꾸고 invoke는 그대로 쓴다.

- queue: 큐 연결(꺼내기 · 지우기 · 속성)
- invoke: 호출 관리(하위 프로세스, 제한 시간 초과 시 종료)
- event: 메시지 형식(AWS SQS 이벤트와 context)

판정 · 실행 경로라 LLM 클라이언트를 import하지 않는다.
"""
