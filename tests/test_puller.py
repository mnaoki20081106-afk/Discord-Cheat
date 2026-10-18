import hashlib
import hmac
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from hub.puller import Puller  # noqa: E402
from hub.store import HubError, HubStore  # noqa: E402

SECRET = "s" * 40
GID = "123456789012345678"
CH = "223456789012345678"


class Perms:
    view_channel = send_messages = embed_links = True


class Channel:
    id, name, category_id, position = int(CH), "panel", None, 0

    def permissions_for(self, me):
        return Perms()


class FakeBot:
    def __init__(self):
        self.guild = SimpleNamespace(id=int(GID), name="Test", me=object(), text_channels=[Channel()])
        self.guilds = [self.guild]

    def get_guild(self, gid):
        return self.guild if gid == int(GID) else None

    async def wait_until_ready(self):
        return


class FakeWorker:
    """Xaccount-Bot側(cheat-bridge.ts)の仕様どおりに署名を検証し、決めておいた返答を返す。"""

    def __init__(self):
        self.poll_reply = {"ok": True, "configs": {}, "commands": [], "needReport": []}
        self.calls = []

    def verify(self, request, body):
        ts = request.headers["X-Cheat-Timestamp"]
        assert abs(time.time() * 1000 - int(ts)) < 60_000
        canonical = "\n".join([ts, request.method, request.raw_path, body])
        expected = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, request.headers["X-Cheat-Signature"])

    async def handle(self, request):
        body = await request.text()
        if not self.verify(request, body):
            return web.json_response({"error": "INVALID_SIGNATURE"}, status=401)
        self.calls.append((request.path, json.loads(body)))
        if request.path.endswith("/poll"):
            return web.json_response(self.poll_reply)
        return web.json_response({"ok": True, "saved": 1, "updated": True})


class PullerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = HubStore(Path(self.tmp.name) / "hub.json")
        self.worker = FakeWorker()
        app = web.Application()
        app.router.add_post("/api/cheat-bridge/{op}", self.worker.handle)
        self.server = TestServer(app)
        await self.server.start_server()
        self.puller = Puller(FakeBot(), self.store, f"http://127.0.0.1:{self.server.port}", SECRET,
                             poll_seconds=0.01)

    async def asyncTearDown(self):
        await self.puller.stop()
        await self.server.close()
        self.tmp.cleanup()

    def paths(self):
        return [p.rsplit("/", 1)[1] for p, _ in self.worker.calls]

    async def test_signed_poll_applies_config_and_reports_state_once(self):
        self.worker.poll_reply["configs"] = {GID: {"rev": 4, "config": {
            "panels": {"puni": {"channelId": CH, "color": 255}},
            "achievements": {"enabled": True, "channelId": CH, "color": 1, "showAmount": True,
                             "showUser": True, "games": {}}}}}
        await self.puller.tick()
        self.assertEqual(self.store.guild(GID)["remoteRev"], 4)
        self.assertEqual(self.store.guild(GID)["achievements"]["channelId"], CH)
        self.assertEqual(self.paths(), ["poll", "report"])
        report = self.worker.calls[1][1]["guilds"][0]
        self.assertEqual(report["id"], GID)
        self.assertEqual(report["state"]["channels"], [{"id": CH, "name": "panel", "canPost": True}])
        self.assertEqual(self.worker.calls[0][1]["revs"], {GID: 0})

        self.worker.poll_reply["configs"] = {}
        self.worker.calls.clear()
        await self.puller.tick()  # 状態が変わっていなければ報告しない
        self.assertEqual(self.paths(), ["poll"])
        self.assertEqual(self.worker.calls[0][1]["revs"], {GID: 4})

    async def test_commands_run_and_results_are_posted(self):
        self.worker.poll_reply["commands"] = [
            {"id": "cmd-deploy-1", "guildId": GID, "kind": "deploy_panel",
             "payload": {"game": "puni", "channelId": CH, "color": 255}},
            {"id": "cmd-test-0001", "guildId": GID, "kind": "test_achievement", "payload": {"game": "tsum"}},
            {"id": "cmd-bad-0001", "guildId": GID, "kind": "deploy_panel",
             "payload": {"game": "puni", "channelId": "nope"}},
        ]
        deploy = mock.AsyncMock(return_value={"channelId": CH, "messageId": "1", "updated": False})
        test = mock.AsyncMock(return_value=CH)
        with mock.patch("hub.panels.deploy_panel", deploy), mock.patch("hub.achievements.post_test", test):
            await self.puller.tick()
        deploy.assert_awaited_once()
        self.assertEqual(deploy.await_args.args[2:], (GID, "puni", CH, 255))
        test.assert_awaited_once()
        results = {c["id"]: c for p, c in self.worker.calls if p.endswith("/result")}
        self.assertTrue(results["cmd-deploy-1"]["ok"])
        self.assertTrue(results["cmd-test-0001"]["ok"])
        self.assertFalse(results["cmd-bad-0001"]["ok"])
        self.assertEqual(results["cmd-bad-0001"]["result"]["error"], "INVALID_CHANNEL")
        self.assertEqual(self.paths()[-1], "report", "設置の直後は状態を送り直す")

    async def test_hub_errors_become_failed_results(self):
        self.worker.poll_reply["commands"] = [
            {"id": "cmd-deploy-2", "guildId": GID, "kind": "deploy_panel",
             "payload": {"game": "puni", "channelId": CH}}]
        boom = mock.AsyncMock(side_effect=HubError(403, "CHANNEL_FORBIDDEN", "no perms"))
        with mock.patch("hub.panels.deploy_panel", boom):
            await self.puller.tick()
        result = next(c for p, c in self.worker.calls if p.endswith("/result"))
        self.assertEqual((result["ok"], result["result"]["error"]), (False, "CHANNEL_FORBIDDEN"))

    async def test_bad_secret_is_rejected_and_loop_survives(self):
        bad = Puller(self.puller.bot, self.store, self.puller.base_url, "x" * 40, poll_seconds=0.01)
        with self.assertRaises(RuntimeError):
            await bad.tick()
        bad.start()  # ループは例外で落ちない
        import asyncio
        await asyncio.sleep(0.05)
        self.assertFalse(bad._task.done())
        await bad.stop()


if __name__ == "__main__":
    unittest.main()
