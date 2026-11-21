"""이미지 세트(라벨 뷰어) 자동 검수 — 시험 가동용. 기준: docs/img_review_guide.md

  /opt/miniconda3/bin/python scripts/imgreview.py pilot-prepare --n 50 --max-obj 2 [--set "이미지 15차"] [--batch 5] [--seed 1]
  /opt/miniconda3/bin/python scripts/imgreview.py human-pilot-prepare --reviewer <이름> [--since 2026-11-21] [--prefix GBG] [--n 50]
  /opt/miniconda3/bin/python scripts/imgreview.py check runs/<run>

- pilot-prepare: 사람이 판정한 건 중 객체 수 1~max-obj 개를 보강 가능 절반·보류/불가 절반으로 뽑아
  겹친 그림(박스·마스크)과 원본을 runs/<run>/images/ 에 만들고 묶음을 쓴다. 사람 판정은 gold.json 에 따로 둔다.
- human-pilot-prepare: 특정 검수자가 since 이후 판정한 건을 처리 유형(수정 없이 승인·객체 추가 후 승인·문양 추가 후 승인·
  마스크 수정 후 승인·보류)별로 나눠 뽑는다. 라벨은 그 사람이 고치기 **전** 판본(/api/label/{id}/revision/{rev id})으로 그린다.
  gold.json 에 사람 판정·유형·클래스별 증감을 둔다. 좌표 읽기용 grid 그림도 만든다.
- check: 결과 검증 + 사람 판정과 일치율. 사이트 저장 없음.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, Site, read_jsonl  # noqa: E402
from qscore import FONT, render  # noqa: E402

ADD_CLASSES = {"전각", "궁문", "석조물", "현판", "한자", "정원"}  # 문양은 직접 추가하지 않는다(사용자 지시 11/19)
IMG_REASONS = {"검출 영역이 부정확", "클래스 분류 오류", "한자·자막 오독", "설명이 사실과 다름", "학습에 부적합한 원천"}


def compact(lab: dict) -> dict:
    objs = []
    for i, o in enumerate(lab.get("objects") or []):
        x = {"no": i + 1, "class": o.get("class"), "bbox_xywh": o.get("bbox"),
             "has_mask": bool((o.get("mask") or {}).get("data"))}
        if o.get("ocr"):
            x["ocr"] = {k: o["ocr"].get(k) for k in ("text", "reading")}
        objs.append(x)
    return {"title": lab.get("title"), "heritage_name": lab.get("heritage_name"), "palace": lab.get("palace"),
            "place": lab.get("class_code_path"), "image_caption": lab.get("image_caption"),
            "description": lab.get("description"), "tags": lab.get("tags"),
            "label_size": [lab.get("width"), lab.get("height")], "objects": objs}


def pilot_prepare(a):
    site = Site()
    site.login()
    items, _ = site.list_ids(a.set, "human")
    pool = [it for it in items if 1 <= (it.get("objects") or 0) <= a.max_obj and it.get("has_media")]
    rnd = random.Random(a.seed)
    good = [it for it in pool if it.get("verdict") == "approve"]
    bad = [it for it in pool if it.get("verdict") in ("hold", "reject")]
    pick = rnd.sample(good, min(a.n // 2, len(good))) + rnd.sample(bad, min(a.n - a.n // 2, len(bad)))
    rnd.shuffle(pick)
    run = ROOT / "runs" / f"imgpilot-{datetime.now():%Y%m%d-%H%M%S}"
    for d in ("images", "batches", "results"):
        (run / d).mkdir(parents=True)

    def one(it):
        did = it["data_id"]
        try:
            lab = site.label(a.set, did)
            rv = site.review(a.set, did)
            rv = rv.get("review") or rv
            orig, ov = render(site.media(a.set, did), lab, run / "images", did)
            row = {"id": did, **compact(lab), "overlay_image": str(Path(ov).relative_to(ROOT)),
                   "orig_image": str(Path(orig).relative_to(ROOT)), "rule_flags": []}
        except Exception as e:
            print(f"  {did} 실패: {e}")
            return None
        return row, {"verdict": it["verdict"], "reasons": rv.get("reasons") or [], "comment": rv.get("comment") or ""}

    with ThreadPoolExecutor(8) as ex:
        res = [r for r in ex.map(one, pick) if r]
    gold = {r[0]["id"]: r[1] for r in res}
    (run / "gold.json").write_text(json.dumps(gold, ensure_ascii=False, indent=1), encoding="utf-8")
    rows = [r[0] for r in res]
    batches = []
    for i in range(0, len(rows), a.batch):
        b = f"batch_{i // a.batch + 1:03d}"
        (run / "batches" / f"{b}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows[i:i + a.batch]), encoding="utf-8")
        batches.append(b)
    man = {"kind": "img", "set": a.set, "mode": "pilot", "n": len(rows), "max_obj": a.max_obj, "seed": a.seed,
           "created": datetime.now().isoformat(timespec="seconds"), "batches": batches}
    (run / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{run.relative_to(ROOT)} · 대상 {len(rows)} · 묶음 {len(batches)}개 · 사람 판정 {Counter(g['verdict'] for g in gold.values())}")


def grid_image(orig: str, out: Path) -> str:
    """원본 위에 100px 격자와 좌표 숫자를 그린다(add_objects 박스 좌표를 읽는 용도)."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(orig).convert("RGB")
    d = ImageDraw.Draw(im)
    W, H = im.size
    try:
        font = ImageFont.truetype(FONT, 15)
    except Exception:
        font = ImageFont.load_default()
    for x in range(100, W, 100):
        d.line([(x, 0), (x, H)], fill=(255, 255, 0), width=1)
        d.text((x + 2, 2), str(x), fill=(255, 255, 0), font=font, stroke_width=2, stroke_fill=(0, 0, 0))
    for y in range(100, H, 100):
        d.line([(0, y), (W, y)], fill=(255, 255, 0), width=1)
        d.text((2, y + 2), str(y), fill=(255, 255, 0), font=font, stroke_width=2, stroke_fill=(0, 0, 0))
    im.save(out, quality=88)
    return str(out)


def human_edit(revs: list[dict], reviewer: str) -> tuple[int | None, dict | None]:
    """reviewer 가 처음 저장하기 직전 판본 id 와 그 뒤 클래스별 객체 수 증감. 저장이 없으면 (None, None)."""
    revs = sorted(revs or [], key=lambda x: x["rev_no"])
    saves = [x for x in revs if x["action"] == "save" and x.get("actor") == reviewer]
    if not saves:
        return None, None
    prev = [x for x in revs if x["rev_no"] < saves[0]["rev_no"]]
    pb = (prev[-1]["summary"] or {}).get("by_class") or {} if prev else {}
    cur = (revs[-1]["summary"] or {}).get("by_class") or {}
    diff = {k: cur.get(k, 0) - pb.get(k, 0) for k in set(cur) | set(pb) if cur.get(k, 0) != pb.get(k, 0)}
    return (prev[-1]["id"] if prev else None), diff


def human_category(verdict: str, diff: dict | None) -> str:
    if verdict != "approve":
        return verdict
    if diff is None:
        return "approve_noedit"
    if not diff:
        return "approve_maskfix"
    if diff.get("문양", 0) > 0:
        return "approve_add_munyang"
    return "approve_add"


def human_pilot_prepare(a):
    site = Site()
    site.login()
    if a.since:
        start = datetime.strptime(a.since, "%Y-%m-%d").astimezone()
    else:
        start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    since = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prefixes = tuple(p for p in (a.prefix or "").split(",") if p)
    items, _ = site.list_ids(a.set, "")
    cand = [it for it in items if it.get("verdict") and (it.get("reviewed_at") or "") >= since and it.get("has_media")
            and (not prefixes or it["data_id"].startswith(prefixes))]

    def hist(it):
        did = it["data_id"]
        h = site._get(f"/api/label/{did}/history", set=a.set)
        last = max((r for r in h.get("reviews") or [] if r.get("verdict")), key=lambda r: r.get("at") or "", default=None)
        if not last or last.get("reviewer") != a.reviewer:
            return None
        rid, diff = human_edit(h.get("revisions") or [], a.reviewer)
        return {"id": did, "verdict": last["verdict"], "reasons": last.get("reasons") or [], "comment": last.get("comment") or "",
                "base_rev": rid, "class_diff": diff, "category": human_category(last["verdict"], diff)}

    with ThreadPoolExecutor(8) as ex:
        judged = [g for g in ex.map(hist, cand) if g]
    groups: dict[str, list[dict]] = defaultdict(list)
    for g in judged:
        groups[g["category"]].append(g)
    total = len(judged)
    if not total:
        sys.exit("조건에 맞는 사람 판정이 없습니다.")
    quota = {c: min(len(v), max(a.min_per, round(a.n * len(v) / total))) for c, v in groups.items()}
    while sum(quota.values()) > a.n:
        quota[max(quota, key=lambda c: quota[c])] -= 1
    rnd = random.Random(a.seed)
    pick = [g for c in sorted(groups) for g in rnd.sample(groups[c], quota[c])]
    rnd.shuffle(pick)
    run = ROOT / "runs" / f"imghpilot-{datetime.now():%Y%m%d-%H%M%S}"
    for d in ("images", "batches", "results"):
        (run / d).mkdir(parents=True)

    def one(g):
        did = g["id"]
        try:
            if g["base_rev"]:
                lab = site._get(f"/api/label/{did}/revision/{g['base_rev']}", set=a.set)["label"]
            else:
                lab = site.label(a.set, did)
            orig, ov = render(site.media(a.set, did), lab, run / "images", did)
            gr = grid_image(orig, run / "images" / f"{did}_grid.jpg")
            return {"id": did, **compact(lab), "overlay_image": str(Path(ov).relative_to(ROOT)),
                    "orig_image": str(Path(orig).relative_to(ROOT)), "grid_image": str(Path(gr).relative_to(ROOT)),
                    "rule_flags": []}
        except Exception as e:
            print(f"  {did} 실패: {e}")
            return None

    with ThreadPoolExecutor(8) as ex:
        rows = [r for r in ex.map(one, pick) if r]
    ok = {r["id"] for r in rows}
    gold = {g["id"]: {k: g[k] for k in ("verdict", "reasons", "comment", "category", "class_diff")} for g in pick if g["id"] in ok}
    (run / "gold.json").write_text(json.dumps(gold, ensure_ascii=False, indent=1), encoding="utf-8")
    batches = []
    for i in range(0, len(rows), a.batch):
        b = f"batch_{i // a.batch + 1:03d}"
        (run / "batches" / f"{b}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows[i:i + a.batch]), encoding="utf-8")
        batches.append(b)
    man = {"kind": "img", "set": a.set, "mode": "pilot", "source": "human-today", "reviewer": a.reviewer, "since": since,
           "prefix": a.prefix, "n": len(rows), "seed": a.seed, "guide": "i9",
           "created": datetime.now().isoformat(timespec="seconds"), "batches": batches}
    (run / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{run.relative_to(ROOT)} · 후보 {total}건 {dict(Counter(g['category'] for g in judged))}")
    print(f"뽑음 {len(rows)}건 {dict(Counter(gold[d]['category'] for d in gold))} · 묶음 {len(batches)}개")


def review_prepare(a):
    """미검수(pending) 건을 객체 적은 순(같으면 data_id 순)으로 n건 골라 묶음을 만든다."""
    site = Site()
    site.login()
    items, _ = site.list_ids(a.set, "pending")
    items = [it for it in items if it.get("has_media") and (it.get("objects") or 0) <= a.max_obj]
    pre = tuple(x for x in a.prefix.split(",") if x)
    if pre:
        items = [it for it in items if it["data_id"].startswith(pre)]
    skipped = set()  # 앞선 실검수에서 문양 때문에 건너뛴 건은 다시 고르지 않는다
    for f in (ROOT / "runs").glob("imgreview-*/results/*.jsonl"):
        skipped |= {r["data_id"] for r in read_jsonl(f) if r.get("verdict") == "skip"}
    if not (a.include_skipped or a.skipped_last):
        items = [it for it in items if it["data_id"] not in skipped]
    for c in [x for x in (a.include_cls or "").split(",") if x]:  # 이 클래스 객체가 있는 건만 남긴다
        d = site._get("/api/labels", set=a.set, type="", status="", palace="", q="", review="pending", sample=0, cls=c,
                      sort="data_id", order="asc", only_multi=0, part="", offset=0, limit=100000)
        keep = {i["data_id"] for i in d["items"]}
        items = [it for it in items if it["data_id"] in keep]
    for c in [x for x in (a.exclude_cls or "").split(",") if x]:  # 이 클래스 객체가 있는 건은 뺀다(목록 cls 필터)
        d = site._get("/api/labels", set=a.set, type="", status="", palace="", q="", review="pending", sample=0, cls=c,
                      sort="data_id", order="asc", only_multi=0, part="", offset=0, limit=100000)
        ex = {i["data_id"] for i in d["items"]}
        items = [it for it in items if it["data_id"] not in ex]
    items.sort(key=lambda it: (a.skipped_last and it["data_id"] in skipped, (it.get("objects") or 0), it["data_id"]))
    pick = items[a.offset:a.offset + a.n]
    if a.dry:
        print(f"후보 {len(items)}건 · 고른 {len(pick)}건 · 객체 수 {Counter(it.get('objects') or 0 for it in pick)}")
        return
    run = ROOT / "runs" / f"imgreview-{datetime.now():%Y%m%d-%H%M%S}"
    for d in ("images", "batches", "results"):
        (run / d).mkdir(parents=True)

    def one(it):
        did = it["data_id"]
        try:
            lab = site.label(a.set, did)
            orig, ov = render(site.media(a.set, did), lab, run / "images", did)
            row = {"id": did, **compact(lab), "overlay_image": str(Path(ov).relative_to(ROOT)),
                   "orig_image": str(Path(orig).relative_to(ROOT)), "rule_flags": []}
            if a.grid:  # 객체 추가 좌표를 읽을 100px 격자 그림
                row["grid_image"] = str(Path(grid_image(orig, run / "images" / f"{did}_grid.jpg")).relative_to(ROOT))
            return row
        except Exception as e:
            print(f"  {did} 실패: {e}")
            return None

    with ThreadPoolExecutor(8) as ex:
        rows = [r for r in ex.map(one, pick) if r]
    batches = []
    for i in range(0, len(rows), a.batch):
        b = f"batch_{i // a.batch + 1:03d}"
        (run / "batches" / f"{b}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows[i:i + a.batch]), encoding="utf-8")
        batches.append(b)
    man = {"kind": "img", "set": a.set, "mode": "review", "n": len(rows), "max_obj": a.max_obj, "offset": a.offset,
           "prefix": a.prefix, "exclude_cls": a.exclude_cls,
           "created": datetime.now().isoformat(timespec="seconds"), "batches": batches}
    (run / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{run.relative_to(ROOT)} · 대상 {len(rows)} · 묶음 {len(batches)}개 · 객체 수 {Counter(len(r['objects']) for r in rows)}")


def check(a):
    run = Path(a.run)
    run = run if run.is_absolute() else ROOT / run
    man = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    gp = run / "gold.json"
    gold = json.loads(gp.read_text(encoding="utf-8")) if gp.exists() else {}
    res, errs = {}, []
    for b in man["batches"]:
        want = [json.loads(l)["id"] for l in (run / "batches" / f"{b}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        p = run / "results" / f"{b}.jsonl"
        if not p.exists():
            errs.append(f"{b}: 결과 없음")
            continue
        got = {}
        for r in read_jsonl(p):
            if r.get("verdict") not in ("approve", "hold", "reject", "error", "skip", "recheck"):
                errs.append(f"{b} {r.get('data_id')}: verdict 이상 {r.get('verdict')}")
            bad = set(r.get("reasons") or []) - IMG_REASONS
            if bad:
                errs.append(f"{b} {r.get('data_id')}: 사유 이상 {bad}")
            for o in r.get("add_objects") or []:
                boxes = o.get("boxes") or ([o["box"]] if o.get("box") else [])  # i8: 부분별 박스 여러 개
                ok_box = bool(boxes) and all(len(bx) == 4 and bx[0] < bx[2] and bx[1] < bx[3] for bx in boxes)
                if o.get("class") not in ADD_CLASSES or not ok_box:
                    errs.append(f"{b} {r.get('data_id')}: add_objects 이상 {o}")
            got[r.get("data_id")] = r
        miss = [d for d in want if d not in got]
        if miss:
            errs.append(f"{b}: 누락 {miss}")
        res.update({d: got[d] for d in want if d in got})
    print(f"결과 {len(res)}/{man['n']} · 오류 {len(errs)}")
    for e in errs:
        print("  " + e)
    if not gold:
        print(f"판정 {Counter(r['verdict'] for r in res.values())} · 객체 추가 {sum(1 for r in res.values() if r.get('add_objects'))}건 "
              f"({sum(len(r.get('add_objects') or []) for r in res.values())}개, {Counter(o['class'] for r in res.values() for o in r.get('add_objects') or [])})")
        print(f"사유 {Counter(x for r in res.values() for x in r.get('reasons') or [])} · 확신도 {Counter(r.get('confidence') for r in res.values())}")
        return
    bin_ = lambda v: "approve" if v == "approve" else "not"
    pairs = [(gold[d]["verdict"], r["verdict"]) for d, r in res.items() if r["verdict"] != "error"]
    if not pairs:
        return
    exact = sum(g == p for g, p in pairs)
    binm = sum(bin_(g) == bin_(p) for g, p in pairs)
    print(f"일치율(3값) {exact}/{len(pairs)} = {exact / len(pairs):.0%} · 승인/비승인 {binm}/{len(pairs)} = {binm / len(pairs):.0%}")
    cm = Counter(pairs)
    print("사람 \\ 에이전트 | approve | hold | reject")
    for g in ("approve", "hold", "reject"):
        print(f"{g:8} | " + " | ".join(str(cm[(g, p)]) for p in ("approve", "hold", "reject")))
    print(f"에이전트 확신도: {Counter(r.get('confidence') for r in res.values())}")
    if any("category" in g for g in gold.values()):  # human-pilot-prepare 로 만든 실행
        by: dict[str, Counter] = defaultdict(Counter)
        for d, r in res.items():
            by[gold[d].get("category")][r["verdict"] + ("+추가" if r.get("add_objects") else "")] += 1
        print("사람 처리 유형별 에이전트 판정")
        for c, cnt in sorted(by.items()):
            print(f"  {c}: {dict(cnt)}")
        cls = Counter()
        for d, r in res.items():  # 사람이 늘린 클래스(문양 제외) vs 에이전트 add_objects 클래스
            hk = sorted(k for k, v in (gold[d].get("class_diff") or {}).items() if v > 0 and k != "문양")
            ak = sorted({o.get("class") for o in r.get("add_objects") or []})
            if hk or ak:
                cls["일치" if hk == ak else f"사람 {hk} / 에이전트 {ak}"] += 1
        print(f"객체 추가 클래스: {dict(cls)}")
    out = []
    for d, r in res.items():
        g = gold[d]
        if bin_(g["verdict"]) != bin_(r["verdict"]):
            out.append({"data_id": d, "category": g.get("category"), "human": g["verdict"], "human_comment": g["comment"][:60],
                        "agent": r["verdict"], "agent_reasons": r.get("reasons"), "agent_comment": (r.get("comment") or "")[:80],
                        "evidence": r.get("evidence"), "confidence": r.get("confidence")})
    (run / "mismatch.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"불일치 {len(out)}건 → {(run / 'mismatch.json').relative_to(ROOT)}")
    for m in out:
        print(f"  {m['data_id']} 사람 {m['human']}({m['human_comment']}) / 에이전트 {m['agent']} {m['agent_comment'] or m['evidence']} [{m['confidence']}]")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("pilot-prepare")
    p.add_argument("--set", default="이미지 15차")
    p.add_argument("--n", type=int, default=50)
    p.add_argument("--max-obj", type=int, default=2)
    p.add_argument("--batch", type=int, default=5)
    p.add_argument("--seed", type=int, default=1)
    p = sp.add_parser("human-pilot-prepare")
    p.add_argument("--set", default="이미지 15차")
    p.add_argument("--reviewer", required=True, help="이 검수자의 판정을 정답으로 쓴다")
    p.add_argument("--since", default="", help="YYYY-MM-DD (현지 시각 자정부터). 비우면 오늘")
    p.add_argument("--prefix", default="", help="쉼표 구분 data_id 접두어 (예: GBG)")
    p.add_argument("--n", type=int, default=50)
    p.add_argument("--min-per", type=int, default=4, help="처리 유형마다 최소 건수")
    p.add_argument("--batch", type=int, default=5)
    p.add_argument("--seed", type=int, default=1)
    p = sp.add_parser("review-prepare")
    p.add_argument("--set", default="이미지 15차")
    p.add_argument("--n", type=int, default=50)
    p.add_argument("--max-obj", type=int, default=1000)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--exclude-cls", default="", help="쉼표 구분. 이 클래스 객체가 있는 건 제외 (예: 문양)")
    p.add_argument("--include-cls", default="", help="쉼표 구분. 이 클래스 객체가 있는 건만")
    p.add_argument("--include-skipped", action="store_true", help="앞선 실검수에서 skip 한 건도 다시 고른다")
    p.add_argument("--skipped-last", action="store_true", help="앞선 skip 건도 고르되 처음 보는 건 뒤에 둔다(n 채우기)")
    p.add_argument("--prefix", default="", help="쉼표 구분 data_id 접두어 (예: GBG)")
    p.add_argument("--grid", action="store_true", help="100px 좌표 격자 그림도 만든다(객체 추가용)")
    p.add_argument("--dry", action="store_true", help="후보 수만 세고 묶음은 만들지 않는다")
    p.add_argument("--batch", type=int, default=5)
    p = sp.add_parser("check")
    p.add_argument("run")
    a = ap.parse_args()
    {"pilot-prepare": pilot_prepare, "human-pilot-prepare": human_pilot_prepare, "review-prepare": review_prepare,
     "check": check}[a.cmd](a)


if __name__ == "__main__":
    main()
