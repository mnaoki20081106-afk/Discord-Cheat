import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hub import achievements  # noqa: E402
from hub.store import HubError, HubStore  # noqa: E402

GID = "123456789012345678"
CH = "223456789012345678"


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

    def test_remote_config_keeps_local_state_and_tracks_revision(self):
        self.store.mark_deployed(GID, "puni", channel_id=CH, message_id="999999999999999999", color=None)
        self.store.bump_counter(GID, "puni")
        remote = {"panels": {"nyanko": {"channelId": CH, "color": 255}},
                  "achievements": {"enabled": True, "channelId": CH, "color": 1, "showAmount": False,
                                   "showUser": True, "games": {"tsum": {"enabled": False, "channelId": None}}}}
        self.store.apply_remote(GID, remote, 7)
        g = self.store.guild(GID)
        self.assertEqual(g["remoteRev"], 7)
        self.assertEqual(g["panels"]["nyanko"]["color"], 255)
        self.assertFalse(g["achievements"]["games"]["tsum"]["enabled"])
        self.assertFalse(g["achievements"]["showAmount"])
        self.assertEqual(g["panels"]["puni"]["messageId"], "999999999999999999")
        self.assertEqual(g["achievements"]["counters"]["puni"], 1)
        self.assertEqual(self.store.remote_revs([GID, "123456789012345679"]), {GID: 7, "123456789012345679": 0})
        with self.assertRaises(HubError):
            self.store.apply_remote(GID, {"panels": {"puni": {"color": -5}}}, 8)
        self.assertEqual(self.store.guild(GID)["remoteRev"], 7)

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


if __name__ == "__main__":
    unittest.main()
