# NET-01.1 외부 공개 포트 검사

T5의 노드·LB 주소 외부 검사에 Nmap TCP connect scan을 사용한다.
직접 포트 스캐너를 만들지 않으며 공개 스캔 API도 제공하지 않는다.
`public_ports.PublicPortsProbe`를 신뢰된 T14 호스트가 등록한다.

## 검사와 판정

1. 호스트가 승인된 앱·환경의 **전체 노드/LB 주소 집합**, 자원 UID,
   IPv4별 허용 포트, 공개 진입점 참조, 외부 실행기 신원, inventory revision을 등록한다.
2. `read_inventory(context, external_agent)`가 등록 집합과 외부 실행 위치를 확인한다.
   검증되지 않은 사용자 JSON을 이 콜백으로 돌려주면 안 된다.
3. 같은 외부 실행기에서 공개 진입점의 정상 응답을 확인한다. 운영 콜백은
   실제 HTTPS 인증서·호스트명·기대 응답을 검사하고 현재 호출 nonce와 시각을 결속한다.
4. 등록된 각 IP의 **TCP 1–65535 전체**를 Nmap으로 검사한다.
   DNS, 호스트 검색, NSE, 서비스/OS 판별, UDP, shell, 사용자 옵션은 실행하지 않는다.
5. 공개 진입점 응답과 inventory를 다시 확인한다. 전후의 nonce는 다르며
   다른 앱·환경·실행기·진입점·오래된 응답은 거부한다.
6. 요약 증거가 보존된 뒤에만 Observation을 반환한다. T14 Runner가 T5 결과를 만든다.

| 결과 | 판정 |
|---|---|
| 전체 주소·포트 검사 완료, 허용 외 open 없음, 대조군 성공 | PASS |
| 허용 외 open 확인, 대조군 성공 | FAIL |
| filtered / open\|filtered / unfiltered 등으로 상태 확정 불가 | UNKNOWN |
| 진입점 응답 실패, 일부 주소 누락, 시간 초과, scope 변경 | UNKNOWN |
| 도구 오류·버전/해시 불일치·불완전 XML·포트 중복 | UNKNOWN |

`filtered`는 Nmap이 실제 open 여부를 결정하지 못한 상태다. 스캔만으로
정책 차단이 검증됐다고 표시하지 않는다. 방화벽이 대부분의 포트를 drop하는
실환경에서 UNKNOWN이 나올 수 있다. 이를 PASS로 바꾸려면 정책 readback과
수집 방법의 별도 검증이 필요하며 이번 구현에는 포함하지 않았다.
명확한 비허용 open을 확인하면 다른 포트가 모호해도 FAIL로 기록한다.

## 실행 제한과 신뢰 경계

- 첫 구현은 IPv4/TCP 전용이다. IPv6·UDP 노출까지 검사했다고 주장하지 않는다.
  등록 환경에 이 경로가 있으면 별도 검사 없이는 환경 전체 노출 인수를 완료하지 않는다.
- 1–8개 주소, 주소별 1–300초(default 120), Nmap `--max-rate 1000` 설정,
  재시도 1회, XML 최대 32MiB. Nmap 자체 timeout에 외부 실행 deadline을 더한다.
  출력 한도·deadline 초과 시 child를 kill/wait한다. 주소는 순차 실행한다.
  rate는 평균 속도 제한이며 순간 초과나 커널 TCP 재전송까지의 엄격한 트래픽 상한은 아니다.
- 배포자가 자격 시험한 Nmap 버전·바이너리 SHA-256을 고정한다. 실행 직전
  개인 임시 디렉터리로 복사하고 해시를 확인한다. 부모 디렉터리·동적 라이브러리·
  Nmap 데이터 파일을 포함한 실행 이미지도 배포자가 고정해야 한다.
- 앱 환경변수·AWS 인증·사용자 Nmap 설정을 프로세스에 전달하지 않는다.
  scanner 임시 디렉터리에는 실행 가능한 파일시스템이 필요하다.
- `read_inventory`는 요청자의 주소 목록이 아닌 등록된 실제 자원 집합 전체를
  읽어야 한다. 스캔 허가·실행기 인증·외부 배치·시간창·취소·전체 예산은
  호스트 책임이다. 문자열 `external_agent` 자체는 외부 위치의 증거가 아니다.
- 원문 XML은 임시 파일에서 제거한다. 보존 증거에는 scope 해시, 자원 UID,
  비허용 포트, 결과 해시만 둔다. 저장소는 접근 통제·변경 불가 보존을 적용한다.
  원문 없이 해시만으로 원본 보고서를 재구성할 수는 없다.

## 실제 도구 자격 시험

`tests/integration/verify_public_ports.py`는 폐기 가능한 Linux 컨테이너에서
실제 Nmap 7.95와 자체 loopback HTTP 서버를 사용한다.

- 허용 listener만 존재: PASS
- 같은 IP에 비허용 listener 추가: FAIL
- 공개 진입점 대조군 불가: UNKNOWN, 추가 스캔 없음

컨테이너는 비루트, capability 제거, read-only root, 외부 네트워크 없음으로
실행한다. 이 시험의 대조군은 로컬 HTTP이며 운영 HTTPS·외부 배치 검증이 아니다.
팀 AWS·온프렘 노드나 임의 인터넷 주소에는 이 시험을 실행하지 않는다.

```sh
# 호스트가 준비한 Nmap 7.95 + Python/jsonschema 이미지의 고정 digest 사용
# CONTROL_RUNNER_DIR는 이 서비스의 절대 경로, QUALIFIED_IMAGE는 자격 시험 이미지
# /tmp exec는 해시 확인한 도구 사본을 실행하기 위해 전용 컨테이너 안에서만 허용
docker run --rm --network none --cap-drop ALL \
  --security-opt no-new-privileges --read-only --user 65534:65534 \
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=128m --memory 256m --cpus 1 \
  -v "$CONTROL_RUNNER_DIR:/work:ro" \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/work/src \
  "$QUALIFIED_IMAGE" python /work/tests/integration/verify_public_ports.py
```

## 남은 운영 인수

- 양쪽 실제 노드·LB inventory와 허용 목록의 소유자 확인
- 같은 외부 실행기에서의 공개 HTTPS 대조군 및 실제 네트워크 위치 검증
- 승인된 스캔 시간창·전체 예산·취소 연결
- 실제 T10 계획/profile로 결과 보존 및 T19 원본·목적지 대조

코드와 실제 로컬 도구 자격 시험을 양쪽 운영 환경의 PASS로 대신하지 않는다.

## 도구 근거

- [Nmap XML 출력](https://nmap.org/book/output-formats-xml-output.html)
- [Nmap 포트 상태 정의](https://nmap.org/book/man-port-scanning-basics.html)
- [Nmap 포트 범위](https://nmap.org/book/man-port-specification.html)

- [Nmap 속도·시간 제한](https://nmap.org/book/man-performance.html)
