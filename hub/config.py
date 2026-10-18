import os
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

BRIDGE_PREFIX = "/bridge/main"
BRIDGE_MAX_SKEW_MS = 5 * 60 * 1000
BRIDGE_NONCE_TTL_S = 10 * 60


def bridge_secret() -> str:
    return (os.getenv("CHEAT_BRIDGE_SECRET") or "").strip()


def bridge_host() -> str:
    return (os.getenv("CHEAT_API_HOST") or "127.0.0.1").strip()


def bridge_port() -> int:
    try:
        return int(os.getenv("CHEAT_API_PORT") or "8790")
    except ValueError:
        return 8790
