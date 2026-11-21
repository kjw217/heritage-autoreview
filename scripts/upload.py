"""판정 결과를 사이트에 저장한다. 사용자가 저장을 지시했을 때만 실행한다.

  python scripts/upload.py runs/<run>                    미리보기 (저장하지 않음, 기본)
  python scripts/upload.py runs/<run> --execute          실제 저장 (확인 문구 입력 필요)
  python scripts/upload.py runs/<run> --execute --confirm "UPLOAD N"   확인 문구를 인자로 (비대화형)
  실제 세트(이름에 '테스트'가 없는 세트)는 --real 이 있어야 저장한다 (사용자 확인 후).
  옵션: --include-low (low 확신도도 저장) --only approve,reject (특정 판정만)
        --low-as-hold (low 확신도는 보류로 바꿔 저장. 사유는 유지, 코멘트에 원판정 표시)
        --high-only (high 확신도만 저장. --include-low·--low-as-hold 와 함께 쓸 수 없다)

주의
- 사이트는 이 저장을 로그인 계정의 "사람 판정"(by: human)으로 기록한다.
  자동 판정을 사람 판정으로 올려도 되는지 팀 확인 후 사용한다.
- 이미 판정이 있는 건은 건너뛴다(덮어쓰지 않는다). --overwrite 를 주면 덮어쓰고 이전 판정을 로그에 남긴다.
- 기본값으로 low 확신도와 error 판정은 저장하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Site, read_jsonl  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--include-low", action="store_true")
    ap.add_argument("--low-as-hold", action="store_true", help="low 확신도 건을 보류(hold)로 바꿔 저장")
    ap.add_argument("--high-only", action="store_true", help="high 확신도 건만 저장")
    ap.add_argument("--only", default="approve,reject,hold")
    ap.add_argument("--sleep", type=float, default=0.2, help="요청 간격(초)")
    ap.add_argument("--confirm", help="확인 문구를 인자로 넘긴다 (예: \"UPLOAD 475\"). 없으면 입력받는다")
    ap.add_argument("--real", action="store_true", help="세트 이름에 '테스트'가 없는 실제 세트에 저장할 때 필요")
    ap.add_argument("--overwrite", action="store_true", help="이미 판정이 있는 건도 덮어쓴다(재검 수정용). 이전 판정은 upload_log 에 남긴다")
    a = ap.parse_args()
    if a.high_only and (a.include_low or a.low_as_hold):
        sys.exit("--high-only 는 --include-low·--low-as-hold 와 함께 쓸 수 없습니다.")
    run = Path(a.run)
    m = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    if m["mode"] != "review":
        sys.exit("pilot 실행 결과는 업로드하지 않습니다 (실험용).")
    allowed = set(a.only.split(","))
    rows: list[dict] = read_jsonl(run / "results_all.jsonl")
    if a.low_as_hold:
        rows = [dict(r, verdict="hold", comment=f"자동 검수 확신도 low (원판정 {r['verdict']}) - 사람 확인 필요")
                if r.get("confidence") == "low" and r["verdict"] != "error" else r for r in rows]
        a.include_low = True
    todo =[r for r in rows if r["verdict"] in allowed and (a.include_low or r.get("confidence") != "low")]
    if a.high_only:
        todo = [r for r in todo if r.get("confidence") == "high"]
    skipped = len(rows) - len(todo)
    print(f"대상 {len(todo)}건 (제외 {skipped}건: {'high 외 확신도' if a.high_only else 'low 확신도'}·error·제외 판정) · 분포 {dict(Counter(r['verdict'] for r in todo))}")
    if not a.execute:
        for r in todo[:10]:
            print(f"  {r['data_id']} → {r['verdict']} {r.get('reasons')} {r.get('comment', '')[:40]}")
        print("미리보기만 했습니다. 실제 저장은 --execute 를 붙이세요.")
        return
    phrase = f"UPLOAD {len(todo)}"
    if "테스트" not in m["set"] and not a.real:
        sys.exit(f"실제 세트({m['set']})입니다. 사용자 확인을 받은 뒤 --real 을 붙여 실행하세요.")
    if a.confirm is not None:
        answer = a.confirm
    else:
        answer = input(f"사이트에 {len(todo)}건을 로그인 계정의 판정으로 저장합니다. 계속하려면 '{phrase}' 입력: ")
    if answer.strip() != phrase:
        sys.exit(f"취소했습니다. (확인 문구는 '{phrase}')")
    site = Site()
    site.login()
    log = open(run / "upload_log.jsonl", "a", encoding="utf-8")
    done = exist = fail = 0
    for r in todo:
        try:
            cur = site.review(m["set"], r["data_id"])
            prev = cur.get("verdict")
            if prev and not a.overwrite:
                exist += 1
                continue
            site.post_review(m["set"], r["data_id"], r["verdict"], r.get("reasons") or [], r.get("comment") or "")
            done += 1
            log.write(json.dumps({"data_id": r["data_id"], "verdict": r["verdict"], "prev": prev or None, "prev_reasons": cur.get("reasons") if prev else None, "at": time.time()}, ensure_ascii=False) + "\n")
        except Exception as e:
            fail += 1
            print(f"  실패 {r['data_id']}: {e}")
        time.sleep(a.sleep)
    log.close()
    print(f"저장 {done} · 이미 판정 있어 건너뜀 {exist} · 실패 {fail}")


if __name__ == "__main__":
    main()
