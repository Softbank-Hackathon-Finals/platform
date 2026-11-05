# T39: 시험 컨테이너의 자동 삭제와 정리 확인 경합

2026-11-05 · [T39 #228](https://github.com/Softbank-Hackathon-Finals/platform/issues/228) · [PR #281](https://github.com/Softbank-Hackathon-Finals/platform/pull/281)

## 확인한 원인과 증거 한계

단일 TLS PostgreSQL fixture는 `docker run --rm`과 `OwnedDocker.cleanup()`를 함께 사용했다. Docker의 자동 삭제가 소유 자원 목록 조회와 ID inspect 사이에 끝나면, inspect는 이미 사라진 컨테이너를 조회한다. 이 오류가 원래 `OUTPUT_UNREADABLE`에 `CLEANUP_UNCONFIRMED`를 붙였다. 컨테이너·네트워크 잔여가 0개여도 정리 시험은 실패했다.

실제 Docker에서 생성 응답 유실 뒤 목록 조회 직후 해당 소유 컨테이너만 종료해 이 경합을 재현했다. 자동 삭제를 쓰지 않는 쌍 DB fixture는 같은 종료 조건에서도 통과했다. [Docker의 `--rm` 설명](https://docs.docker.com/reference/cli/docker/container/run/#rm)과 [명시적 컨테이너·익명 볼륨 삭제](https://docs.docker.com/reference/cli/docker/container/rm/#volumes)가 두 삭제 주체의 차이를 설명한다.

이 경합이 [최초 CI 실패 한 건](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41971061338)의 정확한 원인이라고 단정하지 않는다. 당시 오류는 `original_diagnostic_or_cleanup_unconfirmed`였고 중첩 원문은 보존하지 않았다. 기존 생성 응답 유실 시험을 로컬에서 20회 실행했을 때 자연 발생 실패는 없었다. 이 결과는 재시도로 수정 완료를 주장하는 근거가 아니다.

## 수정과 회귀

- 단일 fixture의 `--rm`을 제거했다. 종료된 컨테이너도 기존 소유 label·정확한 ID 확인을 거쳐 `rm -fv`로 정리한다. 익명 볼륨 삭제와 최종 잔여 조회를 유지한다.
- 실제 단일·쌍 fixture에 생성 응답 유실과 목록 조회 직후 종료를 함께 주입한다. 기본 출력과 `--showlocals`를 각각 실행해 비밀 표식 비노출, 원 실패 유지, 종료 주입 실행, 컨테이너·네트워크 잔여 0개를 확인한다.
- 원 진단이 없으면 `original_diagnostic_not_observed`, 정리 미확인이 있으면 `nested_cleanup_unconfirmed`로 구분한다. 원문 stdout/stderr·argv·환경값을 공개하지 않는다.

종료 주입은 같은 소유 label을 다시 확인한 ID만 대상으로 한다. 자동 삭제를 기다리는 제한된 조회는 결함 재현용이며, 자원 생성이나 실패한 시험을 다시 실행하는 로직이 아니다. 제품 판정, 운영 자원, CI 워크플로와 필수 검사 조건은 변경하지 않았다.

## 검증

| 환경·대상 | 결과 |
| --- | --- |
| 로컬, 수정 전 회귀 커밋 `664fbc73a973299986c6539bb91ec6abdd87aa80` | 단일 fixture 2건 실패, 쌍 fixture 2건 통과 |
| GitHub Actions, 동일 수정 전 커밋 | 예상한 두 `nested_cleanup_unconfirmed` 실패, 나머지 299건 통과 |
| 로컬, 최소 수정 후 `scripts/ci_project.py` | Ruff 통과, 전체 301건 통과, skip 0 |
| 수정 후 GitHub Actions | 최종 head별 결과는 PR #281의 Checks와 본문에서 확인 |

[수정 전 CI 원문](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41985918365)에 실패한 두 매개변수와 결과가 남아 있다. 수정 후에도 같은 회귀를 포함한 전체 시험을 실행한다. 이 검증은 로컬 및 CI의 임시 PostgreSQL 시험 하네스에 한정한다. T19 운영 환경 통합이나 제품 전체 E2E의 완료를 뜻하지 않는다.

재현 명령(저장소 루트):

```sh
python3 scripts/ci_project.py services/control-gate python
```

종료 경합만 확인하려면 `services/control-gate`에서 실행한다.

```sh
uv run --locked pytest -q tests/test_fixture_failure_outputs.py -k container_exit_after_list
```

원 실패를 다시 확인할 때는 별도 checkout에서 위 수정 전 커밋을 사용한다. 공유 환경의 기존 컨테이너를 중지하거나 전체 prune을 실행하지 않는다.
