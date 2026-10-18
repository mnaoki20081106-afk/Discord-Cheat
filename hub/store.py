"""サーバーごとのパネル/実績設定を data/hub.json に保存する。

書き込みは tmp → os.replace の原子的置換。ロックはプロセス内のみ(1プロセス運用前提)。
"""
import copy
import json
import os
import re
import threading
import time
from pathlib import Path

from .config import DATA_DIR, DEFAULT_ACHIEVEMENT_COLOR, GAMES

SNOWFLAKE = re.compile(r"^\d{15,25}$")


class HubError(Exception):
    """APIにそのまま返せるエラー。code は Worker 側が分岐に使う。"""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code


def valid_snowflake(value) -> bool:
    return isinstance(value, str) and bool(SNOWFLAKE.match(value))


def valid_color(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0xFFFFFF


def _default_panel() -> dict:
    return {
        "channelId": None,          # 設置先(管理画面で選んだ値)
        "color": None,              # None = ゲーム既定色
        "messageId": None,          # 設置済みメッセージ
        "deployedChannelId": None,  # messageId があるチャンネル
        "deployedAt": None,
    }


def _default_game_ach() -> dict:
    return {"enabled": True, "channelId": None}  # channelId None = 共通チャンネル


def default_guild() -> dict:
    return {
        "remoteRev": 0,             # Workerから最後に取り込んだ設定の版
        "panels": {key: _default_panel() for key in GAMES},
        "achievements": {
            "enabled": True,
            "channelId": None,
            "color": DEFAULT_ACHIEVEMENT_COLOR,
            "showAmount": True,
            "showUser": True,
            "games": {key: _default_game_ach() for key in GAMES},
            "counters": {key: 0 for key in GAMES},
        },
    }


def _merge(base: dict, saved: dict) -> dict:
    """保存値を既定値の上に重ねる(キーが増えた更新後も古いファイルが読める)。"""
    out = copy.deepcopy(base)
    for key, value in (saved or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value)
        elif key in out:
            out[key] = value
    return out


def _apply_patch(cur: dict, patch: dict) -> None:
    """管理画面/Workerからの部分更新を検証して cur に適用する。不正なら HubError(途中まで書かない)。"""
    if not isinstance(patch, dict):
        raise HubError(400, "INVALID_BODY", "JSON objectが必要です")
    panels = patch.get("panels")
    if panels is not None:
        if not isinstance(panels, dict):
            raise HubError(400, "INVALID_PANELS")
        for game, p in panels.items():
            if game not in GAMES:
                continue
            if not isinstance(p, dict):
                raise HubError(400, "INVALID_PANELS")
            if "channelId" in p:
                cid = p["channelId"]
                if cid is not None and not valid_snowflake(cid):
                    raise HubError(400, "INVALID_CHANNEL", "チャンネルIDの形式が不正です")
                cur["panels"][game]["channelId"] = cid
            if "color" in p:
                col = p["color"]
                if col is not None and not valid_color(col):
                    raise HubError(400, "INVALID_COLOR", "色は0〜16777215の整数です")
                cur["panels"][game]["color"] = col

    ach = patch.get("achievements")
    if ach is not None:
        if not isinstance(ach, dict):
            raise HubError(400, "INVALID_ACHIEVEMENTS")
        a = cur["achievements"]
        for flag in ("enabled", "showAmount", "showUser"):
            if flag in ach:
                if not isinstance(ach[flag], bool):
                    raise HubError(400, "INVALID_ACHIEVEMENTS", f"{flag}はtrue/falseです")
                a[flag] = ach[flag]
        if "channelId" in ach:
            cid = ach["channelId"]
            if cid is not None and not valid_snowflake(cid):
                raise HubError(400, "INVALID_CHANNEL", "実績チャンネルIDの形式が不正です")
            a["channelId"] = cid
        if "color" in ach:
            if not valid_color(ach["color"]):
                raise HubError(400, "INVALID_COLOR", "色は0〜16777215の整数です")
            a["color"] = ach["color"]
        games = ach.get("games")
        if games is not None:
            if not isinstance(games, dict):
                raise HubError(400, "INVALID_ACHIEVEMENTS")
            for game, g in games.items():
                if game not in GAMES:
                    continue
                if not isinstance(g, dict):
                    raise HubError(400, "INVALID_ACHIEVEMENTS")
                if "enabled" in g:
                    if not isinstance(g["enabled"], bool):
                        raise HubError(400, "INVALID_ACHIEVEMENTS")
                    a["games"][game]["enabled"] = g["enabled"]
                if "channelId" in g:
                    cid = g["channelId"]
                    if cid is not None and not valid_snowflake(cid):
                        raise HubError(400, "INVALID_CHANNEL")
                    a["games"][game]["channelId"] = cid


class HubStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else DATA_DIR / "hub.json"
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("guilds"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"version": 1, "guilds": {}}

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ---- 読み取り ----
    def guild(self, guild_id: str) -> dict:
        with self._lock:
            saved = self._data["guilds"].get(str(guild_id), {})
            return _merge(default_guild(), saved)

    def guild_ids(self) -> list[str]:
        with self._lock:
            return list(self._data["guilds"])

    # ---- 書き込み ----
    def _update(self, guild_id: str, fn) -> dict:
        with self._lock:
            cur = _merge(default_guild(), self._data["guilds"].get(str(guild_id), {}))
            fn(cur)
            self._data["guilds"][str(guild_id)] = cur
            self._flush()
            return copy.deepcopy(cur)

    def patch_config(self, guild_id: str, patch: dict) -> dict:
        return self._update(guild_id, lambda cur: _apply_patch(cur, patch))

    def apply_remote(self, guild_id: str, config: dict, rev: int) -> dict:
        """Xaccount-Bot(Worker)に保存された設定を取り込む。設置済みメッセージや実績件数は触らない。"""
        if not isinstance(config, dict) or isinstance(rev, bool) or not isinstance(rev, int):
            raise HubError(400, "INVALID_BODY")
        patch = {
            "panels": {
                game: {"channelId": (config.get("panels", {}).get(game) or {}).get("channelId"),
                       "color": (config.get("panels", {}).get(game) or {}).get("color")}
                for game in GAMES
            },
            "achievements": {k: v for k, v in (config.get("achievements") or {}).items() if k != "counters"},
        }

        def apply(cur: dict) -> None:
            _apply_patch(cur, patch)
            cur["remoteRev"] = rev

        return self._update(guild_id, apply)

    def remote_revs(self, guild_ids: list[str]) -> dict[str, int]:
        return {gid: int(self.guild(gid).get("remoteRev") or 0) for gid in guild_ids}

    def mark_deployed(self, guild_id: str, game: str, *, channel_id: str, message_id: str,
                      color: int | None) -> dict:
        def apply(cur: dict) -> None:
            p = cur["panels"][game]
            p["channelId"] = channel_id
            p["color"] = color
            p["messageId"] = message_id
            p["deployedChannelId"] = channel_id
            p["deployedAt"] = int(time.time() * 1000)

        return self._update(guild_id, apply)

    def bump_counter(self, guild_id: str, game: str, at_least: int = 0) -> int:
        box = {}

        def apply(cur: dict) -> None:
            c = cur["achievements"]["counters"]
            c[game] = max(int(c.get(game) or 0) + 1, at_least)
            box["n"] = c[game]

        self._update(guild_id, apply)
        return box["n"]
