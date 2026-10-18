"""初回セットアップ: .env が無ければ .env.example から作り、CHEAT_BRIDGE_SECRET を乱数で埋める。

    python scripts/setup.py

Secret は1回だけ画面に表示します。Xaccount-Bot の Bot-Factory 欄「Discord-Cheat連携 Secret」へ貼ってください。
既に値がある場合は何もしません(上書きしない)。--show で現在値を再表示します。
"""
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"
KEY = "CHEAT_BRIDGE_SECRET"


def current(text: str) -> str:
    m = re.search(rf"^{KEY}=(.*)$", text, re.M)
    return m.group(1).strip() if m else ""


def main() -> None:
    text = ENV.read_text(encoding="utf-8") if ENV.exists() else EXAMPLE.read_text(encoding="utf-8")
    value = current(text)
    if "--show" in sys.argv:
        print(value or "(未設定)")
        return
    if len(value) >= 32:
        print(f"{KEY} は設定済みです(--show で表示)")
        return
    value = secrets.token_urlsafe(48)
    if re.search(rf"^{KEY}=.*$", text, re.M):
        text = re.sub(rf"^{KEY}=.*$", f"{KEY}={value}", text, flags=re.M)
    else:
        text += f"\n{KEY}={value}\n"
    ENV.write_text(text, encoding="utf-8")
    try:
        ENV.chmod(0o600)
    except OSError:
        pass
    print(f".env を作成/更新しました: {ENV}")
    print(f"\n{KEY}={value}\n")
    print("↑ Xaccount-Bot の Bot-Factory「Discord-Cheat連携 Secret」に同じ値を貼ってください。")
    print("TOKEN / OWNER_ID は .env に自分で入れてください。")


if __name__ == "__main__":
    main()
