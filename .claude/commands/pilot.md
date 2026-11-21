---
description: 실험 가동 — 정답이 있는 표본으로 자동 검수를 돌려 일치율을 잰다 (사이트 저장 없음)
argument-hint: "[--kind text|qa|gen] [--n 52] [--source gold|human] [--parallel 4] [--set 텍스트 16차]"
---
실험 가동(pilot)을 감독한다. 인자: $ARGUMENTS

1. `docs/supervisor.md` 를 읽고 그 절차를 따른다.
2. 준비: 아래를 실행한다. 인자에 없는 값은 기본값을 쓴다.
   `python scripts/prepare.py pilot $ARGUMENTS`
   - `--source gold`(기본): `data/gold.tsv`(사람이 지침대로 판정한 정답)에서 표본을 만든다. 파일이 없으면 오류가 나므로, 그때는 사용자에게 `--source human`으로 바꿀지 묻는다.
   - `--source human`: 사이트에 사람이 저장한 판정에서 판정별 같은 수로 표본을 뽑는다. 이 판정은 지침과 기준이 다를 수 있다(docs/총정리.md 4장).
   - `--kind qa`: 이름에 `_QA_` 가 있는 질의응답만 표본으로 쓴다(정답 파일·사람 판정 모두 같은 필터). 에이전트는 qa-reviewer.
3. 검수 에이전트는 manifest의 `agent` 를 쓴다. 동시 실행 수는 `--parallel` 값(기본 4)을 따른다.
4. 끝나면 `python scripts/report.py runs/<run>` 을 실행하고, 일치율·혼동행렬·불일치 상위 사례를 사용자에게 짧게 전한다.
5. 사이트에 저장하지 않는다.
