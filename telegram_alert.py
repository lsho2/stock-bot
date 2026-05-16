#!/usr/bin/env python3
"""텔레그램 알림 전송.

send_message(msg)   — 본인(CHAT_ID_ME)에게만 전송. trader/monitor 등에서 사용.
send_broadcast(msg) — 본인 + 아빠(CHAT_ID_DAD, 설정 시). consensus 추천 리포트에 사용.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
_CHAT_ID_ME    = os.getenv("TELEGRAM_CHAT_ID_ME",  "")
_CHAT_ID_DAD   = os.getenv("TELEGRAM_CHAT_ID_DAD", "")
_API_BASE      = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
_MAX_LEN       = 4000  # Telegram 단일 메시지 한도 4096보다 여유 있게


def _enabled() -> bool:
    return os.getenv("TELEGRAM_ENABLED", "true").lower() != "false"


def _send_to(label: str, chat_id: str, text: str) -> bool:
    """단일 수신자에게 분할 전송. 성공 여부 반환."""
    chunks = [text[i : i + _MAX_LEN] for i in range(0, len(text), _MAX_LEN)]
    ok = True
    for chunk in chunks:
        try:
            resp = requests.post(
                f"{_API_BASE}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
                timeout=10,
            )
            if not resp.ok:
                print(f"  [텔레그램:{label}] 전송 실패 (HTTP {resp.status_code}): {resp.text[:200]}")
                ok = False
        except Exception as exc:
            print(f"  [텔레그램:{label}] 오류: {exc}")
            ok = False
    return ok


def send_message(text: str) -> bool:
    """본인(CHAT_ID_ME)에게만 전송. 4000자 초과 시 분할.

    Returns:
        True: 전송 성공, False: 실패
    """
    if not _enabled():
        print("  [텔레그램] TELEGRAM_ENABLED=false → 전송 스킵")
        return True
    if not TELEGRAM_TOKEN:
        print("  [텔레그램] TELEGRAM_TOKEN 미설정 (.env 확인)")
        return False
    if not _CHAT_ID_ME:
        print("  [텔레그램] TELEGRAM_CHAT_ID_ME 미설정 (.env 확인)")
        return False

    return _send_to("ME", _CHAT_ID_ME, text)


def send_broadcast(text: str) -> bool:
    """본인(CHAT_ID_ME) + 아빠(CHAT_ID_DAD, 설정 시) 양쪽 전송. 4000자 초과 시 분할.

    CHAT_ID_DAD 가 비어 있으면 본인에게만 전송 (오류 아님).

    Returns:
        True: 모든 수신자 전송 성공, False: 일부 이상 실패
    """
    if not _enabled():
        print("  [텔레그램] TELEGRAM_ENABLED=false → 전송 스킵")
        return True
    if not TELEGRAM_TOKEN:
        print("  [텔레그램] TELEGRAM_TOKEN 미설정 (.env 확인)")
        return False
    if not _CHAT_ID_ME:
        print("  [텔레그램] TELEGRAM_CHAT_ID_ME 미설정 (.env 확인)")
        return False

    success = _send_to("ME", _CHAT_ID_ME, text)
    if _CHAT_ID_DAD:
        success &= _send_to("DAD", _CHAT_ID_DAD, text)
    return success


if __name__ == "__main__":
    import sys
    msg = " ".join(sys.argv[1:]) or "텔레그램 연결 테스트"
    print(f"send_message: {'성공' if send_message(msg) else '실패'}")
    print(f"send_broadcast: {'성공' if send_broadcast(msg) else '실패'}")
