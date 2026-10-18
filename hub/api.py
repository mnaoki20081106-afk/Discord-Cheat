"""Xaccount-Bot の管理画面から呼ばれる Bridge API。

認証は Xaccount-Bot の X-Utility / Discord-Shiire ブリッジと同じ方式:
    X-Cheat-Timestamp : 送信時刻(ミリ秒)
    X-Cheat-Nonce     : 1回限りの乱数
    X-Cheat-Signature : HMAC-SHA256(secret, timestamp \\n nonce \\n METHOD \\n path+query \\n body) の16進
Secret は CHEAT_BRIDGE_SECRET(Xaccount-Bot側と同じ32文字以上)。未設定ならAPIは起動しない。
"""
import hashlib
import hmac
import json
import time

import discord
from aiohttp import web

from . import VERSION, achievements, panels
from .config import BRIDGE_MAX_SKEW_MS, BRIDGE_NONCE_TTL_S, BRIDGE_PREFIX, GAMES
from .store import HubError, HubStore, valid_snowflake


def canonical(timestamp: str, nonce: str, method: str, target: str, body: str) -> str:
    return "\n".join([timestamp, nonce, method.upper(), target, body])


def sign(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def _json(status: int, payload: dict) -> web.Response:
    return web.json_response(payload, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


class BridgeServer:
    def __init__(self, bot, store: HubStore, secret: str, host: str, port: int):
        self.bot = bot
        self.store = store
        self.secret = secret
        self.host = host
        self.port = port
        self._nonces: dict[str, float] = {}
        self._runner: web.AppRunner | None = None

    # ---------------------------------------------------------- 認証
    def _check_nonce(self, nonce: str) -> bool:
        now = time.time()
        for k in [k for k, exp in self._nonces.items() if exp < now]:
            del self._nonces[k]
        if nonce in self._nonces:
            return False
        self._nonces[nonce] = now + BRIDGE_NONCE_TTL_S
        return True

    @web.middleware
    async def _auth(self, request: web.Request, handler):
        try:
            ts = request.headers.get("X-Cheat-Timestamp", "")
            nonce = request.headers.get("X-Cheat-Nonce", "")
            sig = request.headers.get("X-Cheat-Signature", "")
            if not (ts and nonce and sig) or len(nonce) > 128:
                raise HubError(401, "BRIDGE_AUTH_REQUIRED")
            try:
                skew = abs(int(time.time() * 1000) - int(ts))
            except ValueError:
                raise HubError(401, "BRIDGE_TIMESTAMP_INVALID")
            if skew > BRIDGE_MAX_SKEW_MS:
                raise HubError(401, "BRIDGE_TIMESTAMP_EXPIRED")
            body = (await request.read()).decode("utf-8", "replace")
            expected = sign(self.secret, canonical(ts, nonce, request.method, request.raw_path, body))
            if not hmac.compare_digest(expected, sig):
                raise HubError(401, "BRIDGE_SIGNATURE_INVALID")
            # 署名が正しいものだけnonceを消費する(未認証リクエストでnonce表を膨らませない)
            if not self._check_nonce(nonce):
                raise HubError(401, "BRIDGE_REPLAY")
            return await handler(request)
        except HubError as e:
            return _json(e.status, {"error": e.code, "message": e.message})
        except discord.HTTPException as e:
            return _json(502, {"error": "DISCORD_ERROR", "message": str(e)[:160]})

    # ---------------------------------------------------------- helpers
    def _need_ready(self) -> None:
        if not self.bot.is_ready():
            raise HubError(503, "BOT_NOT_READY", "Discord-Cheat BOTが起動中です")

    def _guild(self, request: web.Request):
        gid = request.match_info["gid"]
        if not valid_snowflake(gid):
            raise HubError(400, "INVALID_GUILD")
        self._need_ready()
        guild = self.bot.get_guild(int(gid))
        if guild is None:
            raise HubError(404, "GUILD_NOT_FOUND", "Discord-Cheat BOTがこのサーバーに参加していません")
        return gid, guild

    async def _body(self, request: web.Request) -> dict:
        # 認証ミドルウェアが先に read() 済み(can_read_body は読了後 False になるので使わない)
        raw = await request.read()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise HubError(400, "INVALID_JSON")
        if not isinstance(data, dict):
            raise HubError(400, "INVALID_BODY", "JSON objectが必要です")
        return data

    def _config_payload(self, gid: str, guild) -> dict:
        cfg = self.store.guild(gid)
        me = guild.me
        channels = []
        for ch in sorted(guild.text_channels, key=lambda c: (c.category_id or 0, c.position)):
            perms = ch.permissions_for(me) if me else None
            can = bool(perms and perms.view_channel and perms.send_messages and perms.embed_links)
            channels.append({"id": str(ch.id), "name": ch.name, "canPost": can})
        return {
            "guildId": gid,
            "guildName": guild.name,
            "games": [
                {"key": g.key, "label": g.label, "panelName": g.panel_name, "defaultColor": g.default_color}
                for g in GAMES.values()
            ],
            "panels": cfg["panels"],
            "achievements": cfg["achievements"],
            "channels": channels,
        }

    # ---------------------------------------------------------- handlers
    async def status(self, request: web.Request) -> web.Response:
        ready = self.bot.is_ready()
        return _json(200, {
            "ok": True,
            "version": VERSION,
            "ready": ready,
            "user": str(self.bot.user) if ready and self.bot.user else None,
            "guilds": [{"id": str(g.id), "name": g.name} for g in self.bot.guilds] if ready else [],
        })

    async def get_config(self, request: web.Request) -> web.Response:
        gid, guild = self._guild(request)
        return _json(200, self._config_payload(gid, guild))

    async def put_config(self, request: web.Request) -> web.Response:
        gid, guild = self._guild(request)
        self.store.patch_config(gid, await self._body(request))
        return _json(200, self._config_payload(gid, guild))

    async def deploy_panel(self, request: web.Request) -> web.Response:
        gid, _guild = self._guild(request)
        game = request.match_info["game"]
        if game not in GAMES:
            raise HubError(404, "UNKNOWN_GAME")
        body = await self._body(request)
        channel_id = body.get("channelId")
        if not valid_snowflake(channel_id):
            raise HubError(400, "INVALID_CHANNEL", "設置先チャンネルを選択してください")
        color = body.get("color", self.store.guild(gid)["panels"][game]["color"])
        from .store import valid_color
        if color is not None and not valid_color(color):
            raise HubError(400, "INVALID_COLOR", "色は0〜16777215の整数です")
        result = await panels.deploy_panel(self.bot, self.store, gid, game, channel_id, color)
        return _json(200, {"ok": True, "game": game, **result})

    async def test_achievement(self, request: web.Request) -> web.Response:
        gid, _guild = self._guild(request)
        body = await self._body(request)
        cid = await achievements.post_test(self.bot, gid, body.get("game"))
        return _json(200, {"ok": True, "channelId": cid})

    # ---------------------------------------------------------- 起動/停止
    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth], client_max_size=64 * 1024)
        p = BRIDGE_PREFIX
        app.router.add_get(p + "/status", self.status)
        app.router.add_get(p + "/guilds/{gid}/config", self.get_config)
        app.router.add_put(p + "/guilds/{gid}/config", self.put_config)
        app.router.add_post(p + "/guilds/{gid}/panels/{game}", self.deploy_panel)
        app.router.add_post(p + "/guilds/{gid}/achievements/test", self.test_achievement)
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self.build_app(), access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        print(f"[hub] Bridge API: http://{self.host}:{self.port}{BRIDGE_PREFIX}")

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
