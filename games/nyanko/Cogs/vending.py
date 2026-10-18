import discord
from discord import app_commands, ui
from discord.ext import commands
import time
import json
import os
import uuid
from Cogs.utils import load_items, load_config, save_config, is_allowed
from Cogs.nyanko_editor import CloudEditor
import paypayu

VENDING_DATA_FILE = "vending_data.json"
LOG_CHANNEL_FILE = "log_channels.json"
PAYPAY_DATA_FILE = "paypay_data.json"
SALES_FILE = "sales_history.json"

def load_vending_data():
    if os.path.exists(VENDING_DATA_FILE):
        with open(VENDING_DATA_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except:
                return {}
    return {}

def save_vending_data(data):
    with open(VENDING_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def load_log_channels():
    if os.path.exists(LOG_CHANNEL_FILE):
        with open(LOG_CHANNEL_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except:
                return {}
    return {}

def save_log_channels(data):
    with open(LOG_CHANNEL_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def load_paypay_accounts():
    if os.path.exists(PAYPAY_DATA_FILE):
        with open(PAYPAY_DATA_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except:
                return {}
    return {}

def load_sales_history():
    if os.path.exists(SALES_FILE):
        with open(SALES_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except:
                return {}
    return {}

def save_sales_history(data):
    with open(SALES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

class ProductSelectDropdown(ui.Select):
    """商品選択ドロップダウン（前半または後半）"""
    def __init__(self, items, vending_id, user, guild, bot, offset=0, label_suffix=""):
        self.items = items
        self.vending_id = vending_id
        self.user = user
        self.guild = guild
        self.bot = bot
        self.offset = offset

        options = [
            discord.SelectOption(
                label=f"{item['name']} (¥{item['price']})",
                value=str(offset + i),
                description=f"価格: ¥{item['price']}"
            )
            for i, item in enumerate(items)
        ]

        super().__init__(
            placeholder=f"購入したいアイテムを選択してください{label_suffix}",
            min_values=1,
            max_values=min(25, len(items)),
            options=options
        )
    
    async def callback(self, interaction: discord.Interaction):
        selected_items = []

        for idx in self.values:
            # valueはoffset込みのインデックスなので、items内のインデックスに変換
            local_idx = int(idx) - self.offset
            item = self.items[local_idx]
            selected_items.append({
                'name': item['name'],
                'price': item['price'],
                'quantity': 1,
                'subtotal': item['price']
            })
        
        # 注文確認画面
        embed = discord.Embed(
            title="注文確認",
            color=0x2ecc71
        )
        
        total_price = sum(item['subtotal'] for item in selected_items)
        
        embed.add_field(
            name="選択アイテム",
            value="\n".join([f"{item['name']} × {item['quantity']}個" for item in selected_items]),
            inline=False
        )
        
        for item in selected_items:
            embed.add_field(
                name=item['name'],
                value=f"¥{item['price']} × {item['quantity']}個 = ¥{item['subtotal']}",
                inline=False
            )
        
        embed.add_field(name="合計金額", value=f"{total_price}円", inline=False)
        
        view = ui.View()
        
        async def buy_cb(it):
            await it.response.send_modal(PayPayModal(selected_items, total_price, self.user, self.guild, self.bot, self.vending_id))
        
        btn = ui.Button(label="購入する", style=discord.ButtonStyle.success)
        btn.callback = buy_cb
        view.add_item(btn)
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

class PayPayModal(ui.Modal, title="注文合計"):
    """PayPayリンク入力"""
    paypay_link = ui.TextInput(
        label="PayPayリンク *",
        placeholder="https://pay.paypay.ne.jp/...",
        required=True
    )
    transfer_code = ui.TextInput(
        label="引継ぎコード *",
        placeholder="引継ぎコード",
        required=True
    )
    pin = ui.TextInput(
        label="PIN *",
        placeholder="PIN",
        required=True
    )

    def __init__(self, selected_items, total_price, user, guild, bot, vending_id):
        super().__init__()
        self.selected_items = selected_items
        self.total_price = total_price
        self.user = user
        self.guild = guild
        self.bot = bot
        self.vending_id = vending_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            if not self.paypay_link.value.startswith("https://pay.paypay.ne.jp/"):
                embed = discord.Embed(
                    title="❌ エラー",
                    description="有効なPayPayリンクを入力してください",
                    color=0xff0000
                )
                return await interaction.followup.send(embed=embed, ephemeral=True)
            
            # PayPayリンク確認
            await interaction.followup.send(
                embed=discord.Embed(
                    title="🔄 処理中",
                    description="PayPayリンクを確認中...",
                    color=0xFFB700
                ),
                ephemeral=True
            )
            
            payment_info = await paypayu.check_link(self.paypay_link.value)
            if not payment_info:
                embed = discord.Embed(
                    title="❌ エラー",
                    description="有効なPayPayリンクではありません",
                    color=0xff0000
                )
                return await interaction.followup.send(embed=embed, ephemeral=True)
            
            amount = payment_info.get("payload", {}).get("message", {}).get("data", {}).get("amount")
            if amount is None:
                embed = discord.Embed(
                    title="❌ エラー",
                    description="PayPayリンクから金額を取得できませんでした",
                    color=0xff0000
                )
                return await interaction.followup.send(embed=embed, ephemeral=True)
            if amount < self.total_price:
                embed = discord.Embed(
                    title="❌ 金額不足",
                    description=f"必要な金額: ¥{self.total_price}\n送信された金額: ¥{amount}",
                    color=0xff0000
                )
                return await interaction.followup.send(embed=embed, ephemeral=True)
            
            # PayPay受け取り
            paypay_accounts = load_paypay_accounts()
            owner_account = paypay_accounts.get(str(self.bot.owner_id))
            
            if not owner_account:
                embed = discord.Embed(
                    title="❌ エラー",
                    description="オーナーのPayPayアカウントが登録されていません",
                    color=0xff0000
                )
                return await interaction.followup.send(embed=embed, ephemeral=True)
            
            result = await paypayu.link_rev(
                self.paypay_link.value,
                owner_account["phone"],
                owner_account["password"],
                owner_account["uuid"]
            )
            
            if result != True:
                embed = discord.Embed(
                    title="❌ PayPay受け取り失敗",
                    description="PayPayリンクが無効か期限切れです",
                    color=0xff0000
                )
                return await interaction.followup.send(embed=embed, ephemeral=True)
            
            # 改造実行
            await interaction.followup.send(
                embed=discord.Embed(
                    title="🔄 代行中",
                    description="セーブファイルを改造中...",
                    color=0xFFB700
                ),
                ephemeral=True
            )
            
            editor = CloudEditor(
                self.transfer_code.value, 
                self.pin.value, 
                self.user, 
                self.guild.id,
                modifications=self.selected_items
            )
            
            # セーブダウンロード
            if not editor.download_save():
                embed = discord.Embed(
                    title="❌ エラー",
                    description="引継ぎコードまたはPINが正しくありません",
                    color=0xff0000
                )
                return await interaction.followup.send(embed=embed, ephemeral=True)
            
            # セーブ改造
            if not editor.apply_modifications():
                embed = discord.Embed(
                    title="❌ エラー",
                    description=editor.last_error,
                    color=0xff0000
                )
                return await interaction.followup.send(embed=embed, ephemeral=True)
            
            # セーブアップロード
            new_code, new_pin = editor.upload_save()
            
            if new_code and new_pin:
                # 販売履歴に記録
                sales_history = load_sales_history()
                if self.vending_id not in sales_history:
                    sales_history[self.vending_id] = []
                
                sale_record = {
                    "timestamp": int(time.time()),
                    "user_id": str(self.user.id),
                    "user_name": str(self.user.name),
                    "items": self.selected_items,
                    "total_price": self.total_price
                }
                sales_history[self.vending_id].append(sale_record)
                save_sales_history(sales_history)
                
                # ユーザーにDM送信
                dm_embed = discord.Embed(
                    title="✅ 代行完了",
                    color=0x2ecc71
                )
                
                items_text = "\n".join([f"{item['name']} × {item['quantity']}個" for item in self.selected_items])
                dm_embed.add_field(name="購入商品", value=items_text, inline=False)
                dm_embed.add_field(name="合計金額", value=f"¥{self.total_price}", inline=False)
                dm_embed.add_field(name="新しい引継ぎコード", value=f"`{new_code}`", inline=False)
                dm_embed.add_field(name="PIN", value=f"`{new_pin}`", inline=False)
                dm_embed.set_footer(text="必ず保存してください")
                
                try:
                    await self.user.send(embed=dm_embed)
                except:
                    pass
                
                confirm_embed = discord.Embed(
                    title="✅ 代行完了",
                    description="PayPayを自動受け取りしました\n新しい引継ぎコードをDMで送信しました",
                    color=0x2ecc71
                )
                await interaction.followup.send(embed=confirm_embed, ephemeral=True)

                # 完了実績(管理画面で設定したチャンネル。単体起動では何もしない)
                try:
                    from hub.achievements import post_achievement
                except ImportError:
                    post_achievement = None
                if post_achievement is not None:
                    await post_achievement(
                        self.bot, game="nyanko", user=self.user, guild=self.guild,
                        menu="\n".join(f"{i['name']} × {i['quantity']}個" for i in self.selected_items),
                        amount=self.total_price)
                
                # ロール付与
                vending_data = load_vending_data()
                vm = vending_data.get(self.vending_id, {})
                if vm.get("role_id"):
                    role = self.guild.get_role(vm["role_id"])
                    if role and role not in self.user.roles:
                        try:
                            await self.user.add_roles(role)
                        except:
                            pass
                
                # ログ記録
                log_channels = load_log_channels()
                guild_logs = log_channels.get(str(self.guild.id), {})
                
                items_text = "\n".join([f"{item['name']} × {item['quantity']}個" for item in self.selected_items])
                
                if guild_logs.get("public"):
                    ch = self.bot.get_channel(guild_logs["public"])
                    if ch:
                        log_embed = discord.Embed(title="✅ 代行完了", color=0x2ecc71)
                        log_embed.set_author(name=self.user.name, icon_url=self.user.display_avatar.url)
                        log_embed.add_field(name="ユーザー", value=self.user.mention, inline=False)
                        log_embed.add_field(name="商品", value=items_text, inline=False)
                        log_embed.add_field(name="合計金額", value=f"¥{self.total_price}", inline=False)
                        log_embed.add_field(name="日時", value=f"<t:{int(time.time())}:F>", inline=False)
                        await ch.send(embed=log_embed)
                
                if guild_logs.get("private"):
                    ch = self.bot.get_channel(guild_logs["private"])
                    if ch:
                        log_embed = discord.Embed(title="✅ 代行完了", color=0x3498db)
                        log_embed.set_author(name=self.user.name, icon_url=self.user.display_avatar.url)
                        log_embed.add_field(name="ユーザー", value=self.user.mention, inline=False)
                        log_embed.add_field(name="商品", value=items_text, inline=False)
                        log_embed.add_field(name="合計金額", value=f"¥{self.total_price}", inline=False)
                        log_embed.add_field(name="新コード", value=f"`{new_code}`", inline=False)
                        log_embed.add_field(name="日時", value=f"<t:{int(time.time())}:F>", inline=False)
                        await ch.send(embed=log_embed)
            else:
                embed = discord.Embed(
                    title="❌ エラー",
                    description=f"アップロード失敗: {editor.last_error}",
                    color=0xff0000
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
        
        except Exception as e:
            embed = discord.Embed(
                title="❌ エラー",
                description=f"```{str(e)}```",
                color=0xff0000
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

class VendingView(ui.View):
    """販売機メイン"""
    def __init__(self, all_items, vending_id, user, guild, bot):
        super().__init__()

        mid = len(all_items) // 2
        first_half = all_items[:mid]
        second_half = all_items[mid:]

        self.add_item(ProductSelectDropdown(first_half, vending_id, user, guild, bot, offset=0, label_suffix="（前半）"))
        self.add_item(ProductSelectDropdown(second_half, vending_id, user, guild, bot, offset=mid, label_suffix="（後半）"))

async def vending_machine_autocomplete(interaction: discord.Interaction, current: str):
    vending_data = load_vending_data()
    user_id_str = str(interaction.user.id)
    
    user_machines = [
        (vm_id, vm_data) for vm_id, vm_data in vending_data.items() 
        if vm_data.get("owner_id") == user_id_str
    ]

    return [
        app_commands.Choice(name=vm_data.get("name", "名称未設定"), value=vm_id)
        for vm_id, vm_data in user_machines
        if current.lower() in vm_data.get("name", "").lower()
    ]

class VendingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="vending_create", description="自販機を作成")
    @is_allowed()
    async def create_vending(self, interaction: discord.Interaction, name: str):
        """自販機作成"""
        vending_data = load_vending_data()
        vm_id = str(uuid.uuid4())
        
        vending_data[vm_id] = {
            "name": name,
            "owner_id": str(interaction.user.id),
            "role_id": None,
            "custom_items": []
        }
        save_vending_data(vending_data)
        
        embed = discord.Embed(
            title="✅ 自販機作成",
            description=f"自販機「{name}」を作成しました\n**ID:** `{vm_id}`",
            color=0x2ecc71
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="vending_add_item", description="自販機に商品を追加")
    @is_allowed()
    @app_commands.autocomplete(vending_id=vending_machine_autocomplete)
    async def add_item(self, interaction: discord.Interaction, vending_id: str, name: str, price: int):
        """商品追加"""
        vending_data = load_vending_data()
        vm = vending_data.get(vending_id)
        
        if not vm or vm.get("owner_id") != str(interaction.user.id):
            await interaction.response.send_message("指定された自販機が見つかりません", ephemeral=True)
            return
        
        vm["custom_items"].append({
            "name": name,
            "price": price
        })
        save_vending_data(vending_data)
        
        embed = discord.Embed(
            title="✅ 商品追加",
            description=f"商品「{name}」(¥{price})を追加しました",
            color=0x2ecc71
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="vending", description="自動販売機")
    @is_allowed()
    @app_commands.autocomplete(vending_id=vending_machine_autocomplete)
    async def vending_machine(self, interaction: discord.Interaction, vending_id: str):
        """自動販売機"""
        vending_data = load_vending_data()
        vm = vending_data.get(vending_id)
        
        if not vm or vm.get("owner_id") != str(interaction.user.id):
            await interaction.response.send_message("指定された自販機が見つかりません", ephemeral=True)
            return
        
        items = load_items()
        
        # カスタム商品を追加
        if vm.get("custom_items"):
            items['menu1'].extend(vm["custom_items"])

        all_items = items['menu1'] + items['menu2']

        # Embedに商品一覧を表示
        embed = discord.Embed(
            title=vm['name'],
            description="購入したいアイテムを以下から選択してください。",
            color=0x2b2d31
        )

        menu1_lines = "\n".join([f"**{item['name']}**\n{item['price']}円" for item in items['menu1']])
        menu2_lines = "\n".join([f"**{item['name']}**\n{item['price']}円" for item in items['menu2']])

        if menu1_lines:
            embed.add_field(name="メニュー1", value=menu1_lines, inline=False)
        if menu2_lines:
            embed.add_field(name="メニュー2", value=menu2_lines, inline=False)

        view = VendingView(all_items, vending_id, interaction.user, interaction.guild, self.bot)
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="vending_sales", description="販売利益を表示")
    @is_allowed()
    @app_commands.autocomplete(vending_id=vending_machine_autocomplete)
    async def show_sales(self, interaction: discord.Interaction, vending_id: str):
        """販売利益表示"""
        vending_data = load_vending_data()
        vm = vending_data.get(vending_id)
        
        if not vm or vm.get("owner_id") != str(interaction.user.id):
            await interaction.response.send_message("指定された自販機が見つかりません", ephemeral=True)
            return
        
        sales_history = load_sales_history()
        sales = sales_history.get(vending_id, [])
        
        if not sales:
            embed = discord.Embed(
                title=f"📊 {vm['name']} - 販売履歴",
                description="まだ販売実績がありません",
                color=0x3498db
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # 計算
        total_revenue = sum(s['total_price'] for s in sales)
        total_sales = len(sales)
        
        # 商品別の集計
        item_stats = {}
        for sale in sales:
            for item in sale['items']:
                item_name = item['name']
                if item_name not in item_stats:
                    item_stats[item_name] = {
                        'quantity': 0,
                        'revenue': 0
                    }
                item_stats[item_name]['quantity'] += item['quantity']
                item_stats[item_name]['revenue'] += item['subtotal']
        
        # 販売利益表示
        embed = discord.Embed(
            title=f"📊 {vm['name']} - 販売利益",
            color=0x3498db
        )
        
        embed.add_field(name="総売上", value=f"¥{total_revenue}", inline=False)
        embed.add_field(name="販売件数", value=f"{total_sales}件", inline=False)
        embed.add_field(name="平均単価", value=f"¥{int(total_revenue / total_sales)}", inline=False)
        
        embed.add_field(name="商品別売上", value="```\n" + "\n".join([
            f"{name}: ¥{stats['revenue']}（{stats['quantity']}個）"
            for name, stats in sorted(item_stats.items(), key=lambda x: x[1]['revenue'], reverse=True)
        ]) + "```", inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="vending_set_role", description="購入時に付与するロールを設定")
    @is_allowed()
    @app_commands.autocomplete(vending_id=vending_machine_autocomplete)
    async def set_vending_role(self, interaction: discord.Interaction, vending_id: str, role: discord.Role):
        """ロール付与設定"""
        vending_data = load_vending_data()
        vm = vending_data.get(vending_id)
        
        if not vm or vm.get("owner_id") != str(interaction.user.id):
            await interaction.response.send_message("指定された自販機が見つかりません", ephemeral=True)
            return
        
        vm["role_id"] = role.id
        save_vending_data(vending_data)
        
        embed = discord.Embed(
            title="✅ ロール設定",
            description=f"購入時に {role.mention} を付与するように設定しました",
            color=0x2ecc71
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="log_channel", description="公開ログチャンネルを設定")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """公開ログチャンネル設定"""
        log_channels = load_log_channels()
        guild_id = str(interaction.guild.id)
        
        if guild_id not in log_channels:
            log_channels[guild_id] = {}
        
        log_channels[guild_id]["public"] = channel.id
        save_log_channels(log_channels)
        
        embed = discord.Embed(
            title="✅ 公開ログチャンネル設定",
            description=f"{channel.mention} に設定しました",
            color=0x2ecc71
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="private_log_channel", description="非公開ログチャンネルを設定")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_private_log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """非公開ログチャンネル設定"""
        log_channels = load_log_channels()
        guild_id = str(interaction.guild.id)
        
        if guild_id not in log_channels:
            log_channels[guild_id] = {}
        
        log_channels[guild_id]["private"] = channel.id
        save_log_channels(log_channels)
        
        embed = discord.Embed(
            title="✅ 非公開ログチャンネル設定",
            description=f"{channel.mention} に設定しました",
            color=0x2ecc71
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(VendingCog(bot))