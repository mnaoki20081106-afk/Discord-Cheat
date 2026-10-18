"""3ゲームのパネル(Embed + 永続View)と、管理画面からの設置・更新。

パネルは元のBotの見た目をそのまま使い、色だけを設定値で差し替える。
同じチャンネルに設置済みなら新規投稿せず既存メッセージを更新する(Discord-Shiireの「設置・更新」と同じ)。
"""
import discord
from discord import ui

from .config import GAMES
from .store import HubError, HubStore


# ---------------------------------------------------------------- にゃんこ
class NyankoPanelView(ui.View):
    """元の /vending_account は押した人ではなくコマンド実行者に結果を送っていたため、
    常設パネル用に「押した人」を使う永続Viewとして作り直している。"""

    def __init__(self):
        super().__init__(timeout=None)

    async def _open(self, interaction: discord.Interaction, mode: str):
        from Cogs.Account_vending import PRICES, AccountNewModal, AccountPayPayModal

        if interaction.guild is None:
            return await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
        if mode == "new":
            modal = AccountNewModal(PRICES["new"], interaction.user, interaction.guild, interaction.client)
        else:
            modal = AccountPayPayModal(mode, PRICES[mode], interaction.user, interaction.guild,
                                       interaction.client)
        await interaction.response.send_modal(modal)

    @ui.button(label="代行全適用アカウント", style=discord.ButtonStyle.primary, custom_id="cheat:nyanko:full")
    async def full(self, interaction: discord.Interaction, button: ui.Button):
        await self._open(interaction, "full")

    @ui.button(label="アカウント複製", style=discord.ButtonStyle.secondary, custom_id="cheat:nyanko:copy")
    async def copy(self, interaction: discord.Interaction, button: ui.Button):
        await self._open(interaction, "copy")

    @ui.button(label="アカウント復旧", style=discord.ButtonStyle.success, custom_id="cheat:nyanko:restore")
    async def restore(self, interaction: discord.Interaction, button: ui.Button):
        await self._open(interaction, "restore")

    @ui.button(label="新規アカウント作成", style=discord.ButtonStyle.secondary, custom_id="cheat:nyanko:new")
    async def new(self, interaction: discord.Interaction, button: ui.Button):
        await self._open(interaction, "new")


def _nyanko_embed(color: int) -> discord.Embed:
    from Cogs.Account_vending import PRICES

    e = discord.Embed(
        title="アカウント自動販売機",
        description="購入したいメニューを選択してください。\nいずれも1アカウントあたりの価格です。",
        color=color,
    )
    e.add_field(name="代行全適用アカウント", value=f"¥{PRICES['full']}", inline=True)
    e.add_field(name="アカウント複製", value=f"¥{PRICES['copy']}", inline=True)
    e.add_field(name="アカウント復旧", value=f"¥{PRICES['restore']}", inline=True)
    e.add_field(name="新規アカウント作成", value=f"¥{PRICES['new']}", inline=True)
    return e


# ---------------------------------------------------------------- ツムツム
def tsum_embed(color: int) -> discord.Embed:
    import mod_stum

    menu_text = "\n".join(f"・**{m['label']}** — {m['desc']}" for m in mod_stum.MENUS.values())
    return discord.Embed(
        title="🎮 ツムツム代行（無料）",
        description=(
            "**ご利用方法**\n"
            "1️⃣ 「代行を依頼する」を押す\n"
            "2️⃣ 実行したいメニューを選択\n"
            "3️⃣ LINEログイン情報を入力\n"
            "4️⃣ 自動で処理を実行\n\n"
            "**メニュー（すべて無料）**\n"
            f"{menu_text}\n\n"
            "※ LINEログイン情報はJSON等へ保存しません。"
        ),
        color=color,
    )


# ---------------------------------------------------------------- 共通
def default_color(game: str) -> int:
    return GAMES[game].default_color


def panel_embed(game: str, color: int | None = None) -> discord.Embed:
    col = default_color(game) if color is None else color
    if game == "nyanko":
        return _nyanko_embed(col)
    if game == "tsum":
        return tsum_embed(col)
    if game == "puni":
        import ywp_bot

        e = ywp_bot.panel_embed()
        e.color = discord.Colour(col)
        return e
    raise HubError(400, "UNKNOWN_GAME")


def panel_view(game: str) -> ui.View:
    if game == "nyanko":
        return NyankoPanelView()
    if game == "tsum":
        import mod_stum

        return mod_stum.TsumPanelView()
    if game == "puni":
        import ywp_bot

        return ywp_bot.PanelView()
    raise HubError(400, "UNKNOWN_GAME")


def register_views(bot) -> None:
    """再起動後も設置済みパネルのボタンが動くように永続Viewを登録する。"""
    bot.add_view(NyankoPanelView())


async def _resolve_channel(bot, guild, channel_id: str):
    try:
        ch = guild.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
    except discord.NotFound:
        raise HubError(404, "CHANNEL_NOT_FOUND", "チャンネルが見つかりません")
    except discord.Forbidden:
        raise HubError(403, "CHANNEL_FORBIDDEN", "Botがこのチャンネルを見られません")
    if getattr(ch, "guild", None) is None or ch.guild.id != guild.id:
        raise HubError(404, "CHANNEL_NOT_FOUND", "このサーバーのチャンネルではありません")
    if not isinstance(ch, discord.abc.Messageable) or isinstance(ch, discord.abc.PrivateChannel):
        raise HubError(400, "CHANNEL_NOT_TEXT", "テキストチャンネルを選んでください")
    perms = ch.permissions_for(guild.me) if guild.me else None
    if perms is None or not (perms.view_channel and perms.send_messages and perms.embed_links):
        raise HubError(403, "CHANNEL_FORBIDDEN",
                       "Botに「チャンネルを見る・メッセージを送信・埋め込みリンク」の権限がありません")
    return ch


async def deploy_panel(bot, store: HubStore, guild_id: str, game: str, channel_id: str,
                       color: int | None) -> dict:
    if game not in GAMES:
        raise HubError(404, "UNKNOWN_GAME")
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        raise HubError(404, "GUILD_NOT_FOUND", "Discord-Cheat BOTがこのサーバーに参加していません")
    ch = await _resolve_channel(bot, guild, channel_id)

    state = store.guild(guild_id)["panels"][game]
    embed = panel_embed(game, color)
    view = panel_view(game)
    message = None
    updated = False

    # 同じチャンネルに設置済みなら、そのメッセージを書き換える
    if state.get("messageId") and state.get("deployedChannelId") == str(ch.id):
        try:
            message = await ch.fetch_message(int(state["messageId"]))
            await message.edit(embed=embed, view=view)
            updated = True
        except discord.NotFound:
            message = None
        except discord.Forbidden:
            raise HubError(403, "CHANNEL_FORBIDDEN", "Botにメッセージを編集する権限がありません")
        except discord.HTTPException as e:
            raise HubError(502, "DISCORD_ERROR", str(e)[:160])

    if message is None:
        try:
            message = await ch.send(embed=embed, view=view)
        except discord.Forbidden:
            raise HubError(403, "CHANNEL_FORBIDDEN", "Botに送信権限がありません")
        except discord.HTTPException as e:
            raise HubError(502, "DISCORD_ERROR", str(e)[:160])
        # 別チャンネルから移した場合は古いパネルを片付ける(失敗しても無視)
        old_cid, old_mid = state.get("deployedChannelId"), state.get("messageId")
        if old_cid and old_mid and old_cid != str(ch.id):
            try:
                old_ch = guild.get_channel(int(old_cid)) or await bot.fetch_channel(int(old_cid))
                old_msg = await old_ch.fetch_message(int(old_mid))
                await old_msg.delete()
            except (discord.HTTPException, ValueError, AttributeError):
                pass

    store.mark_deployed(guild_id, game, channel_id=str(ch.id), message_id=str(message.id), color=color)
    return {"channelId": str(ch.id), "messageId": str(message.id), "updated": updated}
