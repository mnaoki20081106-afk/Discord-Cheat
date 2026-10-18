import discord
from discord import app_commands, ui
from discord.ext import commands
import json
import os
import time
from Cogs.utils import load_items, is_allowed
from Cogs.nyanko_editor import CloudEditor
import paypayu

PAYPAY_DATA_FILE = "paypay_data.json"
ADMIN_USERS_FILE = "admin_users.json"

PRICES = {
    "full": 1000,
    "copy": 400,
    "restore": 50,
    "new": 10,
}

def load_paypay_accounts():
    if os.path.exists(PAYPAY_DATA_FILE):
        with open(PAYPAY_DATA_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except:
                return {}
    return {}

def load_admin_users():
    if os.path.exists(ADMIN_USERS_FILE):
        with open(ADMIN_USERS_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except:
                return []
    return []

def get_all_modifications():
    items = load_items()
    all_items = items['menu1'] + items['menu2']
    return [
        {'name': item['name'], 'price': item['price'], 'quantity': 1, 'subtotal': item['price']}
        for item in all_items
    ]


# ===== PayPay支払い共通処理 =====
async def process_paypay(interaction, paypay_link, price, bot):
    """PayPayリンクの確認・受け取りを行う。成功時Trueを返す"""
    payment_info = await paypayu.check_link(paypay_link)
    if not payment_info:
        await interaction.followup.send(
            embed=discord.Embed(title="エラー", description="有効なPayPayリンクではありません", color=0xff0000),
            ephemeral=True
        )
        return False

    amount = payment_info.get("payload", {}).get("message", {}).get("data", {}).get("amount")
    if amount is None or amount < price:
        await interaction.followup.send(
            embed=discord.Embed(title="金額不足", description=f"必要金額: ¥{price}\n送信金額: ¥{amount or 0}", color=0xff0000),
            ephemeral=True
        )
        return False

    paypay_accounts = load_paypay_accounts()
    owner_account = paypay_accounts.get(str(bot.owner_id))
    if not owner_account:
        await interaction.followup.send(
            embed=discord.Embed(title="エラー", description="オーナーのPayPayアカウントが登録されていません", color=0xff0000),
            ephemeral=True
        )
        return False

    result = await paypayu.link_rev(paypay_link, owner_account["phone"], owner_account["password"], owner_account["uuid"])
    if result != True:
        await interaction.followup.send(
            embed=discord.Embed(title="PayPay受け取り失敗", description="リンクが無効か期限切れです", color=0xff0000),
            ephemeral=True
        )
        return False

    return True


# ===== 有料モーダル（PayPay + 引継ぎコード） =====
class AccountPayPayModal(ui.Modal, title="お支払い・引継ぎコード入力"):
    paypay_link = ui.TextInput(label="PayPayリンク", placeholder="https://pay.paypay.ne.jp/...", required=True)
    transfer_code = ui.TextInput(label="引継ぎコード", placeholder="引継ぎコード", required=True)
    pin = ui.TextInput(label="PIN", placeholder="PIN", required=True)

    def __init__(self, mode, price, user, guild, bot):
        super().__init__()
        self.mode = mode
        self.price = price
        self.user = user
        self.guild = guild
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not self.paypay_link.value.startswith("https://pay.paypay.ne.jp/"):
            return await interaction.followup.send(
                embed=discord.Embed(title="エラー", description="有効なPayPayリンクを入力してください", color=0xff0000),
                ephemeral=True
            )

        await interaction.followup.send(
            embed=discord.Embed(title="処理中", description="PayPayリンクを確認中...", color=0xFFB700),
            ephemeral=True
        )

        if not await process_paypay(interaction, self.paypay_link.value, self.price, self.bot):
            return

        await interaction.followup.send(
            embed=discord.Embed(title="処理中", description="セーブファイルを処理中...", color=0xFFB700),
            ephemeral=True
        )

        handler = AccountHandler(self.transfer_code.value, self.pin.value, self.user, self.guild, self.bot,
                                 award=True, price=self.price)
        if self.mode == "full":
            await handler.handle_full(interaction)
        elif self.mode == "copy":
            await handler.handle_copy(interaction)
        elif self.mode == "restore":
            await handler.handle_restore(interaction)


# ===== 有料モーダル（PayPayのみ・新規作成用） =====
class AccountNewModal(ui.Modal, title="新規アカウント作成"):
    paypay_link = ui.TextInput(label="PayPayリンク", placeholder="https://pay.paypay.ne.jp/...", required=True)

    def __init__(self, price, user, guild, bot):
        super().__init__()
        self.price = price
        self.user = user
        self.guild = guild
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not self.paypay_link.value.startswith("https://pay.paypay.ne.jp/"):
            return await interaction.followup.send(
                embed=discord.Embed(title="エラー", description="有効なPayPayリンクを入力してください", color=0xff0000),
                ephemeral=True
            )

        await interaction.followup.send(
            embed=discord.Embed(title="処理中", description="PayPayリンクを確認中...", color=0xFFB700),
            ephemeral=True
        )

        if not await process_paypay(interaction, self.paypay_link.value, self.price, self.bot):
            return

        await interaction.followup.send(
            embed=discord.Embed(title="処理中", description="新規アカウントを作成中...", color=0xFFB700),
            ephemeral=True
        )

        handler = AccountHandler(None, None, self.user, self.guild, self.bot, award=True, price=self.price)
        await handler.handle_new(interaction)


# ===== 無料モーダル（引継ぎコード） =====
class AccountFreeModal(ui.Modal, title="引継ぎコード入力"):
    transfer_code = ui.TextInput(label="引継ぎコード", placeholder="引継ぎコード", required=True)
    pin = ui.TextInput(label="PIN", placeholder="PIN", required=True)

    def __init__(self, mode, user, guild, bot):
        super().__init__()
        self.mode = mode
        self.user = user
        self.guild = guild
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        await interaction.followup.send(
            embed=discord.Embed(title="処理中", description="セーブファイルをダウンロード中...", color=0xFFB700),
            ephemeral=True
        )

        handler = AccountHandler(self.transfer_code.value, self.pin.value, self.user, self.guild, self.bot)
        if self.mode == "full":
            await handler.handle_full(interaction)
        elif self.mode == "copy":
            await handler.handle_copy(interaction)
        elif self.mode == "restore":
            await handler.handle_restore(interaction)


# ===== 無料モーダル（新規作成） =====
class AccountNewFreeModal(ui.Modal, title="新規アカウント作成（テスト）"):
    confirm = ui.TextInput(label="確認", placeholder="「作成」と入力してください", required=True)

    def __init__(self, user, guild, bot):
        super().__init__()
        self.user = user
        self.guild = guild
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            embed=discord.Embed(title="処理中", description="新規アカウントを作成中...", color=0xFFB700),
            ephemeral=True
        )
        handler = AccountHandler(None, None, self.user, self.guild, self.bot)
        await handler.handle_new(interaction)


# ===== 共通処理クラス =====
class AccountHandler:
    def __init__(self, transfer_code, pin, user, guild, bot, award=False, price=0):
        self.transfer_code = transfer_code
        self.pin = pin
        self.user = user
        self.guild = guild
        self.bot = bot
        self.award = award      # 有料注文だけ完了実績に載せる(テスト販売機は載せない)
        self.price = price

    async def _done(self, menu):
        """完了実績を管理画面で設定したチャンネルへ送る(単体起動では何もしない)"""
        if not self.award:
            return
        try:
            from hub.achievements import post_achievement
        except ImportError:
            return
        await post_achievement(self.bot, game="nyanko", user=self.user, guild=self.guild,
                               menu=menu, amount=self.price)

    async def handle_full(self, interaction):
        mods = get_all_modifications()
        editor = CloudEditor(self.transfer_code, self.pin, self.user, self.guild.id, modifications=mods)

        if not editor.download_save():
            return await interaction.followup.send(
                embed=discord.Embed(title="ダウンロード失敗", description="引継ぎコードまたはPINが正しくありません", color=0xff0000),
                ephemeral=True
            )

        if not editor.apply_modifications():
            return await interaction.followup.send(
                embed=discord.Embed(title="失敗", description=editor.last_error, color=0xff0000),
                ephemeral=True
            )

        new_code, new_pin = editor.upload_save()
        if new_code and new_pin:
            dm_embed = discord.Embed(title="代行全適用 完了", color=0x2ecc71)
            dm_embed.add_field(name="新しい引継ぎコード", value=f"`{new_code}`", inline=False)
            dm_embed.add_field(name="PIN", value=f"`{new_pin}`", inline=False)
            try:
                await self.user.send(embed=dm_embed)
            except:
                pass
            await interaction.followup.send(
                embed=discord.Embed(title="完了", description="全適用が完了しました\n新しい引継ぎコードをDMで送信しました", color=0x2ecc71),
                ephemeral=True
            )
            await self._done("代行全適用アカウント")
        else:
            await interaction.followup.send(
                embed=discord.Embed(title="アップロード失敗", description=editor.last_error, color=0xff0000),
                ephemeral=True
            )

    async def handle_copy(self, interaction):
        editor = CloudEditor(self.transfer_code, self.pin, self.user, self.guild.id, modifications=[])

        if not editor.download_save():
            return await interaction.followup.send(
                embed=discord.Embed(title="ダウンロード失敗", description="引継ぎコードまたはPINが正しくありません", color=0xff0000),
                ephemeral=True
            )

        original_save_data = editor.save_data
        new_code, new_pin = editor.upload_save()
        if not (new_code and new_pin):
            return await interaction.followup.send(
                embed=discord.Embed(title="複製失敗", description=editor.last_error, color=0xff0000),
                ephemeral=True
            )

        editor.save_data = original_save_data
        orig_new_code, orig_new_pin = editor.upload_save()

        dm_embed = discord.Embed(title="アカウント複製 完了", color=0x2ecc71)
        if orig_new_code and orig_new_pin:
            dm_embed.add_field(name="元アカウント コード", value=f"`{orig_new_code}`", inline=False)
            dm_embed.add_field(name="元アカウント PIN", value=f"`{orig_new_pin}`", inline=False)
        else:
            dm_embed.add_field(name="元アカウント", value="再発行失敗（手動で再発行してください）", inline=False)
        dm_embed.add_field(name="複製アカウント コード", value=f"`{new_code}`", inline=False)
        dm_embed.add_field(name="複製アカウント PIN", value=f"`{new_pin}`", inline=False)
        dm_embed.set_footer(text="どちらも新しいコードで引継ぎしてください")

        try:
            await self.user.send(embed=dm_embed)
        except:
            pass

        await interaction.followup.send(
            embed=discord.Embed(title="完了", description="複製が完了しました\n元・複製アカウント両方のコードをDMで送信しました", color=0x2ecc71),
            ephemeral=True
        )
        await self._done("アカウント複製")

    async def handle_restore(self, interaction):
        editor = CloudEditor(self.transfer_code, self.pin, self.user, self.guild.id, modifications=[])

        if not editor.download_save():
            return await interaction.followup.send(
                embed=discord.Embed(title="ダウンロード失敗", description="引継ぎコードまたはPINが正しくありません", color=0xff0000),
                ephemeral=True
            )

        try:
            import bcsfe.core as bc
            bc.core_data.init_data()
            data = bc.Data(editor.save_data)
            save = bc.SaveFile(dt=data, cc=bc.CountryCode("ja"))

            if hasattr(save, 'show_ban_message'):
                save.show_ban_message = False
            if hasattr(save, 'rank_up_sale_value'):
                save.rank_up_sale_value = 0
            save.max_rank_up_sale()

            out = save.to_data()
            editor.save_data = out.to_bytes()
        except Exception as e:
            return await interaction.followup.send(
                embed=discord.Embed(title="復旧処理失敗", description=str(e), color=0xff0000),
                ephemeral=True
            )

        new_code, new_pin = editor.upload_save()
        if new_code and new_pin:
            dm_embed = discord.Embed(title="アカウント復旧 完了", color=0x2ecc71)
            dm_embed.add_field(name="新しい引継ぎコード", value=f"`{new_code}`", inline=False)
            dm_embed.add_field(name="PIN", value=f"`{new_pin}`", inline=False)
            try:
                await self.user.send(embed=dm_embed)
            except:
                pass
            await interaction.followup.send(
                embed=discord.Embed(title="完了", description="復旧が完了しました\n新しい引継ぎコードをDMで送信しました", color=0x2ecc71),
                ephemeral=True
            )
            await self._done("アカウント復旧")
        else:
            await interaction.followup.send(
                embed=discord.Embed(title="アップロード失敗", description=editor.last_error, color=0xff0000),
                ephemeral=True
            )

    async def handle_new(self, interaction):
        """新規アカウント作成 - download_saveなしで直接コードを発行"""
        try:
            import bcsfe.core as bc
            bc.core_data.init_data()

            # inquiry_codeを新規発行
            tmp_save = bc.SaveFile(cc=bc.CountryCode("ja"), load=False)
            tmp_save.init_save()

            tmp_handler = bc.ServerHandler(tmp_save, print=False)

            # inquiry_codeを先に取得してセット
            new_iq = tmp_handler.get_new_inquiry_code()
            if new_iq is None:
                return await interaction.followup.send(
                    embed=discord.Embed(title="作成失敗", description="サーバーへの接続に失敗しました", color=0xff0000),
                    ephemeral=True
                )
            tmp_save.inquiry_code = new_iq

            result = tmp_handler.get_codes()

            if result is None:
                return await interaction.followup.send(
                    embed=discord.Embed(title="作成失敗", description="新規アカウントの作成に失敗しました", color=0xff0000),
                    ephemeral=True
                )

            new_code, new_pin = result

            dm_embed = discord.Embed(title="新規アカウント作成 完了", color=0x2ecc71)
            dm_embed.add_field(name="引継ぎコード", value=f"`{new_code}`", inline=False)
            dm_embed.add_field(name="PIN", value=f"`{new_pin}`", inline=False)
            dm_embed.set_footer(text="必ず保存してください")
            try:
                await self.user.send(embed=dm_embed)
            except:
                pass

            await interaction.followup.send(
                embed=discord.Embed(title="完了", description="新規アカウントを作成しました\n引継ぎコードをDMで送信しました", color=0x2ecc71),
                ephemeral=True
            )
            await self._done("新規アカウント作成")

        except Exception as e:
            await interaction.followup.send(
                embed=discord.Embed(title="エラー", description=f"```{str(e)}```", color=0xff0000),
                ephemeral=True
            )


class AccountVendingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="vending_account", description="アカウント自動販売機")
    @is_allowed()
    async def vending_account(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="アカウント自動販売機",
            description="購入したいメニューを選択してください。\nいずれも1アカウントあたりの価格です。",
            color=0x2b2d31
        )
        embed.add_field(name="代行全適用アカウント", value=f"¥{PRICES['full']}", inline=True)
        embed.add_field(name="アカウント複製", value=f"¥{PRICES['copy']}", inline=True)
        embed.add_field(name="アカウント復旧", value=f"¥{PRICES['restore']}", inline=True)
        embed.add_field(name="新規アカウント作成", value=f"¥{PRICES['new']}", inline=True)

        view = ui.View()

        async def full_cb(it):
            await it.response.send_modal(AccountPayPayModal("full", PRICES["full"], interaction.user, interaction.guild, self.bot))
        async def copy_cb(it):
            await it.response.send_modal(AccountPayPayModal("copy", PRICES["copy"], interaction.user, interaction.guild, self.bot))
        async def restore_cb(it):
            await it.response.send_modal(AccountPayPayModal("restore", PRICES["restore"], interaction.user, interaction.guild, self.bot))
        async def new_cb(it):
            await it.response.send_modal(AccountNewModal(PRICES["new"], interaction.user, interaction.guild, self.bot))

        btn_full = ui.Button(label="代行全適用アカウント", style=discord.ButtonStyle.primary)
        btn_full.callback = full_cb
        btn_copy = ui.Button(label="アカウント複製", style=discord.ButtonStyle.secondary)
        btn_copy.callback = copy_cb
        btn_restore = ui.Button(label="アカウント復旧", style=discord.ButtonStyle.success)
        btn_restore.callback = restore_cb
        btn_new = ui.Button(label="新規アカウント作成", style=discord.ButtonStyle.secondary)
        btn_new.callback = new_cb

        view.add_item(btn_full)
        view.add_item(btn_copy)
        view.add_item(btn_restore)
        view.add_item(btn_new)

        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="test_account_vending", description="テスト用アカウント自動販売機（管理者のみ）")
    async def test_account_vending(self, interaction: discord.Interaction):
        admin_users = load_admin_users()

        if interaction.user.id != self.bot.owner_id and interaction.user.id not in admin_users:
            return await interaction.response.send_message(
                embed=discord.Embed(title="権限がありません", description="このコマンドは管理者のみ使用できます", color=0xff0000),
                ephemeral=True
            )

        embed = discord.Embed(title="テスト用アカウント自動販売機", description="すべて無料です", color=0xFFB700)
        embed.add_field(name="代行全適用アカウント", value="無料", inline=True)
        embed.add_field(name="アカウント複製", value="無料", inline=True)
        embed.add_field(name="アカウント復旧", value="無料", inline=True)
        embed.add_field(name="新規アカウント作成", value="無料", inline=True)

        view = ui.View()

        async def full_cb(it):
            await it.response.send_modal(AccountFreeModal("full", interaction.user, interaction.guild, self.bot))
        async def copy_cb(it):
            await it.response.send_modal(AccountFreeModal("copy", interaction.user, interaction.guild, self.bot))
        async def restore_cb(it):
            await it.response.send_modal(AccountFreeModal("restore", interaction.user, interaction.guild, self.bot))
        async def new_cb(it):
            await it.response.send_modal(AccountNewFreeModal(interaction.user, interaction.guild, self.bot))

        btn_full = ui.Button(label="代行全適用アカウント", style=discord.ButtonStyle.primary)
        btn_full.callback = full_cb
        btn_copy = ui.Button(label="アカウント複製", style=discord.ButtonStyle.secondary)
        btn_copy.callback = copy_cb
        btn_restore = ui.Button(label="アカウント復旧", style=discord.ButtonStyle.success)
        btn_restore.callback = restore_cb
        btn_new = ui.Button(label="新規アカウント作成", style=discord.ButtonStyle.secondary)
        btn_new.callback = new_cb

        view.add_item(btn_full)
        view.add_item(btn_copy)
        view.add_item(btn_restore)
        view.add_item(btn_new)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(AccountVendingCog(bot))