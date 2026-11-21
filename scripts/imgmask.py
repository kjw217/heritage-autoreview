"""이미지 라벨 문양 객체 추가 시험 — 에이전트가 정한 박스로 사이트 SAM3(/api/sam3-box) 마스크를 받아
박스 안으로 자르고, 겹친 그림을 만든다. 사이트에 저장하지 않는다(읽기·계산만).

  /opt/miniconda3/bin/python scripts/imgmask.py runs/imgmask-test [--set "이미지 15차"]

입력: <run>/boxes.jsonl  {"id", "regions":[{"class", "box":[x0,y0,x1,y1] 또는 "boxes":[[..],[..]], "desc"}]}  (좌표는 1024px 원본 그림 기준)
  - "boxes" 를 주면 박스마다 SAM3 를 따로 돌려 각 박스 안만 남기고 합쳐 객체 하나(마스크 하나)로 만든다.
    이어진 대상(전각의 지붕·몸체·기둥·기단 등)을 부분별 박스로 덮어 마스크가 끊기지 않게 할 때 쓴다.
출력: <run>/proposed.json (건별 새 문양 객체), <run>/images/<id>_new.jpg (새 문양만 강조), <id>_all.jpg (기존+새)
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, Site, read_jsonl  # noqa: E402
from qscore import render, rle_mask  # noqa: E402


def rle_encode(m) -> str:
    """열 우선 0/1 마스크 → COCO 압축 RLE 문자열 'HxW:counts' (사이트 형식)."""
    import numpy as np
    h, w = m.shape
    flat = m.T.reshape(-1).astype(np.uint8)
    if flat.size == 0:
        cnts = [0]
    else:  # 값이 바뀌는 지점으로 구간 길이를 한 번에 센다(0 부터 시작, 첫 값이 1 이면 앞에 0 구간)
        idx = np.flatnonzero(np.diff(flat)) + 1
        cnts = np.diff(np.concatenate(([0], idx, [flat.size]))).tolist()
        if flat[0] == 1:
            cnts = [0] + cnts
    out = []
    for i, x in enumerate(cnts):
        if i > 2:
            x -= cnts[i - 2]
        more = True
        while more:
            c = x & 0x1f
            x >>= 5
            more = (x != -1) if (c & 0x10) else (x != 0)
            if more:
                c |= 0x20
            out.append(chr(c + 48))
    return f"{h}x{w}:" + "".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--set", default="이미지 15차")
    a = ap.parse_args()
    import numpy as np
    from PIL import Image
    run = ROOT / a.run
    (run / "images").mkdir(exist_ok=True)
    site = Site()
    site.login()
    proposed = {}
    if (run / "boxes.jsonl").exists():
        rows = read_jsonl(run / "boxes.jsonl")
    else:  # 검수 실행: results/*.jsonl 의 add_objects 를 쓴다
        rows = []
        for f in sorted((run / "results").glob("*.jsonl")):
            for r in read_jsonl(f):
                if r.get("add_objects") and r.get("verdict") != "skip":
                    rows.append({"id": r["data_id"], "regions": r["add_objects"]})
    for row in rows:
        did = row["id"]
        lab = site.label(a.set, did)
        LW, LH = lab["width"], lab["height"]
        img = site.media(a.set, did)
        W, H = Image.open(io.BytesIO(img)).size
        k = max(W, H) / 1024 if max(W, H) > 1024 else 1.0
        sx, sy = LW / W * k, LH / H * k  # 1024 그림 → 라벨 좌표
        new = []
        for rg in row["regions"]:
            # 객체 하나 = 박스 여러 개(부분별)의 SAM3 결과를 각 박스로 자른 뒤 합친 마스크 하나
            parts = rg.get("boxes") or [rg["box"]]
            clip, inbox, raw, scores, boxarea, fails = None, None, 0, [], 0, 0
            for pb in parts:
                x0, y0, x1, y1 = pb
                box = [max(0, round(x0 * sx)), max(0, round(y0 * sy)), min(LW, round(x1 * sx)), min(LH, round(y1 * sy))]
                if box[2] <= box[0] or box[3] <= box[1]:
                    continue
                r = site.s.post(f"{site.base}/api/sam3-box", json={"data_id": did, "set": a.set, "box": box, "scene_no": None},
                                timeout=300)
                if r.status_code >= 400:
                    print(f"  {did} {rg.get('desc')}: SAM3 실패 {r.status_code} {r.text[:120]}")
                    fails += 1
                    continue
                out = r.json()
                m = rle_mask(out["rle"])
                raw += int(m.sum())
                if clip is None or inbox is None:
                    clip = np.zeros_like(m)
                    inbox = np.zeros(m.shape, dtype=bool)
                clip[box[1]:box[3], box[0]:box[2]] |= m[box[1]:box[3], box[0]:box[2]]
                inbox[box[1]:box[3], box[0]:box[2]] = True
                scores.append(float(out.get("score") or 0.9))
            if clip is None or inbox is None:
                continue
            boxarea = int(inbox.sum())  # 박스들을 합친 영역 넓이(겹침 한 번만)
            area = int(clip.sum())
            print(f"  {did} {rg.get('desc')}: 박스 {len(parts)}개 · SAM 면적 {raw:,} → 합친 마스크 {area:,} "
                  f"({area / max(boxarea, 1):.0%} of box){' · 실패 ' + str(fails) if fails else ''}")
            if area == 0:
                continue
            out = {"score": min(scores)}
            ys, xs = np.nonzero(clip)
            bb = [int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)]
            new.append({"class": rg.get("class") or "문양", "bbox": bb, "mask": {"format": "rle", "data": rle_encode(clip)},
                        "confidence": round(float(out.get("score") or 0.9), 3), "_desc": rg.get("desc")})
        proposed[did] = new
        only = dict(lab, objects=[{k2: v for k2, v in o.items() if k2 != "_desc"} for o in new])
        _, ov = render(img, only, run / "images", did)
        Path(ov).rename(run / "images" / f"{did}_new.jpg")
        allv = dict(lab, objects=(lab.get("objects") or []) + only["objects"])
        _, ov = render(img, allv, run / "images", did)
        Path(ov).rename(run / "images" / f"{did}_all.jpg")
        # 건마다 바로 저장(중간에 끊겨도 앞의 결과가 남게)
        (run / "proposed.json").write_text(json.dumps(proposed, ensure_ascii=False), encoding="utf-8")
    if not rows:
        print("추가할 객체 없음")
    (run / "proposed.json").write_text(json.dumps(proposed, ensure_ascii=False), encoding="utf-8")
    print("새 문양 객체:", {d: len(v) for d, v in proposed.items()})


if __name__ == "__main__":
    main()
