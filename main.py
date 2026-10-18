"""Discord-Cheat 起動: にゃんこ大戦争 / ツムツム / ぷにぷに の代行を1つのBotで動かす。

    python main.py
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
# 元のBotのモジュールは import 時に環境変数を読むので、先に .env を入れる
load_dotenv(ROOT / ".env", override=True)

from hub import boot  # noqa: E402

boot.prepare_paths()

import ywp_bot  # noqa: E402  (ぷにぷにのBot本体。ここで作られる bot を3ゲーム共通で使う)

bot = ywp_bot.bot
try:
    bot.owner_id = int(os.getenv("OWNER_ID", "0") or "0") or None
except ValueError:
    bot.owner_id = None

boot.install(bot)


def main() -> None:
    token = (os.getenv("TOKEN") or os.getenv("DISCORD_TOKEN") or "").strip()
    if not token:
        raise SystemExit(f"TOKEN が未設定です。{ROOT / '.env'} に TOKEN=<Botトークン> を書いてください。")
    bot.run(token)


if __name__ == "__main__":
    main()
