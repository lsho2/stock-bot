#!/usr/bin/env python3
"""텔레그램 알림 전송."""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
_CHAT_IDS: list[tuple[str, str]] = [
    (label, cid)
    for label, cid in (
        ("DAD", os.getenv("TELEGRAM_CHAT_ID_DAD", "")),
        ("ME",  os.getenv("TELEGRAM_CHAT_ID_ME",  "")),
    )
    if cid
]
_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
_MAX_LEN = 4000  # Telegram 단일 메시지 한도 4096보다 여유 있게


def send_message(text: str) -> bool:
    """텔레그램으로 메시지 전송. 설정된 모든 수신자에게 전송, 4000자 초과 시 분할.

    Returns:
        True: 모든 수신자 전체 성공, False: 일부 또는 전체 실패
    """
    if not TELEGRAM_TOKEN:
        print("  [텔레그램] TELEGRAM_TOKEN 미설정 (.env 확인)")
        return False
    if not _CHAT_IDS:
        print("  [텔레그램] TELEGRAM_CHAT_ID_DAD / TELEGRAM_CHAT_ID_ME 중 하나 이상 설정 필요")
        return False

    chunks = [text[i : i + _MAX_LEN] for i in range(0, len(text), _MAX_LEN)]
    success = True
    for label, chat_id in _CHAT_IDS:
        for chunk in chunks:
            try:
                resp = requests.post(
                    f"{_API_BASE}/sendMessage",
                    json={"chat_id": chat_id, "text": chunk},
                    timeout=10,
                )
                if not resp.ok:
                    print(f"  [텔레그램:{label}] 전송 실패 (HTTP {resp.status_code}): {resp.text[:200]}")
                    success = False
            except Exception as exc:
                print(f"  [텔레그램:{label}] 오류: {exc}")
                success = False
    return success


if __name__ == "__main__":
    import sys
    msg = " ".join(sys.argv[1:]) or "텔레그램 연결 테스트"
    ok = send_message(msg)
    print("전송 성공" if ok else "전송 실패")
