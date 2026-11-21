"""이미지 실검수 결과 반영 준비·라벨 저장.

  /opt/miniconda3/bin/python scripts/imgapply.py runs/<run>             미리보기
  /opt/miniconda3/bin/python scripts/imgapply.py runs/<run> --execute   추가 객체를 사이트 라벨에 저장 + results_all.jsonl 작성

입력: results/*.jsonl, proposed.json(imgmask.py 출력), verify.jsonl(확인 에이전트: {"data_id","keep":[번호...],"note"})
- 확인에서 빠진 추가 객체가 있으면 그 건은 hold("검출 영역이 부정확")로 바꾸고 라벨은 남은 객체만 저장한다.
- 추가 객체가 하나도 남지 않으면 라벨은 저장하지 않는다.
- 새 객체끼리 겹치면 앞 객체를 뺀다. 라벨 저장 기록은 label_log.jsonl.
- 판정 저장은 이 스크립트가 아니라 scripts/upload.py 로 한다(results_all.jsonl, skip 은 upload 가 건너뜀).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, Site, read_jsonl  # noqa: E402
from imgmask import rle_encode  # noqa: E402
from qscore import rle_mask  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args()
    import numpy as np
    run = ROOT / a.run
    man = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    rows = [r for f in sorted((run / "results").glob("*.jsonl")) for r in read_jsonl(f)]
    prop = json.loads((run / "proposed.json").read_text(encoding="utf-8")) if (run / "proposed.json").exists() else {}
    ver = {r["data_id"]: r for r in read_jsonl(run / "verify.jsonl")} if (run / "verify.jsonl").exists() else {}
    site = Site() if a.execute else None
    if site:
        site.login()
    log = open(run / "label_log.jsonl", "a", encoding="utf-8") if a.execute else None
    out, n_lab, n_obj = [], 0, 0
    for r in rows:
        did = r["data_id"]
        if r.get("add_objects") and r["verdict"] != "skip":
            new = prop.get(did) or []
            v = ver.get(did)
            keep = set(v["keep"]) if v else set()
            kept = [o for i, o in enumerate(new, 1) if i in keep]
            if len(kept) < len(r["add_objects"]):
                r = dict(r, verdict="hold", reasons=sorted(set((r.get("reasons") or []) + ["검출 영역이 부정확"])),
                         comment=((r.get("comment") or "") + " / 자동 객체 추가 일부 실패: " + (v.get("note", "") if v else "확인 없음")).strip(" /"))
            if kept and a.execute:
                assert site is not None and log is not None
                full = site.label_full(man["set"], did)
                lab, sha = full["label"], (full.get("rev") or {}).get("sha", "")
                taken, objs = None, []
                for o in kept:
                    m = rle_mask(o["mask"]["data"])
                    taken = np.zeros_like(m) if taken is None else taken
                    m = m & (1 - taken)
                    taken |= m
                    if m.sum() < 500:
                        continue
                    ys, xs = np.nonzero(m)
                    objs.append({"class": o["class"], "bbox": [int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1),
                                                               int(ys.max() - ys.min() + 1)],
                                 "mask": {"format": "rle", "data": rle_encode(m)}, "confidence": o["confidence"]})
                before = lab.get("objects") or []
                resp = site.s.post(f"{site.base}/api/label/{did}/objects",
                                   json={"set": man["set"], "objects": before + objs, "base_sha": sha, "scene_no": None}, timeout=120)
                ok = resp.status_code < 400
                log.write(json.dumps({"data_id": did, "status": resp.status_code, "prev_sha": sha, "prev_objects": len(before),
                                      "added": [o["class"] for o in objs], "at": time.time()}, ensure_ascii=False) + "\n")
                if not ok:
                    print(f"  {did} 라벨 저장 실패 {resp.status_code} {resp.text[:120]} → error")
                    r = dict(r, verdict="error")
                else:
                    n_lab += 1
                    n_obj += len(objs)
            elif kept:
                print(f"  {did}: 추가 {[o['class'] for o in kept]} (미리보기)")
        out.append(r)
    from collections import Counter
    print(f"판정 {Counter(r['verdict'] for r in out)} · 라벨 저장 {n_lab}건 객체 {n_obj}개")
    if a.execute:
        (run / "results_all.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out), encoding="utf-8")


if __name__ == "__main__":
    main()
