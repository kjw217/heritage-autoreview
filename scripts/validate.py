"""결과 검증: 묶음별 결과 파일이 있는지, 모든 id가 있는지, 형식이 맞는지 확인한다.

  python scripts/validate.py runs/<run>              전체 묶음
  python scripts/validate.py runs/<run> --batch 007  한 묶음
종료 코드: 0 = 모두 통과, 1 = 실패 묶음 있음.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CONFIDENCE, REASONS, REQUIRED, VERDICTS  # noqa: E402


def check_batch(run: Path, b: dict) -> list[str]:
    errs: list[str] = []
    p = run / "results" / f"{b['name']}.jsonl"
    if not p.exists():
        return ["결과 파일 없음"]
    got: dict[str, dict] = {}
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            errs.append(f"{n}행 JSON 아님")
            continue
        miss = [k for k in REQUIRED if k not in r]
        if miss:
            errs.append(f"{n}행 필드 누락 {miss}")
        if r.get("verdict") not in VERDICTS:
            errs.append(f"{n}행 verdict 값 오류 {r.get('verdict')!r}")
        if r.get("verdict") == "error":
            errs.append(f"{r.get('data_id')} error 판정: {r.get('comment', '')[:60]}")
        if r.get("verdict") in ("reject", "hold") and not r.get("reasons"):
            errs.append(f"{n}행 {r.get('verdict')} 인데 reasons 없음")
        bad = [x for x in r.get("reasons") or [] if x not in REASONS]
        if bad:
            errs.append(f"{n}행 사유 목록 밖 {bad}")
        if r.get("confidence") not in CONFIDENCE and r.get("verdict") != "error":
            errs.append(f"{n}행 confidence 값 오류 {r.get('confidence')!r}")
        got[r.get("data_id")] = r
    want = set(b["ids"])
    missing = want - set(got)
    extra = set(got) - want
    if missing:
        errs.append(f"누락 {len(missing)}건: {sorted(missing)[:5]}")
    if extra:
        errs.append(f"묶음 밖 id {len(extra)}건: {sorted(extra)[:5]}")
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--batch", help="묶음 번호(예: 007) 또는 이름(batch_007)")
    ap.add_argument("--json", action="store_true", help="기계용 JSON 출력")
    a = ap.parse_args()
    run = Path(a.run)
    m = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    batches = m["batches"]
    if a.batch:
        name = a.batch if a.batch.startswith("batch_") else f"batch_{int(a.batch):03d}"
        batches = [b for b in batches if b["name"] == name]
        if not batches:
            sys.exit(f"묶음 없음: {name}")
    report = {b["name"]: check_batch(run, b) for b in batches}
    failed = {k: v for k, v in report.items() if v}
    if a.json:
        print(json.dumps({"ok": len(report) - len(failed), "failed": failed}, ensure_ascii=False))
    else:
        print(f"통과 {len(report) - len(failed)} / {len(report)} 묶음")
        for k, v in failed.items():
            print(f"- {k}: " + " | ".join(v[:4]))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
