"""집계 보고서: 결과를 합쳐 report.md, results.csv, results_all.jsonl 을 만든다.
실행 폴더에 truth.tsv 가 있으면(pilot) 정답과의 일치율도 계산한다.

  python scripts/report.py runs/<run>
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import write_jsonl  # noqa: E402
from validate import check_batch  # noqa: E402

V = {"approve": "보강가능", "reject": "보강불가", "hold": "보류", "error": "오류"}


def usage_section(run: Path, n: int, lines: list[str]) -> str:
    """usage.jsonl(scripts/usage.py)의 마지막 start·end 측정으로 이번 실행의 사용량 증가분을 계산한다."""
    p = run / "usage.jsonl"
    if not p.exists():
        return ""
    recs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    st = next((r for r in reversed(recs) if r.get("tag") == "start" and "error" not in r), None)
    en = next((r for r in reversed(recs) if r.get("tag") == "end" and "error" not in r), None)
    lines += ["", "## 요금제 사용량 (claude /usage)"]
    for r in recs:
        lines.append(f"- {r.get('tag') or '-'} {r.get('at')}: " + (f"읽기 실패 ({r['error'][:60]})" if "error" in r
                     else f"세션 {r.get('session', '?')}% · 주간 {r.get('week', '?')}%"))
    if not (st and en):
        return ""
    out = []
    for k, name in (("session", "세션"), ("week", "주간")):
        if k in st and k in en:
            d = en[k] - st[k]
            reset = st.get("resets", {}).get(k) != en.get("resets", {}).get(k)
            note = " (도중에 초기화돼 증가분 계산 불가)" if reset or d < 0 else ""
            per = f", 100건당 {d / n * 100:.1f}%p" if n and not note else ""
            out.append(f"{name} +{d:g}%p{per}{note}")
    line = "사용량 증가: " + " · ".join(out)
    lines.append(f"- {line}")
    lines.append("- 다른 세션·작업의 사용량도 함께 잡힌다(근사치).")
    return line


def main():
    if len(sys.argv) < 2:
        sys.exit("사용법: python scripts/report.py runs/<run>")
    run = Path(sys.argv[1])
    m = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    rows, failed = [], {}
    for b in m["batches"]:
        errs = check_batch(run, b)
        p = run / "results" / f"{b['name']}.jsonl"
        if errs:
            failed[b["name"]] = errs
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                    if r.get("data_id") in b["ids"]:
                        rows.append(r)
                except json.JSONDecodeError:
                    pass
    truth = {}
    tp = run / "truth.tsv"
    if tp.exists():
        for line in tp.read_text(encoding="utf-8").splitlines():
            if line.strip():
                i, v = line.split("\t")[:2]
                truth[i] = v.strip()

    write_jsonl(run / "results_all.jsonl", rows)
    with open(run / "results.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["data_id", "판정", "사유", "코멘트", "근거", "확신도", "앞뒤청크보충"] + (["정답", "일치"] if truth else []))
        for r in rows:
            row = [r.get("data_id"), V.get(r.get("verdict"), r.get("verdict")), "; ".join(r.get("reasons") or []),
                   r.get("comment", ""), r.get("evidence", ""), r.get("confidence", ""), r.get("cross_chunk", False)]
            if truth:
                t = truth.get(r.get("data_id"), "")
                row += [V.get(t, t), "O" if t == r.get("verdict") else "X"]
            w.writerow(row)

    n = len(rows)
    vc = Counter(r.get("verdict") for r in rows)
    rc = Counter(x for r in rows for x in (r.get("reasons") or []))
    cc = Counter(r.get("confidence") for r in rows)
    lines = [f"# 검수 보고서 — {run.name}", "",
             f"- 모드: {m['mode']} · 종류: {m.get('kind', 'text')} · 세트: {m['set']} · 지침 해시: {m.get('guide_sha1')} · 생성: {m['created']}",
             f"- 대상 {m['n_items']}건 · 결과 {n}건 · 묶음 {len(m['batches'])}개 (실패 {len(failed)}개)", "",
             "## 판정 분포", "| 판정 | 건수 | 비율 |", "|---|---|---|"]
    for k in ["approve", "reject", "hold", "error"]:
        lines.append(f"| {V[k]} | {vc.get(k, 0)} | {vc.get(k, 0) / max(1, n):.0%} |")
    lines += ["", "## 사유 분포"] + [f"- {k}: {v}" for k, v in rc.most_common()]
    lines += ["", f"## 확신도: high {cc.get('high', 0)} · medium {cc.get('medium', 0)} · low {cc.get('low', 0)}",
              f"- 앞뒤 청크 보충(cross_chunk): {sum(1 for r in rows if r.get('cross_chunk'))}건"]

    usage_line = usage_section(run, n, lines)
    summary = f"결과 {n}/{m['n_items']}건 · " + " · ".join(f"{V[k]} {vc.get(k, 0)}" for k in ["approve", "reject", "hold", "error"])
    if truth:
        both = [r for r in rows if r.get("data_id") in truth]
        agree = sum(r["verdict"] == truth[r["data_id"]] for r in both)
        binary = sum((r["verdict"] == "approve") == (truth[r["data_id"]] == "approve") for r in both)
        cm = Counter((truth[r["data_id"]], r["verdict"]) for r in both)
        lines += ["", "## 정답 비교",
                  f"- 전체 일치 {agree}/{len(both)} = {agree / max(1, len(both)):.0%}",
                  f"- 이진 일치(보강가능 vs 그 외) {binary}/{len(both)} = {binary / max(1, len(both)):.0%}",
                  "", "| 정답 → 자동 | 건수 |", "|---|---|"]
        lines += [f"| {V.get(a, a)} → {V.get(b, b)} | {c} |" for (a, b), c in sorted(cm.items())]
        lines += ["", "### 불일치 목록"]
        for r in both:
            t = truth[r["data_id"]]
            if r["verdict"] != t:
                lines.append(f"- `{r['data_id']}` 정답 {V.get(t, t)} / 자동 {V.get(r['verdict'])} "
                             f"[{', '.join(r.get('reasons') or [])}] ({r.get('confidence')}) {r.get('comment', '')} — 근거: {r.get('evidence', '')}")
        summary += f" · 일치 {agree / max(1, len(both)):.0%} (이진 {binary / max(1, len(both)):.0%})"
    low = [r for r in rows if r.get("confidence") == "low"]
    if low:
        lines += ["", f"## low 확신도 {len(low)}건 (사람 확인 권장)"]
        lines += [f"- `{r['data_id']}` {V.get(r['verdict'])} — {r.get('evidence', '')}" for r in low[:100]]
    if failed:
        lines += ["", "## 실패 묶음 (/resume-run 으로 재실행)"] + [f"- {k}: {' | '.join(v[:3])}" for k, v in failed.items()]
    (run / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(summary)
    if usage_line:
        print(usage_line)
    print(f"보고서: {run / 'report.md'}")


if __name__ == "__main__":
    main()
