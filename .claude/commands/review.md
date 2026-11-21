---
description: 실제 검수 — 미검수 데이터 전체(또는 일부)를 자동 판정해 결과 파일을 만든다 (사이트 저장은 별도 단계)
argument-hint: "[--kind text|qa|gen] [--limit N] [--offset N] [--parallel 10] [--batch-size 26] [--set 텍스트 16차]"
---
실제 검수(review)를 감독한다. 인자: $ARGUMENTS

1. `docs/supervisor.md` 를 읽고 그 절차를 따른다.
2. 시작 전에 사용자에게 한 줄로 확인받는다: 대상 건수(인자가 없으면 미검수 전체), 묶음 수, 동시 실행 수, `docs/총정리.md` 7장 기준 예상 토큰. 사용자가 이미 이 실행을 지시하며 숫자를 정했다면 다시 묻지 않는다.
3. 준비: `python scripts/prepare.py review $ARGUMENTS`
   - `--kind qa`: 이름에 `_QA_` 가 있는 질의응답만 검수하고 나머지는 건너뛴다. `--limit` 은 질의응답 건수 기준이다.
   - `--kind gen --set 생성형_CL2`: 이름에 `_GEN_` 이 있는 생성형만. 이미지 문항은 원본을 줄여 `runs/<run>/images/` 에 받는다. `--per-subtype N` 이면 미검수 중 이미지·텍스트 문항을 무작위로 N건씩(시험용).
   - 기본(`--kind text`): 텍스트 청크만. `_QA_`·`_GEN_` 데이터는 건너뛴다.
4. 검수 에이전트는 manifest의 `agent`(text-reviewer, qa-reviewer, gen-reviewer)를 쓴다. 동시 실행 수는 `--parallel` 값(기본 10, 최대 20)을 따른다.
5. 끝나면 `python scripts/report.py runs/<run>` 을 실행하고 판정 분포, low 확신도 건수, 실패 묶음, report.md 경로를 전한다.
6. 사용자가 저장까지 지시했으면 `docs/supervisor.md` 5장 규칙대로 저장한다. 지시가 없으면 아래 명령만 안내한다.
   `python scripts/upload.py runs/<run>` (미리보기) → `--execute --confirm "UPLOAD N"` (테스트 세트) / `--real` 추가 (실제 세트, 사용자 확인 후)
