"""검수 현황 집계: 시트 검수 탭 기준으로 유형별 목표치·현재(승인)·진척률을 출력한다. 읽기 전용.

  /opt/miniconda3/bin/python scripts/status.py

- 현재 = 승인(보강 가능) 건수. 영상은 승인된 영상의 키프레임(장면) 수. mp4/avi 쌍은 따로 센다.
- 직전 실행 값은 runs/status_last.json 에 두고 증감을 함께 보여 준다.
- 오늘(자정 이후) 판정을 검수자별로 센다. 관리자(role=admin)는 표에서 뺀다.
  검수자는 건별 조회라 runs/status_reviewers.json 에 캐시한다.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, Site  # noqa: E402

LAST = ROOT / "runs" / "status_last.json"
CACHE = ROOT / "runs" / "status_reviewers.json"
ROWS = [  # 유형, 목표치, 세트, 라벨 종류
    ("이미지", 40400, ["이미지 15차", "이미지_CL2"], "image"),
    ("영상(키프레임)", 100, ["영상_CL"], "video"),
    ("텍스트", 3000, ["텍스트 16차", "텍스트_CL2"], "text"),
    ("QA·RAG", 3000, ["질의응답 16차", "질의응답 15차"], "qa"),
    ("생성형", 3000, ["생성형 5차", "생성형_CL2"], "generative"),
    ("멀티모달", 500, ["멀티모달_CL"], "multimodal"),
]


def keyframes(site: Site) -> int:
    items, _ = site.list_ids("영상_CL", "")
    total = 0
    for it in items:
        if it.get("verdict") == "approve":
            scenes = site.label("영상_CL", it["data_id"]).get("scenes") or []
            total += sum(1 for x in scenes if isinstance(x.get("keyframe"), dict))
    return total


def today_reviews(site: Site) -> Counter:
    """오늘 판정을 (검수자, 세트, 판정)별로 센다. reviewed_at 이 바뀐 건만 다시 조회한다."""
    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    start = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    cache: dict[str, list] = {}
    for n in [s for r in ROWS for s in r[2]]:
        items, _ = site.list_ids(n, "")
        todo = []
        for it in items:
            at = it.get("reviewed_at") or ""
            if it.get("verdict") and at >= start:
                key = f"{n}|{it['data_id']}"
                if key in old and old[key][2] == at:
                    cache[key] = old[key]
                else:
                    todo.append((key, it, at))

        def fetch(t):
            x = site.review(n, t[1]["data_id"])
            return t[0], [x.get("reviewer") or x.get("by") or "?", x.get("verdict"), t[2]]

        with ThreadPoolExecutor(8) as ex:
            cache.update(ex.map(fetch, todo))
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return Counter((v[0], k.split("|")[0], v[1]) for k, v in cache.items())


def main():
    site = Site()
    site.login()
    prev = json.loads(LAST.read_text(encoding="utf-8")) if LAST.exists() else {}
    now: dict[str, int] = {}
    sets: dict[str, dict] = {}
    for name, _, names, kind in ROWS:
        for n in names:
            sets[n] = site._get("/api/review-stats", set=n)["by_type"].get(kind, {})
        now[name] = sum(sets[n].get("approve", 0) for n in names)
    now["영상(키프레임)"] = keyframes(site)
    now["소계"] = sum(now[r[0]] for r in ROWS)

    print(f"{datetime.now():%m/%d %H:%M} 기준" + (f" · 직전 {prev['at']}" if prev.get("at") else ""))
    print("| 유형 | 목표치 | 현재 (승인) | 진척률 | 직전 대비 |\n|---|---|---|---|---|")
    for name, goal, _, _ in [*ROWS, ("소계", sum(r[1] for r in ROWS), [], "")]:
        d = now[name] - prev[name] if name in prev else None
        delta = "-" if d is None else f"{d:+,}"
        print(f"| {name} | {goal:,} | {now[name]:,} | {now[name] / goal:.0%} | {delta} |")
    print("\n유형별 판정 (데이터 건수, 영상은 영상 수)\n| 유형 | 전체 | 보강 가능 | 보류 | 보강 불가 | 미검수 |\n|---|---|---|---|---|---|")
    tot = Counter()
    for name, _, names, _ in ROWS:
        c = Counter()
        for n in names:
            s = sets[n]
            c.update({k: s.get(k, 0) for k in ("total", "approve", "hold", "reject")})
            c["pending"] += s.get("total", 0) - s.get("reviewed", 0)
        tot.update(c)
        print(f"| {name} | {c['total']:,} | {c['approve']:,} | {c['hold']:,} | {c['reject']:,} | {c['pending']:,} |")
    print(f"| 합계 | {tot['total']:,} | {tot['approve']:,} | {tot['hold']:,} | {tot['reject']:,} | {tot['pending']:,} |\n")
    img = [sets[n] for n in ROWS[0][2]]
    left = max(ROWS[0][1] - now["이미지"], 0)
    pending = sum(s.get("total", 0) - s.get("reviewed", 0) for s in img)
    print(f"이미지: 승인 {left:,}건 더 필요 · 미검수 {pending:,} · 보류 {sum(s.get('hold', 0) for s in img):,}")

    day = f"{datetime.now():%Y-%m-%d}"
    who = today_reviews(site)
    users = site._get("/api/admin/users")
    users = users if isinstance(users, list) else next(v for v in users.values() if isinstance(v, list))
    admins = {u["name"] for u in users if u.get("role") == "admin"}
    done: dict[str, dict] = {}
    for (rv, n, v), c in who.items():
        if rv in admins:
            continue
        row = done.setdefault(f"{rv}|{n}", {"approve": 0, "hold": 0, "reject": 0})
        row[v] = row.get(v, 0) + c
    before = prev.get("reviewers", {}) if prev.get("day") == day else {}
    print("\n오늘 검수자별 (관리자 제외)\n| 검수자 | 세트 | 검수 | 보강 가능 | 보류 | 보강 불가 | 직전 대비 |\n|---|---|---|---|---|---|---|")
    for k, r in sorted(done.items(), key=lambda x: -sum(x[1].values())):
        rv, n = k.split("|")
        total = sum(r.values())
        d = total - sum(before[k].values()) if k in before else total
        print(f"| {rv} | {n} | {total:,} | {r['approve']:,} | {r['hold']:,} | {r['reject']:,} | {d:+,} |")

    LAST.write_text(json.dumps({**now, "reviewers": done, "day": day, "at": f"{datetime.now():%m/%d %H:%M}"}, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
