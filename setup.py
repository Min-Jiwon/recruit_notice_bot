#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""텔레그램 연결 도우미.

봇 토큰을 입력받아 내 채팅 ID를 찾아내고, 알림 받을 사람들을 등록한 뒤,
테스트 메시지를 보내보고, GitHub Secrets 에 넣을 값을 정리해서 보여준다.

토큰은 이 컴퓨터 밖으로 나가지 않는다 (텔레그램 서버로만 전송).

사용법:  python setup.py
"""

import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://api.telegram.org/bot{token}/{method}"


def valid_age(text):
    """'38' 또는 '18개월' 형태인지 확인."""
    text = text.strip()
    if text.endswith("개월"):
        num = text[:-2].strip()
        return num.isdigit() and 0 < int(num) <= 240
    return text.isdigit() and 0 < int(text) <= 99


def read_token():
    """토큰을 입력받는다. 가능하면 화면에 보이지 않게, 안 되면 그냥 입력받는다.

    Git Bash 같은 일부 터미널에서는 감춤 입력이 멈춰버려서 대비가 필요하다.
    """
    if sys.stdin.isatty():
        try:
            from getpass import getpass
            return getpass("토큰 (화면에 보이지 않습니다): ").strip()
        except Exception:
            pass
    return input("토큰: ").strip()


def call(token, method, **params):
    url = API.format(token=token, method=method)
    data = urlencode(params).encode() if params else None
    try:
        with urlopen(Request(url, data=data), timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"ok": False, "description": f"HTTP {exc.code}: {body}"}
    except (URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "description":
                f"텔레그램 서버에 연결하지 못했습니다 ({exc}). "
                "인터넷 연결이나 회사망/VPN 차단 여부를 확인해 주세요."}


def ask_people():
    """알림 받을 사람들을 등록받는다."""
    print()
    print("-" * 58)
    print(" 알림 받을 사람을 등록합니다.")
    print(" 본인 외에 자녀도 등록하면 유아 대상 공고도 놓치지 않습니다.")
    print("-" * 58)

    people = []
    while True:
        order = "첫 번째" if not people else f"{len(people) + 1}번째"
        name = input(f"\n{order} 사람의 이름/호칭 (그만 등록하려면 그냥 Enter): ").strip()
        if not name:
            if people:
                break
            print("  최소 한 명은 등록해야 합니다.")
            continue
        if ":" in name or "," in name:
            print("  이름에 콜론(:)이나 쉼표(,)는 쓸 수 없습니다.")
            continue

        gender = ""
        while gender not in ("여성", "남성"):
            gender = input(f"  {name} 의 성별 (여성 / 남성): ").strip()

        age = ""
        while not valid_age(age):
            age = input(f"  {name} 의 나이 (예: 38, 돌 전이면 '18개월'): ").strip()

        people.append((name, gender, age))
        print(f"  ✓ {name} — {gender} / {age}")

    return people


def main():
    print("=" * 58)
    print(" 텔레그램 알림 연결 도우미")
    print("=" * 58)
    print()
    print("BotFather 에게 받은 토큰을 붙여넣고 Enter 를 누르세요.")
    print()

    token = read_token()
    if not token:
        print("\n토큰이 비어 있습니다. 다시 실행해 주세요.")
        return 1
    # "bot" 접두사까지 같이 복사한 경우를 정리
    if token.lower().startswith("bot"):
        token = token[3:]

    # 1) 토큰이 유효한지 확인
    me = call(token, "getMe")
    if not me.get("ok"):
        print(f"\n✗ 토큰이 올바르지 않습니다: {me.get('description')}")
        print("  BotFather 대화에서 토큰을 다시 복사해 보세요.")
        return 1
    bot_name = me["result"].get("username", "?")
    print(f"\n✓ 봇 확인됨: @{bot_name}")

    # 2) 채팅 ID 찾기
    print(f"\n지금 텔레그램에서 @{bot_name} 을 열고 아무 메시지나 하나 보내주세요.")
    print("(예: 안녕)  보내셨으면 Enter 를 누르세요.")
    input()

    updates = call(token, "getUpdates", offset=-1)
    if not updates.get("ok"):
        print(f"\n✗ 조회 실패: {updates.get('description')}")
        return 1

    chats = {}
    for item in updates.get("result", []):
        msg = item.get("message") or item.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            label = (chat.get("first_name") or chat.get("title")
                     or chat.get("username") or "")
            chats[chat["id"]] = label

    if not chats:
        print("\n✗ 아직 메시지가 보이지 않습니다.")
        print(f"  @{bot_name} 대화창에서 '시작/START' 를 누르고 메시지를 보낸 뒤")
        print("  이 스크립트를 다시 실행해 주세요.")
        return 1

    if len(chats) == 1:
        chat_id = next(iter(chats))
    else:
        print("\n대화가 여러 개 있습니다. 알림을 받을 곳을 고르세요:")
        options = list(chats.items())
        for idx, (cid, label) in enumerate(options, 1):
            print(f"  {idx}. {label} (id: {cid})")
        pick = input("번호: ").strip()
        if not pick.isdigit() or not 1 <= int(pick) <= len(options):
            print("잘못 선택했습니다.")
            return 1
        chat_id = options[int(pick) - 1][0]

    print(f"✓ 채팅 ID: {chat_id}")

    # 3) 알림 받을 사람들
    people = ask_people()
    people_value = ", ".join(f"{n}:{g}:{a}" for n, g, a in people)
    summary = "\n".join(f"· {n} — {g} / {a}" for n, g, a in people)

    # 4) 테스트 발송
    sent = call(token, "sendMessage",
                chat_id=str(chat_id),
                parse_mode="HTML",
                text=("🔔 <b>연결 테스트 성공!</b>\n\n"
                      f"등록된 사람:\n{summary}\n\n"
                      "이제 새 공고가 올라오면 여기로 알려드릴게요."))
    if not sent.get("ok"):
        print(f"\n✗ 테스트 메시지 발송 실패: {sent.get('description')}")
        return 1
    print("\n✓ 텔레그램으로 테스트 메시지를 보냈습니다. 확인해 보세요!")

    # 5) 등록할 값 안내
    print()
    print("=" * 58)
    print(" GitHub Secrets 에 아래 3개를 등록하세요")
    print(" (저장소 → Settings → Secrets and variables → Actions)")
    print("=" * 58)
    print("  TELEGRAM_BOT_TOKEN : (방금 입력한 토큰 그대로)")
    print(f"  TELEGRAM_CHAT_ID   : {chat_id}")
    print(f"  PEOPLE             : {people_value}")
    print("=" * 58)
    print()
    print("PEOPLE 값은 그대로 복사해서 붙여넣으세요.")
    print("토큰은 화면에 다시 표시하지 않습니다. BotFather 대화에서 언제든 볼 수 있어요.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n취소했습니다.")
        sys.exit(1)
