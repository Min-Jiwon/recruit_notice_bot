#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""엘리드 / 대한피부과학연구소(KDRI) 임상시험 모집공고 신규 알림 봇.

- 두 게시판의 목록을 읽어 이전 실행 이후 새로 올라온 글을 찾는다.
- 등록해 둔 가족 구성원 중 한 명이라도 조건에 맞으면 텔레그램으로 보낸다.
- 이미 본 글 번호는 seen.json 에 남긴다.

환경변수:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (없으면 화면 출력만 하는 연습 모드)
  PEOPLE   "나:여성:38, 첫째:남성:6, 둘째:여성:18개월" 형식 (config.json 보다 우선)
"""

import html
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "seen.json"

ELLEAD_LIST = "https://www.ellead.com/board/recruitment"
KDRI_LIST = "https://www.kdri.co.kr/bbs/board.php?bo_table=participation"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 목록에서 이런 상태로 표시된 글은 건너뛴다
CLOSED_WORDS = ("마감", "종료", "완료")

# 아이 대상으로 보이는 공고를 놓치지 않기 위한 안전망
# "학생"·"자녀" 는 일반 안내문에도 흔히 나와서 넣지 않는다 (공지글이 걸려든다)
CHILD_WORDS = ("유아", "영유아", "영아", "어린이", "소아", "아동",
               "키즈", "미취학", "초등학", "중학생", "고등학생", "청소년")

# 나이 상한이 사실상 없다는 뜻으로 쓰는 값
NO_LIMIT = 120.0

# 글 번호는 최근 것만 들고 있으면 충분하다
KEEP_IDS = 500


# ---------------------------------------------------------------- 기본 도구

def fetch(url, retries=3):
    """페이지를 받아 문자열로 돌려준다. 인코딩은 utf-8 → cp949 순으로 시도."""
    last = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ko-KR,ko;q=0.9",
            })
            with urlopen(req, timeout=30) as resp:
                raw = resp.read()
            for enc in ("utf-8", "cp949"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", "replace")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{url} 접속 실패: {last}")


def strip_tags(fragment):
    """HTML 조각에서 사람이 읽을 텍스트만 뽑는다."""
    text = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("​", "").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def preview(page):
    """사이트가 실제로 뭘 돌려줬는지 알 수 있게 앞부분만 요약한다."""
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    head = f"제목={strip_tags(title.group(1))[:60]} / " if title else ""
    text = re.sub(r"\s+", " ", strip_tags(page))
    return f"{head}길이={len(page)}자 / 내용앞부분: {text[:250]}"


def squash(text):
    """공백을 전부 지운 비교용 문자열. 사이트마다 띄어쓰기가 제멋대로라서 필요하다."""
    return re.sub(r"\s+", "", text or "")


# ------------------------------------------------------- 나이 / 성별 읽어내기

DASH = r"[-~∼～–—]"

# 범위로 적힌 경우. 양쪽 단위를 따로 읽어서 "6개월~5세" 처럼 섞인 표기도 받는다.
RANGE_RE = re.compile(rf"(만)?(\d{{1,3}})(개월|세)?{DASH}만?(\d{{1,3}})(개월|세)?")

# "1~2개월 사용" 같은 기간 표현을 나이로 오해하지 않도록, 개월로만 적힌 범위는
# 주변에 나이 이야기라는 단서가 있을 때만 받아들인다.
AGE_CUES_BEFORE = ("생후", "나이", "연령", "대상", "자격",
                   "유아", "영아", "아동", "어린이", "소아")
# 뒤쪽은 "사용 후 대상자" 처럼 엉뚱하게 걸릴 수 있어 아이를 가리키는 말만 본다.
AGE_CUES_AFTER = ("유아", "영아", "아동", "어린이", "소아", "여아", "남아")

# 한쪽만 적힌 경우
OPEN_PATTERNS = [
    (re.compile(r"만?(\d{1,2})세이상"), lambda n: (n, NO_LIMIT)),
    (re.compile(r"만?(\d{1,2})세미만"), lambda n: (0.0, n - 1)),
    (re.compile(r"만?(\d{1,2})세이하"), lambda n: (0.0, float(n))),
    (re.compile(r"(\d{1,3})개월이상"), lambda n: (n / 12.0, NO_LIMIT)),
    (re.compile(r"(\d{1,3})개월미만"), lambda n: (0.0, n / 12.0)),
    (re.compile(r"(\d{1,3})개월이하"), lambda n: (0.0, n / 12.0)),
]


def parse_age(text):
    """모집 연령을 (최소, 최대) 나이(살)로 돌려준다. 못 읽으면 None.

    개월로 적힌 유아 공고도 살 단위로 바꿔서 함께 다룬다.
    """
    flat = squash(text)

    for m in RANGE_RE.finditer(flat):
        man, lo_num, lo_unit, hi_num, hi_unit = m.groups()
        # 양쪽 다 단위가 없으면 날짜 범위일 수 있다 ("09/07~09/11"). 건너뛴다.
        if not lo_unit and not hi_unit:
            continue
        # 한쪽만 적힌 단위는 반대쪽에서 빌려온다 ("6~36개월", "20-60세")
        lo_unit = lo_unit or hi_unit
        hi_unit = hi_unit or lo_unit

        # 세 없이 개월로만 적힌 범위는 기간 표현("1~2개월 사용")일 수 있다.
        if "세" not in (lo_unit, hi_unit) and not man:
            before = flat[max(0, m.start() - 25):m.start()]
            after = flat[m.end():m.end() + 15]
            if not (any(c in before for c in AGE_CUES_BEFORE)
                    or any(c in after for c in AGE_CUES_AFTER)):
                continue

        lo = int(lo_num) / (12.0 if lo_unit == "개월" else 1.0)
        hi = int(hi_num) / (12.0 if hi_unit == "개월" else 1.0)
        if 0 <= lo <= hi <= NO_LIMIT:
            return lo, hi

    for pattern, build in OPEN_PATTERNS:
        m = pattern.search(flat)
        if m:
            lo, hi = build(int(m.group(1)))
            if lo <= hi:
                return float(lo), float(hi)

    return None


def format_age(span):
    """(최소, 최대) 를 사람이 읽는 문구로."""
    if not span:
        return "확인필요"
    lo, hi = span

    def one(v):
        if v < 1:
            return f"{round(v * 12)}개월"
        return f"{int(v)}세"

    if hi >= NO_LIMIT:
        return f"{one(lo)} 이상"
    if lo <= 0:
        return f"{one(hi)} 이하"
    return f"{one(lo)}~{one(hi)}"


def format_person_age(age):
    """등록된 사람의 나이를 보기 좋게. 0.5 → '6개월', 38.0 → '38세'"""
    if age < 1:
        return f"{round(age * 12)}개월"
    if age != int(age):
        return f"{age:g}세"
    return f"{int(age)}세"


# "남아" 는 "남아있는" 처럼 다른 뜻으로도 쓰여서 뒤 글자를 확인한다
MALE_CHILD = re.compile(r"남아(?!있|서|도|나|나요|주|줘)")


def parse_gender(text):
    """'여성' / '남성' / '무관' 또는 알 수 없으면 None."""
    flat = squash(text)
    if "남녀" in flat or "여남" in flat:
        return "무관"
    male = "남성" in flat or "남자" in flat or bool(MALE_CHILD.search(flat))
    female = "여성" in flat or "여자" in flat or "여아" in flat
    if male and female:
        return "무관"
    if female:
        return "여성"
    if male:
        return "남성"
    return None


def looks_like_child_trial(post):
    """제목이나 본문이 아이 대상 시험으로 보이는지."""
    haystack = squash(post["title"] + " " + post.get("age_text", ""))
    return any(word in haystack for word in CHILD_WORDS)


# ------------------------------------------------------------------- 엘리드

def fetch_ellead():
    page = fetch(ELLEAD_LIST)
    body = re.search(r"<tbody>(.*?)</tbody>", page, re.S)
    if not body:
        raise RuntimeError("엘리드: 목록 표를 찾지 못했습니다. " + preview(page))

    posts = []
    for row in re.findall(r"<tr[^>]*>.*?</tr>", body.group(1), re.S):
        pid = re.search(r"/board/recruitment/(\d+)", row)
        if not pid:
            continue

        subject = re.search(r'<td class="subject[^"]*">(.*?)</td>', row, re.S)
        block = subject.group(1) if subject else row

        title_m = re.search(r"<span[^>]*>(.*?)</span>", block, re.S)
        title = strip_tags(title_m.group(1)) if title_m else "(제목 없음)"

        info = {}
        for key, val in re.findall(r"<p>(.*?)\s*:\s*<span>(.*?)</span>\s*</p>", block, re.S):
            info[strip_tags(key)] = strip_tags(val)

        status_m = re.search(r'class="status"><span[^>]*>(.*?)</span>', row, re.S)
        status = strip_tags(status_m.group(1)) if status_m else ""

        posts.append({
            "site": "엘리드",
            "id": pid.group(1),
            "title": title,
            "url": f"https://www.ellead.com/board/recruitment/{pid.group(1)}",
            "status": status,
            "age_text": info.get("나이", ""),
            "gender_text": info.get("성별", ""),
            "pay": info.get("피험비", ""),
        })
    return posts


# --------------------------------------------------------------------- KDRI

def fetch_kdri():
    page = fetch(KDRI_LIST)
    body = re.search(r"<tbody>(.*?)</tbody>", page, re.S)
    if not body:
        raise RuntimeError("KDRI: 목록 표를 찾지 못했습니다. " + preview(page))

    posts = []
    for row in re.findall(r"<tr[^>]*>.*?</tr>", body.group(1), re.S):
        wid = re.search(r"wr_id=(\d+)", row)
        if not wid:
            continue
        tit = re.search(r'<div class="bo_tit">(.*?)</div>', row, re.S)
        if not tit:
            continue
        raw = re.sub(r'<span class="sound_only">.*?</span>', " ", tit.group(1), flags=re.S)
        title = strip_tags(raw)
        if not title:
            continue
        posts.append({
            "site": "KDRI",
            "id": wid.group(1),
            "title": title,
            "url": f"{KDRI_LIST}&wr_id={wid.group(1)}",
            "status": "",
            "age_text": "",
            "gender_text": "",
            "pay": "",
        })
    return posts


def enrich_kdri(post):
    """KDRI 는 목록에 조건이 없어서 상세페이지에서 지원 자격을 읽어온다."""
    try:
        page = fetch(post["url"])
    except RuntimeError as exc:
        # 조건을 못 읽었으니 뒤에서 전원 매칭으로 넘어간다. 알림에 그 사실을 밝힌다.
        post["detail_failed"] = True
        print(f"  ! 상세 조회 실패({post['id']}): {exc}", file=sys.stderr)
        return

    con = re.search(r'id="bo_v_con"(.*?)<!--', page, re.S)
    text = strip_tags(con.group(1) if con else page)

    # "지원 자격" 은 본문에 여러 번 나온다 ("지원 자격과 주의사항을 읽어보시고" 처럼
    # 안내 문구인 경우도 있다). 나이가 실제로 적힌 문단을 고르고, 없으면 본문 전체를 본다.
    scope = text
    for m in re.finditer(r"지원\s*자격", text):
        candidate = text[m.end():m.end() + 600]
        if parse_age(candidate):
            scope = candidate
            break

    post["age_text"] = scope
    # 성별은 자격 문단에 없는 경우가 잦아 본문 전체로 한 번 더 찾는다.
    post["gender_text"] = scope if parse_gender(scope) else text

    pay = re.search(r"참여비(.{0,120})", text, re.S)
    if pay:
        money = re.search(r"([\d,]+\s*만?\s*원)", pay.group(1))
        if money:
            post["pay"] = squash(money.group(1))


# --------------------------------------------------------------- 조건 맞추기

def match(post, people):
    """조건에 맞는 사람 이름 목록과, 아무도 없을 때의 사유를 돌려준다.

    조건을 읽어내지 못한 항목은 놓치는 것보다 낫다고 보고 통과시킨다.
    """
    if any(w in post["status"] for w in CLOSED_WORDS):
        return [], f"상태: {post['status']}"

    gender = parse_gender(post["gender_text"])
    span = parse_age(post["age_text"])
    post["gender_norm"] = gender or "확인필요"
    post["age_norm"] = format_age(span)
    post["child_hint"] = looks_like_child_trial(post)

    # 엘리드는 목록에 나이·성별을 항상 적어둔다. 둘 다 "시험마다 상이" 인 글은
    # 모집공고가 아니라 상단 고정 공지(필독 사항 등)라서 알리지 않는다.
    if post["site"] == "엘리드" and not gender and not span and not post["child_hint"]:
        return [], "공지글"

    hits = []
    for person in people:
        if gender and gender != "무관" and gender != person["gender"]:
            continue
        if span and not (span[0] <= person["age"] <= span[1]):
            continue
        hits.append(person["name"])

    if hits:
        return hits, ""

    # 나이를 아예 못 읽었는데 아이 대상으로 보이면, 표기를 놓친 것일 수 있으니
    # 아이들에게 보낸다. 나이를 제대로 읽고 아무도 안 맞은 경우까지 보내면
    # 알림이 의미를 잃으므로, span 을 못 읽은 경우로 한정한다.
    if span is None and post["child_hint"]:
        kids = [p["name"] for p in people
                if p["age"] < 19
                and (not gender or gender == "무관" or gender == p["gender"])]
        if kids:
            return kids, ""

    if gender and gender != "무관" and all(p["gender"] != gender for p in people):
        return [], f"{gender} 전용"
    return [], f"모집 연령 {post['age_norm']}"


# ------------------------------------------------------------------- 텔레그램

def send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("\n--- (연습 모드: 텔레그램 미설정, 보낼 내용만 출력) ---")
        print(re.sub(r"<[^>]+>", "", text))
        return

    data = urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urlopen(req, timeout=30) as resp:
            resp.read()
    except HTTPError as exc:
        print(f"! 텔레그램 전송 실패 {exc.code}: {exc.read().decode('utf-8', 'replace')}",
              file=sys.stderr)
        raise


def compose(post, who):
    esc = html.escape
    lines = [
        f"🔔 <b>[{esc(post['site'])}] 새 공고</b>",
        "",
        f"<b>{esc(post['title'])}</b>",
        "",
        f"👤 해당: <b>{esc(', '.join(who))}</b>",
        f"· 나이: {esc(post['age_norm'])}",
        f"· 성별: {esc(post['gender_norm'])}",
    ]
    if post.get("pay"):
        lines.append(f"· 피험비: {esc(post['pay'])}")
    if post.get("child_hint"):
        lines.append("· 🧒 아이 대상 공고로 보입니다")
    if post.get("detail_failed"):
        lines.append("· ⚠️ 상세 조건을 읽지 못해 일단 보냅니다. 링크에서 확인해 주세요")
    lines += ["", f'<a href="{esc(post["url"])}">👉 신청하러 가기</a>']
    return "\n".join(lines)


# --------------------------------------------------------------- 가족 정보 읽기

def parse_person_age(raw):
    """'38' → 38.0,  '18개월' → 1.5"""
    text = squash(str(raw))
    m = re.fullmatch(r"(\d{1,3})개월", text)
    if m:
        return int(m.group(1)) / 12.0
    return float(text)


def load_people(cfg):
    """PEOPLE 환경변수를 우선하고, 없으면 config.json 을 쓴다.

    저장소를 공개로 두는 경우 나이·성별이 파일에 남지 않도록 환경변수를 권한다.
    """
    raw = os.environ.get("PEOPLE", "").strip()
    people = []

    if raw:
        for chunk in raw.split(","):
            parts = [p.strip() for p in chunk.split(":")]
            if len(parts) != 3:
                print(f"! PEOPLE 형식 오류, 건너뜁니다: {chunk!r}", file=sys.stderr)
                continue
            name, gender, age = parts
            try:
                people.append({"name": name, "gender": gender,
                               "age": parse_person_age(age)})
            except ValueError:
                print(f"! 나이를 숫자로 읽지 못했습니다: {chunk!r}", file=sys.stderr)
    else:
        for entry in cfg.get("people", []):
            try:
                people.append({"name": entry["name"], "gender": entry["gender"],
                               "age": parse_person_age(entry["age"])})
            except (KeyError, ValueError):
                print(f"! config.json 항목을 읽지 못했습니다: {entry!r}", file=sys.stderr)

    # 예전 방식(MY_GENDER / MY_AGE)도 계속 동작하게 둔다
    if not people and os.environ.get("MY_GENDER") and os.environ.get("MY_AGE"):
        people.append({"name": "나",
                       "gender": os.environ["MY_GENDER"].strip(),
                       "age": parse_person_age(os.environ["MY_AGE"])})
    return people


# ------------------------------------------------------------------ 상태 저장

def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def main():
    cfg = load_json(CONFIG_PATH, {})
    people = load_people(cfg)
    if not people:
        print("! 등록된 사람이 없습니다. PEOPLE 환경변수나 config.json 을 확인하세요.",
              file=sys.stderr)
        return 1
    print("등록된 사람: " + ", ".join(
        f"{p['name']}({p['gender']}/{format_person_age(p['age'])})" for p in people))

    state = load_json(STATE_PATH, {})
    # 아래 수집 과정에서 state 가 채워지므로, 시작 메시지 여부는 미리 정해둔다
    was_empty = not any(state.get(k) for k in ("ellead", "kdri"))

    found = {}
    errors = {}
    for name, fetcher in (("ellead", fetch_ellead), ("kdri", fetch_kdri)):
        try:
            found[name] = fetcher()
            print(f"{name}: 목록 {len(found[name])}건")
        except Exception as exc:          # 한쪽이 죽어도 다른 쪽은 계속
            errors[name] = str(exc)
            print(f"! {name} 수집 실패: {exc}", file=sys.stderr)

    # 사이트가 실패한 사실을 조용히 넘기면 반쪽만 감시하는 줄 모르게 된다.
    # 다만 10분마다 같은 경고를 보내면 알림이 무의미해지므로 하루 한 번만 보낸다.
    today = time.strftime("%Y-%m-%d")
    notified = state.setdefault("error_notified", {})
    for name, message in errors.items():
        if notified.get(name) != today:
            send(f"⚠️ <b>{name} 사이트를 읽지 못했습니다</b>\n\n"
                 f"{html.escape(message[:600])}\n\n"
                 "이 사이트의 공고는 당분간 알림이 가지 않습니다.")
            notified[name] = today
    for name in found:
        notified.pop(name, None)          # 다시 되살아나면 경고 기록도 지운다

    if not found:
        STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        return 1

    sent = 0
    for name, posts in found.items():
        seen = set(state.get(name, []))
        fresh = [p for p in posts if p["id"] not in seen]

        # 첫 실행 판단은 사이트별로 한다. 한쪽만 성공한 날이 있으면, 나중에 다른
        # 쪽이 살아났을 때 기존 공고 수십 건이 한꺼번에 쏟아지기 때문이다.
        if not seen:
            print(f"{name}: 첫 실행 — 기존 {len(fresh)}건은 알림 없이 기록만 합니다")
        else:
            for post in fresh:
                if name == "kdri":
                    enrich_kdri(post)
                who, why = match(post, people)
                if who:
                    send(compose(post, who))
                    sent += 1
                    print(f"  → 알림({', '.join(who)}): [{post['site']}] {post['title']}")
                    time.sleep(1)         # 텔레그램 속도 제한 여유
                else:
                    print(f"  · 건너뜀({why}): [{post['site']}] {post['title']}")

        # 목록에 아직 보이는 글 + 방금 본 글만 남긴다
        merged = [p["id"] for p in posts] + list(state.get(name, []))
        state[name] = list(dict.fromkeys(merged))[:KEEP_IDS]

    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    if was_empty:
        send("✅ 공고 알림 봇을 시작했습니다.\n\n등록된 사람:\n"
             + "\n".join(f"· {p['name']} — {p['gender']} / "
                         f"{format_person_age(p['age'])}" for p in people)
             + "\n\n지금부터 <b>새로 올라오는</b> 공고만 보내드릴게요.")
    print(f"완료 — 보낸 알림 {sent}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
