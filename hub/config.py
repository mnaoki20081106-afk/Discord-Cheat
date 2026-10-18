import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("CHEAT_DATA_DIR") or ROOT / "data")


@dataclass(frozen=True)
class Game:
    key: str
    label: str          # 管理画面/実績フッターに出す名前
    panel_name: str     # パネルの呼び名
    default_color: int  # 元のBotのパネル色


# 元のBotのEmbed色をそのまま既定値にする(未設定なら見た目は従来と同じ)
GAMES: dict[str, Game] = {
    "nyanko": Game("nyanko", "にゃんこ大戦争", "にゃんこ大戦争パネル", 0x2B2D31),
    "tsum": Game("tsum", "ツムツム", "ツムツムパネル", 0x06C755),
    "puni": Game("puni", "ぷにぷに", "ぷにぷにパネル", 0x5865F2),
}

DEFAULT_ACHIEVEMENT_COLOR = 5763719  # Discord-Shiire 購入完了ログと同じ緑

POLL_SECONDS = 10           # Xaccount-Botへの問い合わせ間隔
FULL_REPORT_SECONDS = 300   # 変化が無くても状態を報告し直す間隔
MAX_GUILDS_PER_REQUEST = 50


def bridge_secret() -> str:
    return (os.getenv("CHEAT_BRIDGE_SECRET") or "").strip()


def xaccount_url() -> str:
    """Xaccount-Bot Worker の HTTPS origin(例: https://xaccount-bot.<subdomain>.workers.dev)。
    推測はしない。未設定/不正なら空文字(= 連携を起動しない)。"""
    raw = (os.getenv("XACCOUNT_BOT_URL") or "").strip().rstrip("/")
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?", raw):
        return ""
    return raw
