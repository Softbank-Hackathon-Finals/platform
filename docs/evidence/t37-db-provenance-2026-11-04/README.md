# T37 DB 수집 출처 경계 수정 검증

앱 OTLP 경로의 CNPG 이름 지표가 DB 정상 상태로 표시될 수 있던 문제를 수정했다. DB scrape 파이프라인만 `bravo.signal=cnpg-scrape`를 부여하고, 앱 경로는 resource·datapoint의 위조 속성을 제거한다. 조회와 DB 알림은 이 출처와 필요한 식별자를 요구한다. 식별자 없는 인스턴스는 정상으로 표시하지 않는다.

실제 고정 Collector/Prometheus·조회 API에서 CNPG metric 이름·출처·클러스터·Pod·역할 위조를 보내 원시 앱 시계열이 수신됨을 확인한 뒤, 출처가 제거되고 DB 상태가 UNOBSERVABLE인 것을 확인했다. 별도 자체 생성 k3s/CNPG에서는 실제 DB scrape만 신뢰 라벨·클러스터·Pod·역할을 만들고 DB OK와 슬롯 상태를 표시했다. Collector 중단 후에는 기존 DB 표본이 남아도 UNOBSERVABLE이다.

Python95개, compose21개, k3s15개와 promtool 두 묶음이 통과했다. 정확한 수는 result.json에 기록한다. 재현은 services/observability/testing/run.sh와 testing/k8s.sh, deploy/platform/rules/alerts_test.yml이다. 기존 mTLS 거부·디스크 큐·NetworkPolicy 대조군도 포함한다. 원문은 비공개로 보존하고 해시만 공개했다.

이번 시험이 생성한 클러스터·애플리케이션만 정리했다. 기존 자원은 보존한다. 클러스터·애플리케이션은 자체 생성한 로컬 시험 대상이다. 실제 팀 AWS/RKE2·Tailscale ACL이나 운영 DB 배포·보안 인증을 검증한 결과가 아니다. DB namespace의 발견 정보와 Collector·플랫폼 설정은 신뢰 경계이며, 이들을 바꿀 권한을 가진 관리자에 대한 보호를 주장하지 않는다. 기존 앱/팀 클러스터·DB는 변경하지 않았다.
