"""review_log 의 판정 내보내기(JSON)로 검수 통계를 낸다. 사이트에 저장하지 않는다.

  /opt/miniconda3/bin/python scripts/log_stats.py
  옵션: --dir review_log  --target 40400 (승인 목표)  --recent 7 (최근 활동일 수)
        --all-reviewers (관리자도 검수자 표에 표시)

- 날짜는 한국 시간으로 묶는다. 내보낸 날(파일 이름의 날짜)은 하루가 끝나지 않았으므로 평균에서 뺀다.
- 일 평균은 사람 판정만 센다. reviewer 가 claude 인 건은 사전 판정이라 뺀다.
- reviewer 가 빈 값인 건은 사람 판정이지만 누구인지 기록이 없어 "(이름 없음)"으로 묶는다.
- 관리자 목록은 사이트에서 읽는다. 읽지 못하면 경고를 내고 모두 표시한다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, Site  # noqa: E402

KST = timedelta(hours=9)
NO_NAME = "(이름 없음)"
PRE = "claude"
LABEL = {"approve": "보강 가능", "hold": "보류", "reject": "보강 불가"}


def load(folder: Path) -> tuple[list[dict], date | None]:
    rows, exported = [], None
    for f in sorted(folder.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        rows += d["reviews"]
        m = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
        if m:
            day = date.fromisoformat(m.group(1))
            exported = max(exported, day) if exported else day
    for r in rows:
        t = datetime.strptime(r["at"], "%Y-%m-%dT%H:%M:%SZ") + KST
        r["_day"], r["_hour"] = t.date(), t.hour
        r["_who"] = r.get("reviewer") or NO_NAME
    return [r for r in rows if r.get("verdict") in LABEL], exported


def admins() -> set[str]:
    try:
        site = Site()
        site.login()
        users = site._get("/api/admin/users")
        users = users if isinstance(users, list) else next(v for v in users.values() if isinstance(v, list))
        return {u["name"] for u in users if u.get("role") == "admin"}
    except (Exception, SystemExit) as e:
        print(f"경고: 관리자 목록을 읽지 못해 모두 표시합니다 ({e})")
        return set()


def pct(a: int, b: int) -> str:
    return f"{a / b:.0%}" if b else "-"


def table(head: list[str], body: list[list]) -> None:
    print("| " + " | ".join(head) + " |\n|" + "---|" * len(head))
    for row in body:
        print("| " + " | ".join(f"{x:,}" if isinstance(x, int) else str(x) for x in row) + " |")
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default="review_log")
    ap.add_argument("--target", type=int, default=40400, help="승인 목표 건수 (이미지 기본 40,400)")
    ap.add_argument("--recent", type=int, default=7)
    ap.add_argument("--all-reviewers", action="store_true")
    a = ap.parse_args()
    rows, exported = load(ROOT / a.dir)
    if not rows:
        sys.exit(f"{a.dir} 에 판정 기록이 없습니다.")
    human = [r for r in rows if r["_who"] != PRE]
    full = [r for r in human if r["_day"] != exported]  # 끝난 날만

    print(f"# 검수 통계 ({exported} 내보내기, 판정 {len(rows):,}건)\n")
    print("## 세트별")
    body = []
    for n in sorted({r["set"] for r in rows}):
        s = [r for r in rows if r["set"] == n]
        c = Counter(r["verdict"] for r in s)
        body.append([n, len(s), c["approve"], c["hold"], c["reject"], pct(c["approve"], len(s)),
                     f"{min(r['_day'] for r in s)} ~ {max(r['_day'] for r in s)}"])
    table(["세트", "판정", "보강 가능", "보류", "보강 불가", "승인율", "기간"], body)

    per_day = Counter(r["_day"] for r in full)
    days = sorted(per_day)
    span = (days[-1] - days[0]).days + 1
    weekdays = sum(1 for i in range(span) if (days[0] + timedelta(i)).weekday() < 5)
    recent = days[-a.recent:]
    ok_day = Counter(r["_day"] for r in full if r["verdict"] == "approve")
    recent_n = sum(per_day[d] for d in recent)
    recent_ok = sum(ok_day[d] for d in recent)
    top = max(days, key=lambda d: per_day[d])
    print(f"## 하루 평균 (사람 판정, {days[0]} ~ {days[-1]}, {exported} 제외)")
    table(["기준", "일수", "검수/일", "승인/일"], [
        ["검수한 날", len(days), round(len(full) / len(days)), round(sum(ok_day.values()) / len(days))],
        ["평일", weekdays, round(len(full) / weekdays), round(sum(ok_day.values()) / weekdays)],
        [f"최근 {len(recent)}일 (검수한 날)", len(recent), round(recent_n / len(recent)), round(recent_ok / len(recent))],
    ])
    print(f"가장 많은 날: {top} {per_day[top]:,}건\n")

    print("## 일별 (사람 판정)")
    by_day = defaultdict(Counter)
    for r in human:
        by_day[r["_day"]][r["verdict"]] += 1
        by_day[r["_day"]]["set_" + r["set"]] += 1
    body = []
    for d in sorted(by_day):
        c = by_day[d]
        tag = " (진행 중)" if d == exported else ""
        body.append([f"{d} {'월화수목금토일'[d.weekday()]}{tag}", c["approve"] + c["hold"] + c["reject"], c["approve"], c["hold"], c["reject"]])
    table(["날짜", "검수", "보강 가능", "보류", "보강 불가"], body)

    skip = set() if a.all_reviewers else admins()
    print("## 검수자별" + ("" if a.all_reviewers else " (관리자 제외)"))
    body = []
    for who in sorted({r["_who"] for r in human} - skip, key=lambda w: -sum(r["_who"] == w for r in human)):
        s = [r for r in human if r["_who"] == who]
        c = Counter(r["verdict"] for r in s)
        ds = Counter(r["_day"] for r in s if r["_day"] != exported)
        avg = round(sum(ds.values()) / len(ds)) if ds else "-"
        body.append([who, len(s), len(ds), avg, max(ds.values()) if ds else "-", pct(c["approve"], len(s)), pct(c["hold"], len(s)), pct(c["reject"], len(s))])
    table(["검수자", "검수", "검수한 날", "일 평균", "하루 최대", "승인율", "보류율", "불가율"], body)

    print("## 시간대별 (사람 판정, 한국 시간)")
    hours = Counter(r["_hour"] for r in human)
    table(["시", "검수", "비율"], [[f"{h:02d}시", hours[h], pct(hours[h], len(human))] for h in sorted(hours)])

    print("## 사유 상위")
    for v in ("hold", "reject"):
        s = [r for r in rows if r["verdict"] == v]
        c = Counter(x for r in s for x in (r.get("reasons") or []))
        table([f"{LABEL[v]} 사유", "건수", f"{LABEL[v]} 중 비율"], [[k, n, pct(n, len(s))] for k, n in c.most_common(8)])

    ok = sum(r["verdict"] == "approve" for r in rows)
    left = max(a.target - ok, 0)
    print("## 목표까지")
    per = recent_ok / len(recent)
    print(f"승인 {ok:,} / 목표 {a.target:,} ({pct(ok, a.target)}) · 남은 승인 {left:,}건")
    if per:
        print(f"최근 {len(recent)}일 속도(승인 {per:,.0f}건/일)면 검수하는 날 기준 약 {left / per:.0f}일 필요")


if __name__ == "__main__":
    main()
