import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from hub import achievements  # noqa: E402
from hub.api import BridgeServer, canonical, sign  # noqa: E402
from hub.store import HubError, HubStore  # noqa: E402

SECRET = "t" * 40
GID = "123456789012345678"
CH = "223456789012345678"


class FakePerms:
    view_channel = send_messages = embed_links = True


class FakeChannel:
    def __init__(self, cid, name):
        self.id, self.name, self.category_id, self.position = cid, name, None, 0

    def permissions_for(self, me):
        return FakePerms()


class FakeBot:
    def __init__(self, ready=True):
        self._ready = ready
        self.user = "cheat#0001"
        guild = SimpleNamespace(id=int(GID), name="g", me=object(), text_channels=[FakeChannel(int(CH), "代行")])
        self.guild = guild
        self.guilds = [guild]

    def is_ready(self):
        return self._ready

    def get_guild(self, gid):
        return self.guild if gid == int(GID) else None


def signed(method, path, body="", secret=SECRET, ts=None, nonce=None):
    ts = ts or str(int(time.time() * 1000))
    nonce = nonce or os.urandom(8).hex()
    return {
        "X-Cheat-Timestamp": ts,
        "X-Cheat-Nonce": nonce,
        "X-Cheat-Signature": sign(secret, canonical(ts, nonce, method, path, body)),
    }


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = HubStore(Path(self.tmp.name) / "hub.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_and_roundtrip(self):
        g = self.store.guild(GID)
        self.assertIsNone(g["panels"]["puni"]["channelId"])
        self.store.patch_config(GID, {"panels": {"puni": {"channelId": CH, "color": 0xABCDEF}}})
        again = HubStore(Path(self.tmp.name) / "hub.json").guild(GID)
        self.assertEqual(again["panels"]["puni"]["channelId"], CH)
        self.assertEqual(again["panels"]["puni"]["color"], 0xABCDEF)

    def test_rejects_bad_input_without_partial_write(self):
        with self.assertRaises(HubError) as e:
            self.store.patch_config(GID, {"panels": {"tsum": {"channelId": CH}, "puni": {"color": -1}}})
        self.assertEqual(e.exception.code, "INVALID_COLOR")
        self.assertIsNone(self.store.guild(GID)["panels"]["tsum"]["channelId"])
        for bad in ({"achievements": {"channelId": "abc"}}, {"achievements": {"enabled": "yes"}},
                    {"achievements": {"color": True}}):
            with self.assertRaises(HubError):
                self.store.patch_config(GID, bad)

    def test_counter_never_goes_backwards(self):
        self.assertEqual(self.store.bump_counter(GID, "tsum"), 1)
        self.assertEqual(self.store.bump_counter(GID, "tsum", at_least=5), 5)
        self.assertEqual(self.store.bump_counter(GID, "tsum"), 6)


class AchievementTests(unittest.TestCase):
    def test_channel_resolution(self):
        from hub.store import default_guild
        a = default_guild()["achievements"]
        self.assertIsNone(achievements.resolve_channel_id(a, "puni"))
        a["channelId"] = CH
        self.assertEqual(achievements.resolve_channel_id(a, "puni"), CH)
        a["games"]["puni"]["channelId"] = "323456789012345678"
        self.assertEqual(achievements.resolve_channel_id(a, "puni"), "323456789012345678")
        a["games"]["tsum"]["enabled"] = False
        self.assertIsNone(achievements.resolve_channel_id(a, "tsum"))
        a["enabled"] = False
        self.assertIsNone(achievements.resolve_channel_id(a, "nyanko"))

    def test_embed_respects_display_flags(self):
        from hub.store import default_guild
        a = default_guild()["achievements"]
        user = SimpleNamespace(mention="<@1>")
        e = achievements.build_embed(a, "nyanko", user=user, menu="m", amount=1000, number=3)
        names = [f.name for f in e.fields]
        self.assertIn("ご成約金額", names)
        self.assertIn("<@1>", e.description)
        self.assertIn("No.3", e.footer.text)
        a["showAmount"] = a["showUser"] = False
        e = achievements.build_embed(a, "nyanko", user=user, menu="m", amount=1000, number=None)
        self.assertNotIn("ご成約金額", [f.name for f in e.fields])
        self.assertNotIn("<@1>", e.description)
        self.assertEqual(e.colour.value, a["color"])


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = HubStore(Path(self.tmp.name) / "hub.json")
        self.bot = FakeBot()
        self.server = BridgeServer(self.bot, self.store, SECRET, "127.0.0.1", 0)
        self.client = TestClient(TestServer(self.server.build_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def call(self, method, path, body=None, **kw):
        raw = "" if body is None else json.dumps(body)
        headers = signed(method, path, raw, **kw)
        if raw:
            headers["Content-Type"] = "application/json"
        return await self.client.request(method, path, data=raw or None, headers=headers)

    async def test_requires_valid_signature(self):
        r = await self.client.get("/bridge/main/status")
        self.assertEqual(r.status, 401)
        r = await self.call("GET", "/bridge/main/status", secret="x" * 40)
        self.assertEqual((await r.json())["error"], "BRIDGE_SIGNATURE_INVALID")
        r = await self.call("GET", "/bridge/main/status", ts=str(int(time.time() * 1000) - 600_000))
        self.assertEqual((await r.json())["error"], "BRIDGE_TIMESTAMP_EXPIRED")
        r = await self.call("GET", "/bridge/main/status")
        self.assertEqual(r.status, 200)

    async def test_replay_is_rejected(self):
        path = f"/bridge/main/guilds/{GID}/config"
        headers = signed("GET", path, nonce="fixednonce")
        self.assertEqual((await self.client.get(path, headers=headers)).status, 200)
        r = await self.client.get(path, headers=headers)
        self.assertEqual((await r.json())["error"], "BRIDGE_REPLAY")

    async def test_config_roundtrip_and_validation(self):
        path = f"/bridge/main/guilds/{GID}/config"
        r = await self.call("PUT", path, {"panels": {"nyanko": {"channelId": CH, "color": 0x112233}},
                                          "achievements": {"channelId": CH, "showAmount": False}})
        data = await r.json()
        self.assertEqual(r.status, 200)
        self.assertEqual(data["panels"]["nyanko"]["color"], 0x112233)
        self.assertFalse(data["achievements"]["showAmount"])
        self.assertEqual(data["channels"], [{"id": CH, "name": "代行", "canPost": True}])
        r = await self.call("PUT", path, {"panels": {"nyanko": {"color": 99999999}}})
        self.assertEqual((await r.json())["error"], "INVALID_COLOR")
        r = await self.call("GET", "/bridge/main/guilds/999999999999999999/config")
        self.assertEqual((await r.json())["error"], "GUILD_NOT_FOUND")

    async def test_deploy_uses_saved_color_and_validates_channel(self):
        path = f"/bridge/main/guilds/{GID}/panels/puni"
        r = await self.call("POST", path, {"channelId": "bad"})
        self.assertEqual((await r.json())["error"], "INVALID_CHANNEL")
        seen = {}

        async def fake_deploy(bot, store, gid, game, cid, color):
            seen.update(gid=gid, game=game, cid=cid, color=color)
            return {"channelId": cid, "messageId": "1", "updated": False}

        with mock.patch("hub.panels.deploy_panel", fake_deploy):
            r = await self.call("POST", path, {"channelId": CH, "color": 0x0000FF})
            self.assertEqual(r.status, 200)
        self.assertEqual(seen, dict(gid=GID, game="puni", cid=CH, color=0x0000FF))
        r = await self.call("POST", f"/bridge/main/guilds/{GID}/panels/unknown", {"channelId": CH})
        self.assertEqual((await r.json())["error"], "UNKNOWN_GAME")

    async def test_not_ready(self):
        self.bot._ready = False
        r = await self.call("GET", f"/bridge/main/guilds/{GID}/config")
        self.assertEqual(r.status, 503)


if __name__ == "__main__":
    unittest.main()
