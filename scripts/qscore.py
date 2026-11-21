"""품질 검수 사이트(의미정확성 탭) 이미지 표본 자동 채점 도구.

  prepare : 표본을 뽑아 라벨·이미지를 받고, 원본/겹친 이미지와 묶음 JSON 을 만든다.
      /opt/miniconda3/bin/python scripts/qscore.py prepare --round F01 --n 200 --calib 40 --batch 10
  check   : 결과 JSONL 을 검증하고 점수·정확도·일치율을 집계한다.
      /opt/miniconda3/bin/python scripts/qscore.py check runs/<run>
  upload  : 검증된 결과를 사이트에 저장한다(일치율 확인용 calib 건은 저장하지 않는다).
      /opt/miniconda3/bin/python scripts/qscore.py upload runs/<run> [--execute]

로그인은 .env 의 QUALITY_EMAIL / QUALITY_PASSWORD (출력하지 않는다).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import random
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, load_env  # noqa: E402

BASE = "http://192.168.100.145:8772"
STATES = {"ok", "minor", "major", "na"}
FONT = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
COLORS = [(230, 25, 75), (60, 180, 75), (0, 130, 200), (245, 130, 48), (145, 30, 180), (70, 240, 240),
          (240, 50, 230), (210, 245, 60), (250, 190, 212), (0, 128, 128), (170, 110, 40), (128, 0, 0)]


def session():
    import requests
    load_env()
    s = requests.Session()
    s.headers.update({"Origin": BASE, "Referer": BASE + "/"})
    r = s.post(f"{BASE}/api/auth/login", json={"email": os.environ["QUALITY_EMAIL"],
                                                "password": os.environ["QUALITY_PASSWORD"]}, timeout=30)
    if r.status_code >= 400:
        sys.exit(f"로그인 실패 ({r.status_code}): {r.text[:200]}")
    me = s.get(f"{BASE}/api/auth/me", timeout=30).json()["user"]
    return s, me["name"]


def get(s, path, **params):
    for i in range(4):
        try:
            r = s.get(BASE + path, params=params, timeout=120)
            r.raise_for_status()
            return r
        except Exception:
            if i == 3:
                raise
            time.sleep(2 * (i + 1))
    raise AssertionError("unreachable")


# ---------- RLE (COCO 압축 문자열, 열 우선) ----------
def rle_counts(s: str) -> list[int]:
    cnts, p = [], 0
    while p < len(s):
        x = k = 0
        more = 1
        while more:
            c = ord(s[p]) - 48
            x |= (c & 0x1f) << (5 * k)
            more = c & 0x20
            p += 1
            k += 1
            if not more and (c & 0x10):
                x |= (-1 << (5 * k))
        if len(cnts) > 2:
            x += cnts[-2]
        cnts.append(x)
    return cnts


def rle_mask(data: str):
    import numpy as np
    h, rest = data.split("x", 1)
    w, counts = rest.split(":", 1)
    h, w = int(h), int(w)
    m = np.zeros(h * w, dtype=np.uint8)
    idx, val = 0, 0
    for n in rle_counts(counts):
        if val:
            m[idx:idx + n] = 1
        idx += n
        val = 1 - val
    return m.reshape((w, h)).T  # 열 우선


def render(img_bytes: bytes, label: dict, out_dir: Path, did: str) -> tuple[str, str]:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    W0, H0 = im.size
    k = 1024 / max(W0, H0) if max(W0, H0) > 1024 else 1.0
    im = im.resize((round(W0 * k), round(H0 * k))) if k != 1.0 else im
    W, H = im.size
    orig = out_dir / f"{did}_orig.jpg"
    im.save(orig, quality=88)
    LW, LH = label.get("width") or W0, label.get("height") or H0
    ov = im.copy().convert("RGBA")
    layer = Image.new("RGBA", ov.size, (0, 0, 0, 0))
    for i, o in enumerate(label.get("objects") or []):
        col = COLORS[i % len(COLORS)]
        mk = o.get("mask") or {}
        if mk.get("format") == "rle" and isinstance(mk.get("data"), str):
            try:
                m = rle_mask(mk["data"])
                mi = Image.fromarray(m * 255).resize((W, H), Image.Resampling.NEAREST)
                arr = np.zeros((H, W, 4), dtype=np.uint8)
                sel = np.array(mi) > 0
                arr[sel] = (*col, 110)
                layer = Image.alpha_composite(layer, Image.fromarray(arr, "RGBA"))
            except Exception:
                pass
    ov = Image.alpha_composite(ov, layer)
    d = ImageDraw.Draw(ov)
    fs = max(16, W // 40)
    try:
        font = ImageFont.truetype(FONT, fs)
    except Exception:
        font = ImageFont.load_default()
    for i, o in enumerate(label.get("objects") or []):
        col = COLORS[i % len(COLORS)]
        bx, by, bw, bh = o.get("bbox") or [0, 0, 0, 0]
        x0, y0 = bx * W / LW, by * H / LH
        x1, y1 = (bx + bw) * W / LW, (by + bh) * H / LH
        d.rectangle([x0, y0, x1, y1], outline=(*col, 255), width=max(3, W // 300))
        tag = f"#{i + 1} {o.get('class', '')}"
        tw = d.textlength(tag, font=font)
        ty = max(0, y0 - fs - 6) if y0 > fs + 6 else y0
        d.rectangle([x0, ty, x0 + tw + 8, ty + fs + 6], fill=(*col, 230))
        d.text((x0 + 4, ty + 2), tag, fill=(255, 255, 255, 255), font=font)
    ovp = out_dir / f"{did}_overlay.jpg"
    ov.convert("RGB").save(ovp, quality=88)
    return str(orig), str(ovp)


def compact(label: dict) -> dict:
    objs = []
    for i, o in enumerate(label.get("objects") or []):
        x = {"no": i + 1, "class": o.get("class"), "bbox_xywh": o.get("bbox"),
             "has_mask": bool((o.get("mask") or {}).get("data")), "confidence": o.get("confidence")}
        if o.get("ocr"):
            x["ocr"] = {k: o["ocr"].get(k) for k in ("text", "reading", "confidence")}
        objs.append(x)
    return {
        "palace": label.get("palace"), "heritage_name": label.get("heritage_name"),
        "heritage_id": label.get("heritage_id"), "class_code_path": label.get("class_code_path"),
        "title": label.get("title"), "image_caption": label.get("image_caption"),
        "tags": label.get("tags"), "object_count": label.get("object_count"),
        "label_size": [label.get("width"), label.get("height")], "objects": objs,
    }


def prepare(a):
    s, me = session()
    rd = get(s, f"/api/q/round/{a.round}", reviewer=me).json()
    imgs = [it for it in rd["items"] if it["data_type"] == "image"]
    fresh = [it["data_id"] for it in imgs if it.get("result") and it["result"].get("reviewer") == "일괄적합"
             and not it.get("mine")]
    mine = [it["data_id"] for it in imgs if it.get("result") and it["result"].get("reviewer") == me]
    rng = random.Random(a.seed)
    pick = [(d, "score") for d in rng.sample(fresh, min(a.n, len(fresh)))]
    pick += [(d, "calib") for d in rng.sample(mine, min(a.calib, len(mine)))]
    rng.shuffle(pick)
    run = ROOT / "runs" / f"qscore-{a.round}-{datetime.now():%Y%m%d-%H%M%S}"
    (run / "images").mkdir(parents=True)
    (run / "batches").mkdir()
    (run / "results").mkdir()
    (run / "private").mkdir()

    def one(t):
        did, role = t
        det = get(s, f"/api/q/item/{a.round}/{did}", reviewer=me).json()
        lab = det["label"]
        img = get(s, f"/media/{did}").content
        o, v = render(img, lab, run / "images", did)
        human = det.get("mine") if role == "calib" else None
        return {"data_id": did, "role": role, "orig_image": o, "overlay_image": v, "label": compact(lab)}, \
            (did, human["checks"] if human else None)

    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(one, pick))
    items = [r[0] for r in res]
    truth = {d: c for d, c in (r[1] for r in res) if c}
    (run / "private" / "calib_truth.json").write_text(json.dumps(truth, ensure_ascii=False), encoding="utf-8")
    batches = []
    for i in range(0, len(items), a.batch):
        b = f"batch_{i // a.batch + 1:03d}"
        rows = [{k: v for k, v in it.items() if k != "role"} for it in items[i:i + a.batch]]
        (run / "batches" / f"{b}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        batches.append(b)
    man = {"round": a.round, "reviewer": me, "seed": a.seed, "created": datetime.now().isoformat(timespec="seconds"),
           "n_score": sum(r == "score" for _, r in pick), "n_calib": sum(r == "calib" for _, r in pick),
           "roles": {d: r for d, r in pick}, "batches": batches}
    (run / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{run.relative_to(ROOT)} · 채점 {man['n_score']} · 일치율용 {man['n_calib']} · 묶음 {len(batches)}개")


# ---------- 점수 (사이트 규칙과 같게) ----------
def load_meta():
    return json.loads((Path(__file__).resolve().parent / "qscore_meta.json").read_text(encoding="utf-8"))


def sem_scores(checks: dict, meta: dict) -> dict:
    crit = {x["key"] for x in meta["types"]["image"] if x.get("critical")}
    out = {}
    for it in meta["semantic"]["image"]:
        st = [checks.get(k) for k in it["subs"]]
        if all(v == "na" for v in st):
            continue
        maj = [k for k in it["subs"] if checks.get(k) == "major"]
        mnr = [k for k in it["subs"] if checks.get(k) == "minor"]
        out[it["name"]] = 1 if (any(k in crit for k in maj) or len(maj) >= 3) else 2 if len(maj) == 2 else \
            3 if len(maj) == 1 else 4 if mnr else 5
    return out


def read_results(run: Path) -> tuple[dict, list[str]]:
    man = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    keys = [x["key"] for x in load_meta()["types"]["image"]]
    got, errs = {}, []
    for b in man["batches"]:
        p = run / "results" / f"{b}.jsonl"
        want = {r["data_id"] for r in json.loads((run / "batches" / f"{b}.json").read_text(encoding="utf-8"))}
        if not p.exists():
            errs.append(f"{b}: 결과 없음")
            continue
        seen = set()
        for ln in p.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except Exception:
                errs.append(f"{b}: JSON 오류")
                continue
            c = r.get("checks") or {}
            bad = [k for k in keys if c.get(k) not in STATES] + [k for k in c if k not in keys]
            if r.get("data_id") not in want or bad:
                errs.append(f"{b}: {r.get('data_id')} 형식 오류 {bad[:3]}")
                continue
            if c.get("ocr") == "na" or True:
                pass
            seen.add(r["data_id"])
            got[r["data_id"]] = r
        for m in want - seen:
            errs.append(f"{b}: {m} 누락")
    return got, errs


def check(a):
    run = Path(a.run)
    man = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    meta = load_meta()
    got, errs = read_results(run)
    print(f"결과 {len(got)}/{len(man['roles'])}건 · 오류 {len(errs)}")
    for e in errs[:20]:
        print("  -", e)
    for role, title in (("score", "채점 표본"), ("calib", "일치율용(저장 안 함)")):
        rows = [r for d, r in got.items() if man["roles"].get(d) == role]
        if not rows:
            continue
        agg: dict[str, list[int]] = {}
        for r in rows:
            for k, v in sem_scores(r["checks"], meta).items():
                agg.setdefault(k, []).append(v)
        fail = sum(any(v == "major" for v in r["checks"].values()) for r in rows)
        print(f"\n## {title} {len(rows)}건 · 불합격 {fail} ({fail / len(rows):.1%})")
        for it in meta["semantic"]["image"]:
            v = agg.get(it["name"], [])
            if v:
                dist = Counter(v)
                print(f"- {it['name']}: {sum(v) / (len(v) * 5) * 100:.1f}% · {len(v)}건 · "
                      + " ".join(f"{s}점 {dist[s]}" for s in (5, 4, 3, 2, 1)))
        sub = Counter((k, v) for r in rows for k, v in r["checks"].items() if v in ("major", "minor"))
        print("- 세부 부적합/보통: " + ", ".join(f"{k} {v} {n}" for (k, v), n in sub.most_common(12)))
    truth = json.loads((run / "private" / "calib_truth.json").read_text(encoding="utf-8"))
    pairs = [(truth[d], got[d]["checks"]) for d in truth if d in got]
    if pairs:
        keys = [x["key"] for x in meta["types"]["image"]]
        same = sum(h.get(k) == g.get(k) for h, g in pairs for k in keys)
        pf = sum((any(v == "major" for v in h.values())) == (any(v == "major" for v in g.values())) for h, g in pairs)
        print(f"\n## 사람 채점과 비교 {len(pairs)}건: 세부 항목 일치 {same / (len(pairs) * len(keys)):.1%} · "
              f"합격/불합격 일치 {pf / len(pairs):.1%}")
        hf = sum(any(v == "major" for v in h.values()) for h, _ in pairs)
        gf = sum(any(v == "major" for v in g.values()) for _, g in pairs)
        print(f"- 불합격: 사람 {hf} · 에이전트 {gf}")
        for k in keys:
            c = Counter((h.get(k), g.get(k)) for h, g in pairs if h.get(k) != g.get(k))
            if c:
                print(f"  - {k}: " + ", ".join(f"사람 {x}→에이전트 {y} {n}" for (x, y), n in c.most_common(4)))


def upload(a):
    run = Path(a.run)
    man = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    got, errs = read_results(run)
    rows = [r for d, r in got.items() if man["roles"].get(d) == "score"]
    print(f"저장 대상 {len(rows)}건 (일치율용 {man['n_calib']}건 제외) · 회차 {man['round']} · 검증 오류 {len(errs)}")
    if not a.execute:
        print("미리보기만 했습니다. 저장하려면 --execute")
        return
    s, me = session()
    if me != man["reviewer"]:
        sys.exit(f"로그인 계정({me})이 준비 때({man['reviewer']})와 다릅니다.")
    log = run / "upload_log.jsonl"
    done = {json.loads(x)["data_id"] for x in log.read_text(encoding="utf-8").splitlines()} if log.exists() else set()
    ok = 0
    with log.open("a", encoding="utf-8") as f:
        for r in rows:
            if r["data_id"] in done:
                continue
            body = {"reviewer": me, "checks": r["checks"], "comment": (r.get("comment") or "")[:500]}
            resp = s.post(f"{BASE}/api/q/score/{man['round']}/{r['data_id']}", json=body, timeout=60)
            if resp.status_code >= 400:
                print(f"  실패 {r['data_id']}: {resp.status_code} {resp.text[:150]}")
                continue
            j = resp.json()
            f.write(json.dumps({"data_id": r["data_id"], "pass": j.get("pass"), "at": j.get("at")}, ensure_ascii=False) + "\n")
            ok += 1
    print(f"저장 {ok}건 · 누적 {len(done) + ok}건")


# ---------- 현판 판독만 확인 (나머지 12개 항목은 일괄적합 값 유지) ----------
OCR_NOTE = "현판 판독만 확인(자동 검수). 나머지 12개 항목은 일괄적합 판정 그대로이며 개별 확인 전"


def ocr_prepare(a):
    from PIL import Image
    s, me = session()
    rd = get(s, f"/api/q/round/{a.round}", reviewer=me).json()
    cand = [it["data_id"] for it in rd["items"] if it["data_type"] == "image"
            and {"현판", "한자"} & set(it.get("classes") or [])
            and it.get("result") and it["result"].get("reviewer") == "일괄적합" and not it.get("mine")]
    run = ROOT / "runs" / f"qocr-{a.round}-{datetime.now():%Y%m%d-%H%M%S}"
    for d in ("images", "batches", "results"):
        (run / d).mkdir(parents=True)

    def one(did):
        det = get(s, f"/api/q/item/{a.round}/{did}", reviewer="일괄적합").json()
        lab, pre = det["label"], det.get("mine") or {}
        objs = [(i, o) for i, o in enumerate(lab.get("objects") or []) if o.get("ocr")]
        if not objs or pre.get("reviewer") != "일괄적합":
            return None
        im = Image.open(io.BytesIO(get(s, f"/media/{did}").content)).convert("RGB")
        W, H = im.size
        LW, LH = lab.get("width") or W, lab.get("height") or H
        crops = []
        for i, o in objs:
            bx, by, bw, bh = o.get("bbox") or [0, 0, LW, LH]
            px, py = bw * 0.12, bh * 0.25
            box = [max(0, (bx - px) * W / LW), max(0, (by - py) * H / LH),
                   min(W, (bx + bw + px) * W / LW), min(H, (by + bh + py) * H / LH)]
            x0, y0, x1, y1 = (round(v) for v in box)
            c = im.crop((x0, y0, x1, y1))
            k = 768 / max(c.size) if max(c.size) > 768 else (min(3.0, 384 / max(c.size)) if max(c.size) < 384 else 1.0)
            c = c.resize((max(1, round(c.width * k)), max(1, round(c.height * k))))
            p = run / "images" / f"{did}_ocr{i + 1}.jpg"
            c.save(p, quality=90)
            crops.append({"no": i + 1, "class": o.get("class"), "crop_image": str(p),
                          "ocr_text": o["ocr"].get("text"), "reading": o["ocr"].get("reading")})
        return {"data_id": did, "heritage_name": lab.get("heritage_name"), "palace": lab.get("palace"),
                "plates": crops}, pre["checks"]

    with ThreadPoolExecutor(8) as ex:
        res = [r for r in ex.map(one, cand) if r]
    items = [r[0] for r in res]
    pre = {r[0]["data_id"]: r[1] for r in res}
    (run / "prefill_checks.json").write_text(json.dumps(pre, ensure_ascii=False), encoding="utf-8")
    batches = []
    for i in range(0, len(items), a.batch):
        b = f"batch_{i // a.batch + 1:03d}"
        (run / "batches" / f"{b}.json").write_text(json.dumps(items[i:i + a.batch], ensure_ascii=False, indent=1),
                                                    encoding="utf-8")
        batches.append(b)
    man = {"round": a.round, "reviewer": me, "kind": "ocr", "created": datetime.now().isoformat(timespec="seconds"),
           "candidates": len(cand), "n": len(items), "plates": sum(len(x["plates"]) for x in items), "batches": batches}
    (run / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{run.relative_to(ROOT)} · 후보 {len(cand)} · 판독 객체 있는 이미지 {len(items)} · 현판 조각 {man['plates']} · 묶음 {len(batches)}개")


def ocr_read(run: Path) -> tuple[dict, list[str]]:
    man = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    got, errs = {}, []
    for b in man["batches"]:
        want = {r["data_id"] for r in json.loads((run / "batches" / f"{b}.json").read_text(encoding="utf-8"))}
        p = run / "results" / f"{b}.jsonl"
        if not p.exists():
            errs.append(f"{b}: 결과 없음")
            continue
        for ln in p.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except Exception:
                errs.append(f"{b}: JSON 오류")
                continue
            if r.get("data_id") not in want or r.get("ocr") not in ("ok", "minor", "major"):
                errs.append(f"{b}: {r.get('data_id')} 형식 오류")
                continue
            got[r["data_id"]] = r
        errs += [f"{b}: {m} 누락" for m in want - set(got)]
    return got, errs


def ocr_check(a):
    run = Path(a.run)
    got, errs = ocr_read(run)
    c = Counter(r["ocr"] for r in got.values())
    pts = {"ok": 5, "minor": 4, "major": 3}
    print(f"결과 {len(got)}건 · 오류 {len(errs)} · 적합 {c['ok']} · 보통 {c['minor']} · 부적합 {c['major']}")
    if got:
        print(f"이 표본의 현판 판독 정확성 {sum(pts[r['ocr']] for r in got.values()) / (len(got) * 5) * 100:.1f}%")
    for e in errs[:20]:
        print("  -", e)
    for r in got.values():
        if r["ocr"] == "major":
            print(f"  부적합 {r['data_id']}: {r.get('comment', '')}")


def ocr_upload(a):
    run = Path(a.run)
    man = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    pre = json.loads((run / "prefill_checks.json").read_text(encoding="utf-8"))
    got, errs = ocr_read(run)
    print(f"저장 대상 {len(got)}건 · 검증 오류 {len(errs)}")
    if not a.execute:
        print("미리보기만 했습니다. 저장하려면 --execute")
        return
    s, me = session()
    log = run / "upload_log.jsonl"
    done = {json.loads(x)["data_id"] for x in log.read_text(encoding="utf-8").splitlines()} if log.exists() else set()
    ok = 0
    with log.open("a", encoding="utf-8") as f:
        for did, r in got.items():
            if did in done:
                continue
            checks = {**pre[did], "ocr": r["ocr"]}
            note = OCR_NOTE + (f" · 판독: {r['comment']}" if r.get("comment") else "")
            resp = s.post(f"{BASE}/api/q/score/{man['round']}/{did}",
                          json={"reviewer": me, "checks": checks, "comment": note[:500]}, timeout=60)
            if resp.status_code >= 400:
                print(f"  실패 {did}: {resp.status_code} {resp.text[:150]}")
                continue
            f.write(json.dumps({"data_id": did, "ocr": r["ocr"], "at": resp.json().get("at")}, ensure_ascii=False) + "\n")
            ok += 1
    print(f"저장 {ok}건 · 누적 {len(done) + ok}건")


# ---------- 객체 영역 정확성만 확인 (obj_bbox·obj_mask·obj_missing, 나머지 10개는 일괄적합 값 유지) ----------
REGION_KEYS = ["obj_bbox", "obj_mask", "obj_missing"]
REGION_NOTE = "객체 영역(위치·마스크·누락)만 확인(자동 검수). 나머지 10개 항목은 일괄적합 판정 그대로이며 개별 확인 전"


def region_prepare(a):
    s, me = session()
    rd = get(s, f"/api/q/round/{a.round}", reviewer=me).json()
    cand = [it["data_id"] for it in rd["items"] if it["data_type"] == "image"
            and it.get("result") and it["result"].get("reviewer") == "일괄적합" and not it.get("mine")]
    pick = random.Random(a.seed).sample(cand, min(a.n, len(cand)))
    run = ROOT / "runs" / f"qregion-{a.round}-{datetime.now():%Y%m%d-%H%M%S}"
    for d in ("images", "batches", "results"):
        (run / d).mkdir(parents=True)

    def one(did):
        det = get(s, f"/api/q/item/{a.round}/{did}", reviewer="일괄적합").json()
        lab, pre = det["label"], det.get("mine") or {}
        if pre.get("reviewer") != "일괄적합":
            return None
        _, ov = render(get(s, f"/media/{did}").content, lab, run / "images", did)
        (run / "images" / f"{did}_orig.jpg").unlink(missing_ok=True)
        c = compact(lab)
        return {"data_id": did, "overlay_image": ov, "heritage_name": c["heritage_name"],
                "label_size": c["label_size"], "objects": [{k: o[k] for k in ("no", "class", "bbox_xywh", "has_mask")}
                                                           for o in c["objects"]]}, pre["checks"]

    with ThreadPoolExecutor(8) as ex:
        res = [r for r in ex.map(one, pick) if r]
    items = [r[0] for r in res]
    (run / "prefill_checks.json").write_text(json.dumps({r[0]["data_id"]: r[1] for r in res}, ensure_ascii=False),
                                             encoding="utf-8")
    batches = []
    for i in range(0, len(items), a.batch):
        b = f"batch_{i // a.batch + 1:03d}"
        (run / "batches" / f"{b}.json").write_text(json.dumps(items[i:i + a.batch], ensure_ascii=False, indent=1),
                                                    encoding="utf-8")
        batches.append(b)
    man = {"round": a.round, "reviewer": me, "kind": "region", "seed": a.seed, "n": len(items),
           "created": datetime.now().isoformat(timespec="seconds"), "batches": batches}
    (run / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{run.relative_to(ROOT)} · 대상 {len(items)} · 묶음 {len(batches)}개")


def region_read(run: Path) -> tuple[dict, list[str]]:
    man = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    got, errs = {}, []
    for b in man["batches"]:
        want = {r["data_id"] for r in json.loads((run / "batches" / f"{b}.json").read_text(encoding="utf-8"))}
        p = run / "results" / f"{b}.jsonl"
        if not p.exists():
            errs.append(f"{b}: 결과 없음")
            continue
        for ln in p.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except Exception:
                errs.append(f"{b}: JSON 오류")
                continue
            c = r.get("checks") or {}
            if r.get("data_id") not in want or set(c) != set(REGION_KEYS) or \
                    any(v not in ("ok", "minor", "major") for v in c.values()):
                errs.append(f"{b}: {r.get('data_id')} 형식 오류")
                continue
            got[r["data_id"]] = r
        errs += [f"{b}: {m} 누락" for m in want - set(got)]
    return got, errs


def region_check(a):
    run = Path(a.run)
    got, errs = region_read(run)
    print(f"결과 {len(got)}건 · 오류 {len(errs)}")
    for e in errs[:20]:
        print("  -", e)
    if not got:
        return
    sc = []
    for r in got.values():
        maj = sum(v == "major" for v in r["checks"].values())
        mnr = any(v == "minor" for v in r["checks"].values())
        sc.append(1 if maj >= 3 else 2 if maj == 2 else 3 if maj == 1 else 4 if mnr else 5)
    d = Counter(sc)
    print(f"이 표본의 객체 영역 정확성 {sum(sc) / (len(sc) * 5) * 100:.1f}% · " + " ".join(f"{k}점 {d[k]}" for k in (5, 4, 3, 2, 1)))
    print("세부: " + ", ".join(f"{k} {v} {n}" for (k, v), n in
                              Counter((k, v) for r in got.values() for k, v in r["checks"].items() if v != "ok").most_common()))


def region_upload(a):
    run = Path(a.run)
    man = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    pre = json.loads((run / "prefill_checks.json").read_text(encoding="utf-8"))
    got, errs = region_read(run)
    print(f"저장 대상 {len(got)}건 · 검증 오류 {len(errs)}")
    if not a.execute:
        print("미리보기만 했습니다. 저장하려면 --execute")
        return
    s, me = session()
    log = run / "upload_log.jsonl"
    done = {json.loads(x)["data_id"] for x in log.read_text(encoding="utf-8").splitlines()} if log.exists() else set()
    ok = 0
    with log.open("a", encoding="utf-8") as f:
        for did, r in got.items():
            if did in done:
                continue
            note = REGION_NOTE + (f" · 영역: {r['comment']}" if r.get("comment") else "")
            resp = s.post(f"{BASE}/api/q/score/{man['round']}/{did}",
                          json={"reviewer": me, "checks": {**pre[did], **r["checks"]}, "comment": note[:500]}, timeout=60)
            if resp.status_code >= 400:
                print(f"  실패 {did}: {resp.status_code} {resp.text[:150]}")
                continue
            f.write(json.dumps({"data_id": did, **r["checks"], "at": resp.json().get("at")}, ensure_ascii=False) + "\n")
            ok += 1
    print(f"저장 {ok}건 · 누적 {len(done) + ok}건")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("region-prepare")
    p.add_argument("--round", default="F01")
    p.add_argument("--n", type=int, default=200)
    p.add_argument("--batch", type=int, default=20)
    p.add_argument("--seed", type=int, default=20261120)
    p.set_defaults(fn=region_prepare)
    p = sp.add_parser("region-check")
    p.add_argument("run")
    p.set_defaults(fn=region_check)
    p = sp.add_parser("region-upload")
    p.add_argument("run")
    p.add_argument("--execute", action="store_true")
    p.set_defaults(fn=region_upload)
    p = sp.add_parser("ocr-prepare")
    p.add_argument("--round", default="F01")
    p.add_argument("--batch", type=int, default=25)
    p.set_defaults(fn=ocr_prepare)
    p = sp.add_parser("ocr-check")
    p.add_argument("run")
    p.set_defaults(fn=ocr_check)
    p = sp.add_parser("ocr-upload")
    p.add_argument("run")
    p.add_argument("--execute", action="store_true")
    p.set_defaults(fn=ocr_upload)
    p = sp.add_parser("prepare")
    p.add_argument("--round", default="F01")
    p.add_argument("--n", type=int, default=200)
    p.add_argument("--calib", type=int, default=40)
    p.add_argument("--batch", type=int, default=10)
    p.add_argument("--seed", type=int, default=20261118)
    p.set_defaults(fn=prepare)
    p = sp.add_parser("check")
    p.add_argument("run")
    p.set_defaults(fn=check)
    p = sp.add_parser("upload")
    p.add_argument("run")
    p.add_argument("--execute", action="store_true")
    p.set_defaults(fn=upload)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
