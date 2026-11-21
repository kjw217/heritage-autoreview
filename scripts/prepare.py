"""실행 폴더 준비: 사이트에서 라벨을 내려받아 규칙 검사를 붙이고 묶음 파일로 나눈다.

  python scripts/prepare.py pilot  [--source gold|human] [--n 52] [--seed 7]
  python scripts/prepare.py review [--offset 0] [--limit N] [--per-subtype N]
  공통: [--kind text|qa|gen] [--set "텍스트 16차"] [--batch-size 26] [--max-chars 16000] [--workers 8]

검수 종류(--kind)
  text (기본): 텍스트 청크(엔티티·관계·5종 요약). 이름에 '_QA_'·'_GEN_'이 있는 건은 건너뛴다.
  qa         : 질의응답. 이름에 '_QA_'가 있는 건만 검수하고 나머지는 건너뛴다.
  gen        : 생성형. 이름에 '_GEN_'이 있는 건만 검수한다. 이미지 문항은 원본 이미지를 내려받아
               긴 변 --img-size(기본 1024)px JPEG로 줄여 runs/<run>/images/ 에 둔다(macOS sips 사용).
               (mm 도 같다: 이름에 '_MM_'이 있는 멀티모달. 이미지는 같은 방식으로 받고, 영상은 장면 설명만 넘긴다.)
               --per-subtype N: 미검수 중 이미지 문항·텍스트 문항을 무작위로 N건씩 고른다(시험용, --seed).
  --ids A,B,...: (review) 판정 여부와 관계없이 지정한 데이터만 검수한다(재검수·확인용).

결과: runs/<mode>-<시각>/
  manifest.json          실행 정보(모드, 세트, 묶음별 id 목록, 지침 해시)
  batches/batch_NNN.jsonl 검수 에이전트 입력 (한 줄 = 1건)
  results/               검수 에이전트 출력 위치 (비어 있음)
  images/                (gen) 줄인 원천 이미지. 묶음의 image 필드가 이 파일을 가리킨다
  truth.tsv              (pilot) 정답 판정
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (DEFAULT_SET, ROOT, Site, compact_gen, compact_item, compact_mm, compact_qa,  # noqa: E402
                    dumps_line, gen_sub_type, kind_of, write_jsonl)

# 종류별 검수 에이전트와 판정 기준 파일
KIND_AGENT = {"text": "text-reviewer", "qa": "qa-reviewer", "gen": "gen-reviewer", "mm": "mm-reviewer"}
KIND_GUIDE = {"text": "docs/review_guide.md", "qa": "docs/qa_review_guide.md", "gen": "docs/gen_review_guide.md",
              "mm": "docs/mm_review_guide.md"}
KIND_BATCH = {"text": 26, "qa": 26, "gen": 8, "mm": 8}  # 묶음당 기본 건수. 생성형은 이미지를 1장씩 Read 하므로 작게


def shrink_image(raw: bytes, out: Path, size: int) -> None:
    """원본 이미지를 긴 변 size px JPEG로 줄여 저장한다 (macOS 기본 도구 sips. 작은 이미지는 키우지 않는다)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
        f.write(raw)
        src = f.name
    try:
        w = h = 0
        info = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", src], capture_output=True, text=True).stdout
        for line in info.splitlines():
            if "pixelWidth" in line:
                w = int(line.split()[-1])
            if "pixelHeight" in line:
                h = int(line.split()[-1])
        cmd = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "85"]
        if max(w, h) > size:
            cmd += ["-Z", str(size)]
        # 같은 이미지를 여러 문항이 동시에 받을 수 있어 고유한 임시 파일에 쓴 뒤 이름을 바꾼다
        tmp = out.with_name(f"{out.stem}.{Path(src).stem}.jpg")
        r = subprocess.run(cmd + [src, "--out", str(tmp)], capture_output=True, text=True)
        if r.returncode or not tmp.exists():
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"sips 실패: {r.stderr.strip()[:120]}")
        tmp.replace(out)
    finally:
        Path(src).unlink(missing_ok=True)


def pick_per_subtype(site: Site, a, ids: list[str]) -> list[dict]:
    """미검수 생성형 중 이미지 문항·텍스트 문항을 무작위로 a.per_subtype 건씩 고른다. 고른 라벨 응답을 돌려준다."""
    rnd = random.Random(a.seed)
    ids = ids[:]
    rnd.shuffle(ids)
    want = {"image_based": a.per_subtype, "text_based": a.per_subtype}
    got: dict[str, list[dict]] = {k: [] for k in want}
    pos = 0
    with ThreadPoolExecutor(a.workers) as ex:
        while pos < len(ids) and any(len(got[k]) < n for k, n in want.items()):
            chunk = ids[pos:pos + 40]
            pos += len(chunk)
            for resp in ex.map(lambda i: site.label_full(a.set, i), chunk):
                st = gen_sub_type(resp.get("label") or {})
                if st in got and len(got[st]) < want[st]:
                    got[st].append(resp)
    for k, n in want.items():
        if len(got[k]) < n:
            print(f"{k} 미검수가 {len(got[k])}건뿐입니다 (요청 {n}건).")
    return got["image_based"] + got["text_based"]


def pick_pilot(site: Site, a) -> list[tuple[str, str]]:
    rnd = random.Random(a.seed)
    if a.source == "gold":
        p = ROOT / a.gold
        if not p.exists():
            sys.exit(f"정답 파일이 없습니다: {p}\n  형식: data_id<TAB>approve|reject|hold (한 줄에 1건). 또는 --source human 을 쓰세요.")
        rows = [l.split("\t")[:2] for l in p.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
        rows = [(i.strip(), v.strip()) for i, v in rows if kind_of(i.strip()) == a.kind]
        rnd.shuffle(rows)
        return rows[: a.n] if a.n else rows
    # human: 사이트에 사람이 저장한 판정에서 판정별 같은 수로 뽑는다
    items, _ = site.list_ids(a.set, "human")
    by_v: dict[str, list[str]] = {}
    for it in items:
        if kind_of(it["data_id"]) != a.kind:
            continue
        if it.get("verdict") in ("approve", "reject", "hold"):
            by_v.setdefault(it["verdict"], []).append(it["data_id"])
    if not by_v:
        sys.exit("사람이 저장한 판정이 없습니다.")
    per = max(1, a.n // len(by_v))
    out = []
    for v, ids in by_v.items():
        rnd.shuffle(ids)
        out += [(i, v) for i in ids[:per]]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["pilot", "review"])
    ap.add_argument("--kind", choices=["text", "qa", "gen", "mm"], default="text",
                    help="text | qa (이름에 _QA_) | gen (이름에 _GEN_) | mm (이름에 _MM_)")
    ap.add_argument("--set", default=DEFAULT_SET)
    ap.add_argument("--batch-size", type=int, help="묶음당 최대 건수 (기본: text·qa 26, gen 8)")
    ap.add_argument("--max-chars", type=int, default=16000, help="묶음당 최대 글자 수 (Read 1회 분량)")
    ap.add_argument("--workers", type=int, default=8, help="사이트 동시 조회 수")
    ap.add_argument("--source", choices=["gold", "human"], default="gold")
    ap.add_argument("--gold", default="data/gold.tsv")
    ap.add_argument("--n", type=int, default=52)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0, help="0이면 미검수 전체")
    ap.add_argument("--per-subtype", type=int, default=0, help="(gen, review) 이미지·텍스트 문항을 무작위로 N건씩")
    ap.add_argument("--ids", help="(review) 쉼표로 구분한 data_id. 판정 여부와 무관하게 이 건만 검수")
    ap.add_argument("--img-size", type=int, default=1024, help="(gen) 이미지 긴 변 픽셀")
    ap.add_argument("--parallel", type=int, help="(감독용 인자, 스크립트는 무시)")
    a = ap.parse_args()
    if a.batch_size is None:
        a.batch_size = KIND_BATCH[a.kind]
    if a.per_subtype and (a.kind != "gen" or a.mode != "review"):
        sys.exit("--per-subtype 는 --kind gen 의 review 에서만 씁니다.")

    site = Site()
    site.login()

    truth = None
    prefetched: dict[str, dict] = {}
    if a.mode == "pilot":
        truth = pick_pilot(site, a)
        ids = [i for i, _ in truth]
    elif a.ids:
        ids = [i.strip() for i in a.ids.split(",") if i.strip()]
        other = [i for i in ids if kind_of(i) != a.kind]
        if other:
            sys.exit(f"--kind {a.kind} 가 아닌 id: {other}")
        print(f"지정한 {len(ids)}건 대상 (판정 여부 무관)")
    else:
        # --limit 은 검수 대상(이 종류) 건수 기준이다: 다른 종류를 건너뛰고 limit 만큼 모은다
        items, total = site.list_ids(a.set, "pending", offset=a.offset)
        ids = [it["data_id"] for it in items if kind_of(it["data_id"]) == a.kind]
        skipped = len(items) - len(ids)
        if a.per_subtype:
            picked = pick_per_subtype(site, a, ids)
            prefetched = {r["label"]["data_id"]: r for r in picked}
            ids = list(prefetched)
        elif a.limit:
            ids = ids[: a.limit]
        print(f"미검수 전체 {total}건 중 {a.kind} {len(ids)}건 대상 (다른 종류 {skipped}건 건너뜀, offset {a.offset})")
    if not ids:
        sys.exit("대상 데이터가 없습니다.")

    tag = "" if a.kind == "text" else f"-{a.kind}"
    run = ROOT / "runs" / f"{a.mode}{tag}-{datetime.now():%Y%m%d-%H%M%S}"
    (run / "results").mkdir(parents=True)

    def fetch(i):
        try:
            if a.kind == "qa":
                return compact_qa(site.label_full(a.set, i))
            if a.kind in ("gen", "mm"):
                resp = prefetched.get(i) or site.label_full(a.set, i)
                item = compact_gen(resp) if a.kind == "gen" else compact_mm(resp)
                if item.get("image_id"):
                    out = run / "images" / f"{item['image_id']}.jpg"
                    try:
                        if not out.exists():
                            shrink_image(site.media(a.set, item["image_id"]), out, a.img_size)
                        item["image"] = str(out.relative_to(ROOT))
                    except Exception as e:  # 이미지를 못 받으면 에이전트가 이미지 없이 판정하지 않도록 표시
                        item["rule_flags"].append(f"IMAGE_FETCH_FAILED: {str(e)[:80]}")
                return item
            return compact_item(site.label(a.set, i))
        except Exception as e:  # 조회 실패 건도 묶음에 남겨 에이전트가 error 로 표시하게 한다
            return {"id": i, "fetch_error": str(e)[:200]}

    with ThreadPoolExecutor(a.workers) as ex:
        rows = list(ex.map(fetch, ids))
    failed = [r["id"] for r in rows if "fetch_error" in r]
    # 묶음은 글자 수로 나눈다: 에이전트가 Read 한 번에 다 읽어야 도구 호출(=대화 재전송)이 줄어든다
    chunks: list[list[dict]] = []
    size = 0
    for r in rows:
        n = len(dumps_line(r)) + 1
        if not chunks or size + n > a.max_chars or len(chunks[-1]) >= a.batch_size:
            chunks.append([])
            size = 0
        chunks[-1].append(r)
        size += n
    batches = []
    for k, chunk in enumerate(chunks, 1):
        name = f"batch_{k:03d}"
        write_jsonl(run / "batches" / f"{name}.jsonl", chunk)
        batches.append({"name": name, "ids": [r["id"] for r in chunk]})
    if truth:
        (run / "truth.tsv").write_text("".join(f"{i}\t{v}\n" for i, v in truth), encoding="utf-8")
    guide = (ROOT / KIND_GUIDE[a.kind]).read_bytes()
    manifest = {
        "mode": a.mode, "kind": a.kind, "agent": KIND_AGENT[a.kind], "guide": KIND_GUIDE[a.kind], "set": a.set, "created": datetime.now().isoformat(timespec="seconds"),
        "source": a.source if a.mode == "pilot" else "pending", "batch_size": a.batch_size, "max_chars": a.max_chars,
        "n_items": len(rows), "fetch_failed": failed,
        **({"img_size": a.img_size, "per_subtype": a.per_subtype} if a.kind in ("gen", "mm") else {}),
        "guide_sha1": hashlib.sha1(guide).hexdigest()[:12], "batches": batches,
    }
    (run / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    if failed:
        print(f"조회 실패 {len(failed)}건 (묶음에 fetch_error 로 남김): {failed[:5]}…")
    print(f"RUN {run.relative_to(ROOT)} kind={a.kind} agent={KIND_AGENT[a.kind]} batches={len(batches)} items={len(rows)}")


if __name__ == "__main__":
    main()
