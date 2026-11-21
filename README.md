# heritage-autoreview

국가유산 라벨 뷰어 텍스트 데이터 자동 검수 (Claude Code CLI 프로젝트). 자세한 배경과 결정 사항은 `docs/총정리.md`.

## 준비
```bash
pip install -r requirements.txt
cp .env.example .env        # HERITAGE_EMAIL, HERITAGE_PASSWORD 입력
python scripts/prepare.py pilot --source human --n 4   # 접속 확인용 작은 실행
```
VS Code에서 이 폴더를 열고 터미널에서 `claude` 를 실행한다. 요금제 계정으로 로그인돼 있어야 요금제 사용량으로 돈다(`ANTHROPIC_API_KEY` 가 설정돼 있으면 API 과금).

## 사용
| 목적 | Claude Code에서 |
|---|---|
| 실험 가동 (정답 표본으로 일치율 측정) | `/pilot --source gold --n 52 --parallel 4` |
| 실제 검수 (미검수 전체, 결과 파일만) | `/review --parallel 10` |
| 일부만 실제 검수 | `/review --limit 26` |
| 질의응답(`_QA_`)만 실제 검수 | `/review --kind qa --set "<QA 세트>"` |
| 중단된 실행 이어하기 | `/resume-run runs/review-YYYYMMDD-HHMMSS` |

결과: `runs/<run>/report.md`, `results.csv`, `results_all.jsonl`

## 사이트 저장 (사람이 직접)
```bash
python scripts/upload.py runs/<run>             # 미리보기
python scripts/upload.py runs/<run> --execute   # 실제 저장 (확인 문구 입력)
```
저장은 로그인 계정의 사람 판정으로 기록된다. 팀 확인 후 사용.

## 정답 표본
`data/gold.example.tsv` 형식대로 `data/gold.tsv` 를 만든다 (data_id, 판정). 지침(`docs/review_guide.md`)대로 사람이 판정한 30~50건 권장.
