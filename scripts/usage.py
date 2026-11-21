"""요금제 사용량 기록: `claude -p "/usage"` 결과를 읽어 실행 폴더에 남긴다.

  python scripts/usage.py runs/<run> --tag start   검수 에이전트를 띄우기 전
  python scripts/usage.py runs/<run> --tag end     모든 묶음이 끝난 뒤 (report.py 전)
  python scripts/usage.py                          기록 없이 현재 사용량만 출력

결과: runs/<run>/usage.jsonl (한 줄 = 측정 1회). report.py 가 start·end 차이를 보고서에 넣는다.
사용량을 읽지 못해도 종료 코드 0 (검수 흐름을 막지 않는다).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

LINE = re.compile(r"^(Current [^:]+):\s*(\d+(?:\.\d+)?)% used(?:\s*·\s*resets\s*(.+))?$")


def read_usage() -> dict:
    """{"session": %, "week": %, 그 밖의 한도: %, "resets": {...}} 또는 {"error": ...}"""
    try:
        out = subprocess.run(["claude", "-p", "/usage"], capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": str(e)[:200]}
    res: dict = {"resets": {}}
    for line in out.splitlines():
        m = LINE.match(line.strip())
        if not m:
            continue
        name, pct, reset = m.groups()
        key = ("session" if "session" in name.lower()
               else "week" if name.lower().startswith("current week (all")
               else re.sub(r"\W+", "_", name.lower().replace("current ", "")).strip("_"))
        res[key] = float(pct)
        if reset:
            res["resets"][key] = reset.strip()
    if "session" not in res and "week" not in res:
        return {"error": "사용량 줄을 찾지 못함: " + out.strip()[:200]}
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", nargs="?")
    ap.add_argument("--tag", default="", help="start | end | 임의 이름")
    a = ap.parse_args()
    u = read_usage()
    u = {"tag": a.tag, "at": datetime.now().isoformat(timespec="seconds"), **u}
    if a.run:
        with open(Path(a.run) / "usage.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(u, ensure_ascii=False) + "\n")
    if "error" in u:
        print(f"사용량 읽기 실패: {u['error']}")
    else:
        print(f"사용량[{a.tag or '-'}] 세션 {u.get('session', '?')}% · 주간 {u.get('week', '?')}%")


if __name__ == "__main__":
    main()
    sys.exit(0)
