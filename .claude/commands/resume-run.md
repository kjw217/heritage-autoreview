---
description: 중단된 실행 이어하기 — 결과가 없거나 검증에 실패한 묶음만 다시 검수한다
argument-hint: "runs/<run> [--parallel 10]"
---
중단된 실행을 이어서 감독한다. 인자: $ARGUMENTS

1. `docs/supervisor.md` 를 읽고 그 절차를 따른다(1. 준비 단계는 건너뛴다).
2. `python scripts/validate.py <run 폴더>` 로 실패·누락 묶음 목록을 얻는다.
3. 띄우기 전에 `python scripts/usage.py <run 폴더> --tag start`, 모두 끝나면 `--tag end` 를 실행한다(사용량 기록).
   그 묶음만 manifest의 `agent`(없으면 text-reviewer)로 다시 검수한다. 동시 실행 수는 `--parallel`(기본 10).
4. `python scripts/report.py <run 폴더>` 로 보고서를 다시 만들고 결과를 짧게 전한다.
