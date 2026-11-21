"""공통 모듈: 사이트 접속, 규칙 검사, 판정 형식 정의."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SET = "텍스트 16차"
BASE_URL = os.environ.get("HERITAGE_URL", "https://heritage.ogqgyn.com")

VERDICTS = {"approve", "reject", "hold", "error"}
REASONS = {
    "설명이 사실과 다름",
    "클래스 분류 오류",
    "근거와 답이 불일치",
    "표현이 어색·불충분",
    "학습에 부적합한 원천",
    "한자·자막 오독",  # 생성형 이미지 문항(현판·주련 판독). 사이트 사유 목록과 같은 문자열
}
CONFIDENCE = {"high", "medium", "low"}
REQUIRED = ["data_id", "verdict", "evidence", "confidence"]  # reasons·comment·cross_chunk 는 기본값이면 생략


def load_env() -> None:
    """프로젝트 루트의 .env 를 환경변수로 읽는다 (python-dotenv 없이)."""
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Site:
    """라벨 뷰어 API 클라이언트. 로그인 세션 쿠키를 requests.Session 이 들고 다닌다."""

    def __init__(self, base_url: str = BASE_URL):
        import requests  # 지연 import: 오프라인 스크립트(validate/report)는 requests 없이 동작

        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"Origin": self.base, "Referer": self.base + "/"})

    def login(self) -> None:
        load_env()
        cookie = os.environ.get("HERITAGE_COOKIE")
        if cookie:  # "이름=값; 이름2=값2" 형식
            for part in cookie.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    self.s.cookies.set(k.strip(), v.strip())
        else:
            email, pw = os.environ.get("HERITAGE_EMAIL"), os.environ.get("HERITAGE_PASSWORD")
            if not email or not pw:
                sys.exit("로그인 정보가 없습니다. .env 에 HERITAGE_EMAIL, HERITAGE_PASSWORD 를 넣으세요 (.env.example 참고).")
            r = self.s.post(f"{self.base}/api/auth/login", json={"email": email, "password": pw}, timeout=30)
            if r.status_code >= 400:
                sys.exit(f"로그인 실패 ({r.status_code}): {r.text[:200]}")
        me = self.s.get(f"{self.base}/api/auth/me", timeout=30)
        if me.status_code >= 400:
            sys.exit(f"로그인 확인 실패 ({me.status_code}). 이메일·비밀번호 또는 쿠키를 확인하세요.")

    def _get(self, path: str, **params):
        r = self.s.get(f"{self.base}{path}", params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def list_ids(self, set_name: str, review: str, offset: int = 0, limit: int = 100000) -> tuple[list[dict], int]:
        """review: pending(미검수) | human(사람 저장) | claude | done | '' (전체). data_id 오름차순.
        서버가 한 번에 주는 건수를 제한해도 되도록 1,000건씩 나눠 받는다."""
        out: list[dict] = []
        total = None
        pos = offset
        while len(out) < limit:
            d = self._get("/api/labels", set=set_name, type="", status="", palace="", q="", review=review,
                          sample=0, cls="", sort="data_id", order="asc", only_multi=0, part="",
                          offset=pos, limit=min(1000, limit - len(out)))
            total = d["total"]
            if not d["items"]:
                break
            out += d["items"]
            pos += len(d["items"])
            if pos >= total:
                break
        return out, total or 0

    def label(self, set_name: str, data_id: str) -> dict:
        return self._get(f"/api/label/{data_id}", set=set_name)["label"]

    def label_full(self, set_name: str, data_id: str) -> dict:
        """라벨 응답 전체: {label, related{doc_id: 청크 라벨}, ...}. QA 근거 청크 본문은 related 에 있다."""
        return self._get(f"/api/label/{data_id}", set=set_name)

    def media(self, set_name: str, data_id: str) -> bytes:
        """원본 이미지(JPEG). 라벨 뷰어의 생성형 화면이 쓰는 주소와 같다."""
        r = self.s.get(f"{self.base}/media/{data_id}", params={"set": set_name}, timeout=120)
        r.raise_for_status()
        return r.content

    def review(self, set_name: str, data_id: str) -> dict:
        return self._get(f"/api/review/{data_id}", set=set_name)

    def post_review(self, set_name: str, data_id: str, verdict: str, reasons: list[str], comment: str) -> dict:
        r = self.s.post(f"{self.base}/api/review/{data_id}",
                        json={"set": set_name, "verdict": verdict, "reasons": reasons, "comment": comment}, timeout=60)
        r.raise_for_status()
        return r.json() if r.text else {}


# ---------- 규칙 검사 (기계적 경고) ----------
_NUM = re.compile(r"\d+(?:\.\d+)?")
HEDGE = re.compile(r"추정|추측|짐작|것으로 보인다|듯하다|로 보인다|가능성")


def _nums(s: str) -> set[str]:
    s = (s or "").replace(",", "")
    return set(_NUM.findall(s))


def rule_flags(label: dict) -> list[str]:
    c = label.get("content") or ""
    flags: list[str] = []
    for i, e in enumerate(label.get("entities") or []):
        t = c[e.get("start", 0):e.get("end", 0)]
        if t != e.get("name"):
            flags.append(f"ENT_POS_MISMATCH #{i} '{e.get('name')}' != '{t}'")
    for i, r in enumerate(label.get("entity_relations") or []):
        ev = r.get("evidence") or {}
        txt = ev.get("text") or ""
        if c[ev.get("start", 0):ev.get("end", 0)] != txt:
            flags.append(f"REL_EVIDENCE_POS_MISMATCH #{i}")
        if r.get("subject") and r["subject"] not in txt:
            flags.append(f"REL_SUBJECT_NOT_IN_EVIDENCE #{i} '{r['subject']}'")
        if r.get("object") and r["object"] not in txt:
            flags.append(f"REL_OBJECT_NOT_IN_EVIDENCE #{i} '{r['object']}'")
    # 본문 숫자: OCR 띄어쓰기로 쪼개진 숫자도 잡도록 공백 제거본도 함께 본다
    cn = _nums(c) | _nums(re.sub(r"\s+", "", c))
    hedged = bool(HEDGE.search(c))
    for k, v in (label.get("summaries") or {}).items():
        miss = sorted(_nums(v) - cn)
        if miss:
            flags.append(f"SUMMARY_NUMBER_NOT_IN_CONTENT {k}: {','.join(miss)}")
        if hedged and not HEDGE.search(v or ""):
            flags.append(f"CONTENT_HEDGED_BUT_SUMMARY_NOT {k}")
    return flags


EVIDENCE_HEAD = EVIDENCE_TAIL = 15


def _evidence_ref(content: str, ev: dict) -> str:
    """관계 근거가 본문 그대로면 "앞부분…끝부분"만 남긴다. 에이전트는 본문에서 그 구간 전체를 읽는다.
    근거가 여러 문장일 수 있으므로 끝부분도 남겨 구간 끝을 알 수 있게 한다.
    본문과 다르면(위치 불일치) 대조할 수 있도록 전문을 남긴다."""
    txt = ev.get("text") or ""
    if len(txt) > EVIDENCE_HEAD + EVIDENCE_TAIL + 6 and content[ev.get("start", 0):ev.get("end", 0)] == txt:
        return txt[:EVIDENCE_HEAD] + "…" + txt[-EVIDENCE_TAIL:]
    return txt


def compact_item(label: dict) -> dict:
    """검수 에이전트에 넘길 최소 필드만 남긴다 (판정에 쓰지 않는 필드는 뺀다)."""
    c = label.get("content") or ""
    return {
        "id": label["data_id"],
        "content": c,
        # 위치(start/end)는 rule_flags 가 대조하므로 넘기지 않는다
        "entities": [[e.get("type"), e.get("name")] for e in label.get("entities") or []],
        "relations": [[r.get("subject"), r.get("predicate"), r.get("object"), _evidence_ref(c, r.get("evidence") or {})]
                      for r in label.get("entity_relations") or []],
        "summaries": label.get("summaries") or {},
        "rule_flags": rule_flags(label),
    }


# ---------- 질의응답(QA) ----------
# 검수 종류: text = 텍스트 청크(엔티티·관계·요약), qa = 질의응답, gen = 생성형. 데이터 이름으로 나눈다.
KINDS = {"text", "qa", "gen"}
QA_CONTEXT = 200  # 근거 구간 앞뒤로 함께 넘길 청크 본문 글자 수


def is_qa(data_id: str) -> bool:
    """질의응답 데이터는 이름에 '_QA_' 가 들어 있다 (예: CDG_QA_000001)."""
    return "_QA_" in (data_id or "")


def is_gen(data_id: str) -> bool:
    """생성형 데이터는 이름에 '_GEN_' 이 들어 있다 (예: CDG_GEN_000001)."""
    return "_GEN_" in (data_id or "")


def is_mm(data_id: str) -> bool:
    """멀티모달 데이터는 이름에 '_MM_' 이 들어 있다 (예: CDG_MM_000001)."""
    return "_MM_" in (data_id or "")


def kind_of(data_id: str) -> str:
    if is_mm(data_id):
        return "mm"
    if is_gen(data_id):
        return "gen"
    return "qa" if is_qa(data_id) else "text"


def _qa_evidence(e: dict, related: dict) -> tuple[dict, list[str]]:
    """근거 1개를 에이전트용으로 줄인다. 청크 본문이 있으면 근거 구간을 【】로 표시한 앞뒤 문맥을 넘긴다."""
    flags: list[str] = []
    doc = e.get("doc_id") or ""
    txt = e.get("text") or ""
    chunk = (related.get(doc) or {}).get("content") if doc else None
    s, t = e.get("start_char"), e.get("end_char")
    out = {"doc": doc}
    if chunk and isinstance(s, int) and isinstance(t, int) and 0 <= s < t <= len(chunk):
        span = chunk[s:t]
        if txt and span != txt:
            flags.append("EVIDENCE_POS_MISMATCH")
            out["text"] = txt  # 위치가 어긋나면 저장된 근거 전문도 넘겨 대조하게 한다
        a, b = max(0, s - QA_CONTEXT), min(len(chunk), t + QA_CONTEXT)
        out["ctx"] = ("…" if a else "") + chunk[a:s] + "【" + span + "】" + chunk[t:b] + ("…" if b < len(chunk) else "")
    else:
        if not chunk:
            flags.append("EVIDENCE_CHUNK_MISSING")
        elif txt:
            flags.append("EVIDENCE_POS_MISSING")
        out["text"] = txt
    if not (out.get("text") or out.get("ctx")):
        flags.append("EVIDENCE_EMPTY")
    return out, flags


def compact_qa(resp: dict) -> dict:
    """QA 라벨 응답(label + related)을 검수 에이전트용 최소 필드로 줄이고 규칙 검사를 붙인다."""
    label = resp.get("label") or resp
    related = resp.get("related") or {}
    q = (label.get("question") or "").strip()
    ans = (label.get("answer") or "").strip()
    md = label.get("metadata") or {}
    evs, flags = [], []
    for i, e in enumerate(label.get("evidence") or []):
        ev, f = _qa_evidence(e, related)
        evs.append(ev)
        flags += [f"{x} #{i}" for x in f]
    if not q:
        flags.append("QA_EMPTY_QUESTION")
    if not ans:
        flags.append("QA_EMPTY_ANSWER")
    if not evs:
        flags.append("QA_NO_EVIDENCE")
    # 답의 숫자가 근거 청크(또는 근거 문장)에 있는지. OCR 띄어쓰기로 쪼개진 숫자도 잡도록 공백 제거본도 본다
    pool = " ".join(filter(None, [(related.get(e.get("doc_id") or "") or {}).get("content") or e.get("text") or ""
                                  for e in label.get("evidence") or []]))
    if evs:
        spans = " ".join(e.get("text") or "" for e in label.get("evidence") or [])
        pn = _nums(pool) | _nums(re.sub(r"\s+", "", pool)) | _nums(q)
        sn = _nums(spans) | _nums(re.sub(r"\s+", "", spans)) | _nums(q)
        miss = sorted(_nums(ans) - pn)
        if miss:  # 근거 청크 어디에도 없는 숫자
            flags.append(f"ANSWER_NUMBER_NOT_IN_EVIDENCE: {','.join(miss)}")
        outside = sorted(_nums(ans) - sn - set(miss))
        if outside:  # 청크에는 있지만 근거 구간 밖
            flags.append(f"ANSWER_NUMBER_OUTSIDE_SPAN: {','.join(outside)}")
        if HEDGE.search(spans) and not HEDGE.search(ans):
            flags.append("EVIDENCE_HEDGED_BUT_ANSWER_NOT")
    item = {"id": label.get("data_id"), "question": q, "answer": ans}
    for k in ("question_type", "answer_type"):
        if label.get(k):
            item[k] = label[k]
    meta = {k: md[k] for k in ("category", "difficulty", "user_type") if md.get(k)}
    if meta:
        item["meta"] = meta
    item["evidence"] = evs
    item["rule_flags"] = flags
    return item


# ---------- 생성형(GEN) ----------
# sub_type: image_based(이미지 문항: 객관식·주관식) | text_based(원천 텍스트로 쓴 해설문·대본·안내문 등)
GEN_SOURCE_MAX = 3000  # 원천 텍스트 청크 1개당 넘길 최대 글자 수


def gen_sub_type(label: dict) -> str:
    st = label.get("sub_type") or ""
    if st in ("image_based", "text_based"):
        return st
    return "image_based" if label.get("source_image_id") else "text_based"


def compact_gen(resp: dict) -> dict:
    """생성형 라벨 응답(label + related)을 검수 에이전트용 최소 필드로 줄이고 규칙 검사를 붙인다.
    이미지는 prepare.py 가 내려받아 줄인 뒤 item["image"] 에 파일 경로를 넣는다(여기서는 image_id 만 남긴다)."""
    label = resp.get("label") or resp
    related = resp.get("related") or {}
    st = gen_sub_type(label)
    prompt = (label.get("prompt") or "").strip()
    response = (label.get("response") or "").strip()
    choices = [c for c in (label.get("choices") or []) if isinstance(c, str)]
    key = (label.get("answer_key") or "").strip()
    qt = label.get("question_type") or ""
    flags: list[str] = []
    item: dict = {"id": label.get("data_id"), "sub_type": st}
    for k in ("content_type", "question_type", "user_type", "difficulty"):
        if label.get(k):
            item[k] = label[k]
    item["prompt"] = prompt
    item["response"] = response
    if choices:
        item["choices"] = choices
    if key and key != response:
        item["answer_key"] = key
    expl = (label.get("explanation") or "").strip()
    rub = (label.get("scoring_rubric") or "").strip()
    if expl:
        item["explanation"] = expl
    if rub and rub != expl:
        item["scoring_rubric"] = rub

    if not prompt:
        flags.append("GEN_EMPTY_PROMPT")
    if not response:
        flags.append("GEN_EMPTY_RESPONSE")
    # 문항 형식 검사 (객관식·주관식)
    if qt == "객관식" or choices:
        if qt and qt != "객관식":
            flags.append(f"QTYPE_HAS_CHOICES: {qt}")
        if not choices:
            flags.append("MCQ_NO_CHOICES")
        else:
            if len(set(choices)) < len(choices):
                flags.append("MCQ_DUPLICATE_CHOICES")
            ans = key or response
            if ans and ans not in choices:
                flags.append("MCQ_ANSWER_NOT_IN_CHOICES")
        if key and response and key != response:
            flags.append("MCQ_RESPONSE_NE_ANSWER_KEY")

    if st == "image_based":
        img_id = label.get("source_image_id") or ""
        img = related.get(img_id) or {}
        item["image_id"] = img_id
        info = {k: v for k, v in (("place", img.get("class_code_path") or label.get("class_code_path")),
                                  ("title", img.get("title")),
                                  ("caption", label.get("image_caption") or img.get("image_caption"))) if v}
        if info:
            item["image_info"] = info
        if not img_id:
            flags.append("GEN_NO_IMAGE")
    else:
        ids = label.get("source_text_id") or []
        ids = ids if isinstance(ids, list) else [ids]
        srcs = []
        for sid in ids:
            c = (related.get(sid) or {}).get("content")
            if c:
                srcs.append({"doc": sid, "content": c[:GEN_SOURCE_MAX] + ("…" if len(c) > GEN_SOURCE_MAX else "")})
            else:
                flags.append(f"SOURCE_TEXT_MISSING {sid}")
        item["sources"] = srcs
        if not ids:
            flags.append("GEN_NO_SOURCE_TEXT")
        if srcs and response:
            pool = " ".join(r["content"] for r in srcs)
            pn = _nums(pool) | _nums(re.sub(r"\s+", "", pool)) | _nums(prompt)
            miss = sorted(_nums(response) - pn)
            if miss:
                flags.append(f"RESPONSE_NUMBER_NOT_IN_SOURCE: {','.join(miss)}")
            if HEDGE.search(pool) and not HEDGE.search(response):
                flags.append("SOURCE_HEDGED_BUT_RESPONSE_NOT")
    item["rule_flags"] = flags
    return item


# ---------- 멀티모달(MM) ----------
# 이미지·영상 장면·텍스트 문단 중 2~3개를 한 유산 장면으로 묶은 데이터. 영상은 파일을 보지 않고 장면 설명(캡션)만 넘긴다.
PALACES = ("경복궁", "창덕궁", "창경궁", "덕수궁", "경희궁", "종묘", "사직단")


def compact_mm(resp: dict) -> dict:
    """멀티모달 라벨 응답을 검수 에이전트용 최소 필드로 줄이고 규칙 검사를 붙인다.
    이미지는 prepare.py 가 내려받아 줄인 뒤 item["image"] 에 파일 경로를 넣는다(여기서는 image_id 만 남긴다)."""
    label = resp.get("label") or resp
    m = label.get("modalities") or {}
    flags: list[str] = []
    item: dict = {"id": label.get("data_id")}
    for k, v in (("title", label.get("title")), ("place", label.get("class_code_path")),
                 ("heritage_name", label.get("heritage_name"))):
        if v:
            item[k] = v
    img, vid, txt = m.get("image") or {}, m.get("video") or {}, m.get("text") or {}
    if img:
        item["image_id"] = img.get("data_id") or ""
        if img.get("description"):
            item["image_caption"] = img["description"]
        if not item["image_id"]:
            flags.append("MM_IMAGE_ID_MISSING")
    if vid:
        item["video"] = {k: vid[k] for k in ("scene_id", "place", "start_time", "end_time", "description") if vid.get(k)}
        place = vid.get("place") or ""
        if not place:
            flags.append("VIDEO_PLACE_EMPTY")
        elif not any(p in place for p in PALACES):
            flags.append(f"VIDEO_PLACE_NO_PALACE: {place}")
        if not vid.get("description"):
            flags.append("VIDEO_CAPTION_EMPTY")
    if txt:
        item["text"] = {"doc": txt.get("doc_id"), "content": txt.get("content") or ""}
        if not txt.get("content"):
            flags.append("TEXT_CONTENT_EMPTY")
    if sum(1 for x in (img, vid, txt) if x) < 2:
        flags.append("MM_FEWER_THAN_2_MODALITIES")
    rels = []
    content = (txt.get("content") or "") if txt else ""
    for r in label.get("relations") or []:
        ev = r.get("evidence") or {}
        shared = ev.get("shared_entities") or ev.get("shared_objects") or []
        rels.append({k: v for k, v in (("type", r.get("type")), ("description", r.get("description")),
                                       ("shared", shared)) if v})
        if content and (r.get("type") or "").endswith("text"):
            # 괄호 앞 대표어(예: "기단·월대(갑석)" → 갑석·기단·월대)가 문단에 하나도 없으면 경고
            miss = []
            for s in shared:
                words = [w for w in re.split(r"[()·,\s]+", s) if len(w) >= 2]
                if words and not any(w in content for w in words):
                    miss.append(s)
            if miss:
                flags.append(f"SHARED_NOT_IN_TEXT: {','.join(miss)}")
    item["relations"] = rels
    if label.get("tags"):
        item["tags"] = label["tags"]
    item["rule_flags"] = flags
    return item


# ---------- 파일 도우미 ----------
def read_jsonl(p: Path) -> list[dict]:
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def write_jsonl(p: Path, rows: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(dumps_line(r) + "\n" for r in rows), encoding="utf-8")


def dumps_line(r: dict) -> str:
    return json.dumps(r, ensure_ascii=False, separators=(",", ":"))
