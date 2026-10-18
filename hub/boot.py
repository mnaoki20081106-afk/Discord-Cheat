"""3ゲームを1つのBotに載せる配線。main.py から呼ばれる。"""
import os
import sys
from pathlib import Path

from . import achievements, panels
from .api import BridgeServer
from .config import DATA_DIR, ROOT, bridge_host, bridge_port, bridge_secret
from .store import HubStore

GAME_DIRS = ("nyanko", "tsum", "puni")


def prepare_paths() -> Path:
    """各ゲームの元コードは `import mod_stum` / `from Cogs.utils import ...` のような
    フラットimportなので、ゲームごとのフォルダをsys.pathへ足す。
    にゃんこは相対パスでJSONを読み書きするため、作業ディレクトリを data/nyanko にする。"""
    for name in GAME_DIRS:
        p = str(ROOT / "games" / name)
        if p not in sys.path:
            sys.path.insert(0, p)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    nyanko_dir = DATA_DIR / "nyanko"
    nyanko_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(nyanko_dir)
    return nyanko_dir


def install(bot) -> HubStore:
    """puni の YWPBot に、ハブ側の初期化をフックとして差し込む。"""
    import ywp_bot

    store = HubStore()
    achievements.install(store)
    server = None
    secret = bridge_secret()

    async def on_setup(b):
        nonlocal server
        # にゃんこのCog(元の Cogs/*.py)をそのまま読む
        for f in sorted((ROOT / "games" / "nyanko" / "Cogs").glob("*.py")):
            if f.name == "__init__.py":
                continue
            try:
                await b.load_extension(f"Cogs.{f.stem}")
                print(f"[hub] nyanko: {f.name}")
            except Exception as e:
                print(f"[hub] nyanko: {f.name} の読み込みに失敗: {type(e).__name__}: {e}")
        await b.load_extension("hub.tsum_cog")
        panels.register_views(b)

        guild_id = int(os.getenv("SERVER_ID", "0") or "0")
        if guild_id:
            import discord
            g = discord.Object(id=guild_id)
            b.tree.copy_global_to(guild=g)
            synced = await b.tree.sync(guild=g)
        else:
            synced = await b.tree.sync()
        print(f"[hub] コマンド同期: {len(synced)}件 ({'guild ' + str(guild_id) if guild_id else 'global'})")

        if len(secret) >= 32:
            server = BridgeServer(b, store, secret, bridge_host(), bridge_port())
            await server.start()
        else:
            print("[hub] CHEAT_BRIDGE_SECRET が未設定(32文字未満)のため、管理画面連携APIは起動しません")

    ywp_bot.SKIP_DEFAULT_SYNC = True
    ywp_bot.SETUP_HOOKS.append(on_setup)
    return store
