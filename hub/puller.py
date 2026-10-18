"""Xaccount-Bot(Cloudflare Worker)へ定期的に問い合わせに行く(プル方式)。

外部から入ってくる接続は一切受けない。ドメイン/トンネル/ポート開放が不要で、
自宅や無料ホストのように外に公開できない場所でも動く。

    POST {XACCOUNT_BOT_URL}/api/cheat-bridge/poll    設定の版・依頼(パネル設置/テスト送信)を受け取る
    POST {XACCOUNT_BOT_URL}/api/cheat-bridge/report  参加サーバー/チャンネル/設置状況を送る
    POST {XACCOUNT_BOT_URL}/api/cheat-bridge/result  依頼の結果を返す

署名は hub/signing.py(Worker側 cheat-bridge.ts と同一)。通信はHTTPSのみ。
"""
import asyncio
import hashlib
import json
import time
import traceback

import aiohttp
import discord

from . import achievements, panels
from .config import FULL_REPORT_SECONDS, GAMES, MAX_GUILDS_PER_REQUEST, POLL_SECONDS
from .signing import sign
from .store import HubError, HubStore, valid_color, valid_snowflake


def build_state(bot, store: HubStore, guild) -> dict:
    """管理画面に見せる状態。チャンネルは「このBotが投稿できるか」付き。"""
    cfg = store.guild(str(guild.id))
    me = guild.me
    channels = []
    for ch in sorted(guild.text_channels, key=lambda c: (c.category_id or 0, c.position)):
        perms = ch.permissions_for(me) if me else None
        can = bool(perms and perms.view_channel and perms.send_messages and perms.embed_links)
        channels.append({"id": str(ch.id), "name": ch.name, "canPost": can})
    return {
        "guildName": guild.name,
        "channels": channels[:500],
        "panels": {
            game: {k: cfg["panels"][game].get(k) for k in ("messageId", "deployedChannelId", "deployedAt")}
            for game in GAMES
        },
        "counters": dict(cfg["achievements"]["counters"]),
    }


class Puller:
    def __init__(self, bot, store: HubStore, base_url: str, secret: str, *,
                 poll_seconds: float = POLL_SECONDS, full_report_seconds: float = FULL_REPORT_SECONDS):
        self.bot = bot
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.poll_seconds = poll_seconds
        self.full_report_seconds = full_report_seconds
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._last_hash: dict[str, str] = {}
        self._last_full = 0.0
        self._last_log = 0.0

    # ---------------------------------------------------------- HTTP
    async def _post(self, path: str, payload: dict) -> dict:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25))
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        ts = str(int(time.time() * 1000))
        headers = {
            "Content-Type": "application/json",
            "X-Cheat-Timestamp": ts,
            "X-Cheat-Signature": sign(self.secret, ts, "POST", path, body),
        }
        async with self._session.post(self.base_url + path, data=body.encode(), headers=headers) as r:
            text = await r.text()
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status} {path}: {text[:160]}")
            return json.loads(text) if text else {}

    # ---------------------------------------------------------- 1周分
    def _guild_ids(self) -> list[str]:
        return [str(g.id) for g in self.bot.guilds][:MAX_GUILDS_PER_REQUEST]

    async def report(self, guild_ids: list[str] | None = None, *, force: bool = False) -> int:
        """状態が変わったサーバー(force なら全部)をWorkerへ送る。"""
        now = time.time()
        full = force or now - self._last_full >= self.full_report_seconds
        send = []
        for gid in guild_ids if guild_ids is not None else self._guild_ids():
            guild = self.bot.get_guild(int(gid))
            if guild is None:
                continue
            state = build_state(self.bot, self.store, guild)
            digest = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
            if full or self._last_hash.get(gid) != digest:
                send.append({"id": gid, "state": state})
                self._last_hash[gid] = digest
        if send:
            await self._post("/api/cheat-bridge/report", {"guilds": send})
        if full:
            self._last_full = now
        return len(send)

    async def run_command(self, cmd: dict) -> tuple[bool, dict]:
        kind, gid, payload = cmd.get("kind"), str(cmd.get("guildId")), cmd.get("payload") or {}
        try:
            if kind == "deploy_panel":
                if not valid_snowflake(payload.get("channelId")):
                    raise HubError(400, "INVALID_CHANNEL", "設置先チャンネルが不正です")
                color = payload.get("color")
                if color is not None and not valid_color(color):
                    raise HubError(400, "INVALID_COLOR", "色が不正です")
                result = await panels.deploy_panel(self.bot, self.store, gid, payload.get("game"),
                                                   payload.get("channelId"), payload.get("color"))
                return True, result
            if kind == "test_achievement":
                cid = await achievements.post_test(self.bot, gid, payload.get("game"))
                return True, {"channelId": cid}
            return False, {"error": "UNKNOWN_COMMAND", "message": f"未対応の依頼です: {kind}"}
        except HubError as e:
            return False, {"error": e.code, "message": e.message}
        except discord.HTTPException as e:
            return False, {"error": "DISCORD_ERROR", "message": str(e)[:160]}

    async def tick(self) -> None:
        ids = self._guild_ids()
        if not ids:
            return
        revs = self.store.remote_revs(ids)
        reply = await self._post("/api/cheat-bridge/poll", {"guilds": ids, "revs": revs})

        for gid, item in (reply.get("configs") or {}).items():
            try:
                self.store.apply_remote(gid, item["config"], int(item["rev"]))
            except (HubError, KeyError, TypeError, ValueError) as e:
                self._log(f"設定の取り込みに失敗 guild={gid}: {e}")

        touched = set(reply.get("needReport") or [])
        for cmd in reply.get("commands") or []:
            ok, result = await self.run_command(cmd)
            try:
                await self._post("/api/cheat-bridge/result", {"id": cmd.get("id"), "ok": ok, "result": result})
            except Exception as e:  # 結果の送信失敗は次回の再配信(60秒後)に任せる
                self._log(f"結果の送信に失敗 id={cmd.get('id')}: {e}")
            touched.add(str(cmd.get("guildId")))
        for gid in touched:  # 設置/テストの直後は、変化が無くても状態を送り直す
            self._last_hash.pop(gid, None)
        await self.report()

    # ---------------------------------------------------------- ループ
    def _log(self, message: str) -> None:
        now = time.time()
        if now - self._last_log >= 30:  # 障害中にログを埋め尽くさない
            self._last_log = now
            print(f"[hub] {message}")

    async def _loop(self) -> None:
        await self.bot.wait_until_ready()
        print(f"[hub] Xaccount-Bot へ {self.poll_seconds:.0f}秒ごとに問い合わせます: {self.base_url}")
        delay = self.poll_seconds
        while True:
            try:
                await self.tick()
                delay = self.poll_seconds
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log(f"問い合わせに失敗(再試行します): {type(e).__name__}: {str(e)[:160]}")
                delay = min(delay * 2, 120)
            await asyncio.sleep(delay)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop(), name="cheat-puller")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._session:
            await self._session.close()
            self._session = None
