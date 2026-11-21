# 감독(supervisor) 절차

감독은 메인 세션이 맡는다. /pilot, /review, /resume-run 명령어가 이 절차를 불러온다.
감독은 직접 데이터를 판정하지 않는다. 묶음을 나누고, 검수 에이전트를 띄우고, 결과를 검증·재시도·집계한다.

검수 종류(`--kind`)에 따라 에이전트가 다르다. 실행 폴더의 `manifest.json` 의 `agent` 값을 쓴다(없으면 text-reviewer).

| kind | 대상 | 에이전트 | 지침 |
|---|---|---|---|
| text (기본) | 이름에 `_QA_` 가 없는 텍스트 청크 | text-reviewer | docs/review_guide.md |
| qa | 이름에 `_QA_` 가 있는 질의응답만. 나머지는 건너뜀 | qa-reviewer | docs/qa_review_guide.md |
| gen | 이름에 `_GEN_` 이 있는 생성형만(세트 `생성형_CL2`). 이미지 문항은 `runs/<run>/images/` 의 줄인 이미지를 에이전트가 Read | gen-reviewer | docs/gen_review_guide.md |

> 감독을 별도 서브에이전트로 두지 않는 이유: 메인 세션이 감독해야 검수 에이전트를 최대 20개까지 직접 병렬로 띄울 수 있고, 진행 상황을 사용자가 보고 중간에 멈출 수 있다. 감독 서브에이전트를 한 층 더 두면 그 층의 토큰이 추가로 들고 진행이 보이지 않는다.

## 1. 준비
- `python scripts/prepare.py ...` 로 실행 폴더(`runs/<run>/`)를 만든다. 데이터 조회·규칙 검사·묶음 나누기는 스크립트가 한다.
- 출력 마지막 줄의 실행 폴더 경로, kind·agent, 묶음 수를 확인한다. 실패하면 오류 메시지를 사용자에게 그대로 전하고 멈춘다(로그인 정보 `.env` 확인 안내).

- 사용량 기록: 에이전트를 띄우기 직전에 `python scripts/usage.py runs/<run> --tag start` 를 실행한다. 출력 한 줄(세션·주간 %)을 사용자에게 전한다. 읽기 실패해도 진행한다.

## 2. 검수 에이전트 실행
- 묶음마다 manifest의 agent(text-reviewer 또는 qa-reviewer) 하나를 백그라운드로 띄운다. 동시 실행 수는 명령어가 정한 값(기본: pilot 4, review 10, 최대 20)을 넘기지 않는다.
- 하나가 끝나면 남은 묶음을 바로 띄워 동시 실행 수를 유지한다.
- 에이전트에게 주는 지시는 아래 형식만 쓴다. 지침 내용을 지시문에 복사하지 않는다(에이전트가 파일에서 읽는다).

```
runs/<run>/batches/batch_NNN.jsonl 묶음을 처리하라.
결과 파일: runs/<run>/results/batch_NNN.jsonl
```

- 에이전트는 `완료`(또는 `실패: 이유`)만 보고한다. 건수·판정 분포는 validate/report 출력으로 확인한다.

## 3. 검증과 재시도
- 에이전트가 끝날 때마다 `python scripts/validate.py runs/<run> --batch NNN` 을 실행한다.
- 실패(파일 없음, 누락 id, 형식 오류, error 판정)면 같은 묶음을 한 번만 다시 띄운다. 두 번째도 실패하면 그 묶음을 실패 목록에 남기고 넘어간다.
- 모든 묶음이 끝나면 `python scripts/validate.py runs/<run>` 로 전체를 한 번 더 확인한다.

## 4. 집계와 보고
- 먼저 `python scripts/usage.py runs/<run> --tag end` 로 사용량을 기록한다.
- `python scripts/report.py runs/<run>` 을 실행한다. 보고서에 이번 실행의 사용량 증가분(세션·주간 %p, 100건당)이 들어간다. `report.md`와 `results.csv`가 만들어진다.
- 사용자에게는 다음만 짧게 전한다: 처리 건수, 판정 분포, 사용량 증가분, (pilot이면) 정답과의 일치율, low 확신도 건수, 실패 묶음, report.md 경로.

## 5. 금지 사항
- 사이트 저장은 사용자가 지시했을 때만 `scripts/upload.py`로 한다(API 직접 호출 금지). 먼저 미리보기를 실행해 건수를 확인한다.
  - 테스트 세트(이름에 "테스트" 포함): `--execute --confirm "UPLOAD N"` 으로 저장한다.
  - 실제 세트: 건수·분포를 보여 주고 사용자 확인을 받은 뒤 `--real` 을 붙인다. 스크립트도 `--real` 없이는 거부한다.
- 판정 기준(docs/review_guide.md, docs/qa_review_guide.md)을 사용자 지시 없이 고치지 않는다.
- 검수 에이전트 결과를 감독이 임의로 고치지 않는다. 이상하면 보고서에 적는다.
- 묶음 데이터와 `images/` 의 이미지를 감독이 직접 읽지 않는다(토큰 절약). 필요하면 report.md와 validate 출력만 본다.
