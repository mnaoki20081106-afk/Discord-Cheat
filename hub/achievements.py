"""完了実績の投稿。

Discord-Shiire の購入完了ログ(allowed_mentions なし・緑Embed・商品/金額/購入者フィールド)と同じ方式で、
3ゲームの完了を管理画面で選んだ1つのチャンネルへ流す。投稿先・色・表示項目は data/hub.json。
"""
import traceback
from datetime import datetime, timezone

import discord

from .config import GAMES
from .store import HubStore, valid_snowflake

_store: HubStore | None = None


def install(store: HubStore) -> None:
    global _store
    _store = store


def resolve_channel_id(ach: dict, game: str) -> str | None:
    """実績を出すか + 出すならどのチャンネルか。出さないなら None。"""
    if not ach.get("enabled"):
        return None
    g = ach["games"].get(game) or {}
    if not g.get("enabled"):
        return None
    cid = g.get("channelId") or ach.get("channelId")
    return cid if valid_snowflake(cid) else None


def build_embed(ach: dict, game: str, *, user, menu: str, amount: int, number: int | None,
                extra_fields: list[tuple[str, str]] | None = None, test: bool = False) -> discord.Embed:
    info = GAMES[game]
    paid = amount > 0
    who = user.mention if (ach.get("showUser") and user is not None) else "匿名のお客様"
    e = discord.Embed(
        title="✅ 完了実績" + ("（テスト）" if test else ""),
        description=f"{who} 様のご依頼を完了しました。",
        color=int(ach.get("color") or 0),
        timestamp=datetime.now(timezone.utc),
    )
    e.add_field(name="ゲーム", value=info.label, inline=True)
    if ach.get("showAmount"):
        e.add_field(name="ご成約金額", value=f"¥{amount:,}" if paid else "無料", inline=True)
    e.add_field(name="ご依頼内容", value=(menu or "不明")[:1024], inline=False)
    for name, value in extra_fields or []:
        if value:
            e.add_field(name=name[:256], value=str(value)[:1024], inline=False)
    e.add_field(name="​", value="> ご利用ありがとうございました。またのご依頼をお待ちしております。", inline=False)
    e.set_footer(text=(f"No.{number} ・ " if number else "") + f"{info.label}代行")
    return e


async def _channel(bot, guild, channel_id: str):
    cid = int(channel_id)
    ch = guild.get_channel(cid) if guild else None
    if ch is None:
        ch = bot.get_channel(cid) or await bot.fetch_channel(cid)
    return ch


def _guild_of(bot, user, guild):
    if guild is not None:
        return guild
    g = getattr(user, "guild", None)
    if g is not None:
        return g
    # DM起点など。実績設定のあるサーバーが1つだけならそこへ。
    if _store is not None:
        ids = [gid for gid in _store.guild_ids() if bot.get_guild(int(gid))]
        if len(ids) == 1:
            return bot.get_guild(int(ids[0]))
    return None


async def post_achievement(bot, *, game: str, user, menu: str, amount: int = 0, guild=None,
                           extra_fields: list[tuple[str, str]] | None = None) -> bool:
    """実績を1件投稿する。設定が無い/投稿できない場合は False(ゲーム側の処理は止めない)。"""
    if _store is None or game not in GAMES:
        return False
    try:
        guild = _guild_of(bot, user, guild)
        if guild is None:
            return False
        ach = _store.guild(str(guild.id))["achievements"]
        cid = resolve_channel_id(ach, game)
        if cid is None:
            return False
        ch = await _channel(bot, guild, cid)
        number = int(ach["counters"].get(game) or 0) + 1
        embed = build_embed(ach, game, user=user, menu=menu, amount=max(0, int(amount or 0)),
                            number=number, extra_fields=extra_fields)
        await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        # 投稿に失敗したときは番号を飛ばさない(成功後に確定)
        _store.bump_counter(str(guild.id), game, at_least=number)
        return True
    except (discord.HTTPException, ValueError) as e:
        print(f"[実績] {game} 投稿に失敗: {str(e)[:160]}")
    except Exception:
        print(f"[実績] {game} 投稿でエラー\n{traceback.format_exc()}")
    return False


async def post_test(bot, guild_id: str, game: str | None):
    """管理画面のテスト送信。カウンターは進めない。送れたチャンネルIDを返す。"""
    from .store import HubError

    if _store is None:
        raise HubError(503, "HUB_NOT_READY")
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        raise HubError(404, "GUILD_NOT_FOUND", "Discord-Cheat BOTがこのサーバーに参加していません")
    ach = _store.guild(guild_id)["achievements"]
    game = game if game in GAMES else "puni"
    if not ach.get("enabled"):
        raise HubError(409, "ACHIEVEMENTS_DISABLED", "実績投稿がOFFです")
    cid = resolve_channel_id(ach, game)
    if cid is None:
        raise HubError(409, "ACHIEVEMENT_CHANNEL_NOT_SET", "実績の投稿先チャンネルが未設定です")
    try:
        ch = await _channel(bot, guild, cid)
        me = guild.me
        perms = ch.permissions_for(me) if me else None
        if perms is None or not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise HubError(403, "CHANNEL_FORBIDDEN", "Botに投稿権限がありません")
        embed = build_embed(ach, game, user=None, menu="テスト送信(実績の見た目確認)", amount=0,
                            number=None, test=True)
        await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.NotFound:
        raise HubError(404, "CHANNEL_NOT_FOUND", "実績チャンネルが見つかりません")
    except discord.Forbidden:
        raise HubError(403, "CHANNEL_FORBIDDEN", "Botに投稿権限がありません")
    except discord.HTTPException as e:
        raise HubError(502, "DISCORD_ERROR", str(e)[:160])
    return cid
