# AWS 비용 경보 모듈

월 지원금(KRW), 고정한 KRW/USD 환율, 수신자를 받아 계정 전체의 월 COST 예산 한 개를 만든다. 실제 비용 80%·100%, 예상 비용 100% 초과 시 이메일 알림을 설정한다. USD 한도는 지원금 ÷ 환율을 센트 단위로 내림한다. 자동 차단·자원 종료 Budget Action은 만들지 않는다.

모듈에는 provider/backend가 없다. 실행 루트는 [`envs/cost-aws`](../../../envs/cost-aws), 정책과 검증 절차도 그곳에서 관리한다. Cost Explorer의 UnblendedCost와 같이 credit/refund/tax 등을 포함하며 amortized/blended 집계는 사용하지 않는다. 알림은 즉시 비용 차단을 보장하지 않는다.

```sh
terraform init -backend=false
terraform validate
terraform test
```

모의 시험은 환산·경보 세 종류·계정 전체 범위·잘못된 계정/금액/환율/주소 입력을 검증한다. 메일 수신 및 AWS 평가 주기의 런타임 시험을 대신하지 않는다.
