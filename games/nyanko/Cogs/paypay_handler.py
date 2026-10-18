import discord
from discord import ui
from discord.ext import commands
from discord import app_commands
import json
import os
import uuid
from Cogs.utils import is_allowed
import paypayu

PAYPAY_DATA_FILE = "paypay_data.json"

def load_paypay_data():
    if os.path.exists(PAYPAY_DATA_FILE):
        with open(PAYPAY_DATA_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except:
                return {}
    return {}

def save_paypay_data(data):
    with open(PAYPAY_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

class PayPayModal(ui.Modal, title="PayPay OTP認証"):
    def __init__(self, phone, password, set_uuid, otpid, otp_pre):
        super().__init__(timeout=300)
        self.phone = phone
        self.password = password
        self.set_uuid = set_uuid
        self.otpid = otpid
        self.otp_pre = otp_pre

    otp_input = ui.TextInput(label="ワンタイムパスワード", placeholder="SMSに届いた4桁の認証コードを入力", min_length=4, max_length=4, required=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        # OTP認証実行
        otp_result = await paypayu.login_otp(self.set_uuid, self.otp_input.value, self.otpid, self.otp_pre)

        if otp_result == "OK":
            paypay_data = load_paypay_data()
            user_id_str = str(interaction.user.id)
            
            paypay_data[user_id_str] = {
                "phone": self.phone,
                "password": self.password,
                "uuid": self.set_uuid
            }
            save_paypay_data(paypay_data)
            
            embed = discord.Embed(title="✅ 登録完了", description="PayPayアカウントを正常に登録しました。", color=discord.Color.green())
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            embed = discord.Embed(title="❌ 認証エラー", description="認証コードが正しくないか、期限が切れています。", color=discord.Color.red())
            await interaction.followup.send(embed=embed, ephemeral=True)

class PaypayCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if not os.path.exists(PAYPAY_DATA_FILE):
            save_paypay_data({})

    @app_commands.command(name="paypay登録", description="PayPayアカウントを登録します")
    @is_allowed()
    @app_commands.describe(phone="電話番号（090...）", password="パスワード")
    async def paypay_register(self, interaction: discord.Interaction, phone: str, password: str):
        # 内部的なUUIDを生成
        set_uuid = str(uuid.uuid4())
        
        # ログイン試行
        result = await paypayu.login(phone, password, set_uuid)
        
        # デバッグログ（コンソールで原因を確認するため）
        print(f"--- PayPay Login Response ---")
        print(result)
        print(f"-----------------------------")

        # 1. 明示的なエラーレスポンスの場合
        if result.get("response_type") == "ErrorResponse":
            error_code = result.get("error_code", "")
            reason = "電話番号またはパスワードが間違っています。"
            
            if "TOO_MANY_REQUESTS" in error_code:
                reason = "短時間に何度も試行したためロックされています。時間を置いて試してください。"
            elif "UNAUTHORIZED_CLIENT" in error_code:
                reason = "この環境からのログインは許可されていません（IPブロック等）。"

            embed = discord.Embed(title="PayPayログインエラー", description=reason, color=0xff3333)
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        # 2. 正常にOTPが必要な場合
        if "otp_reference_id" in result:
            otpid = result["otp_reference_id"]
            otp_pre = result.get("otp_prefix", "")
            modal = PayPayModal(phone, password, set_uuid, otpid, otp_pre)
            await interaction.response.send_modal(modal)
            
        # 3. OTPなしでログイン成功（アクセストークンが直接返ってきた場合）
        elif "access_token" in result:
            paypay_data = load_paypay_data()
            paypay_data[str(interaction.user.id)] = {
                "phone": phone,
                "password": password,
                "uuid": set_uuid
            }
            save_paypay_data(paypay_data)
            embed = discord.Embed(title="✅ 登録完了", description="ログインに成功しました（認証コード不要）。", color=discord.Color.green())
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        # 4. それ以外の不明な応答
        else:
            embed = discord.Embed(title="エラー", description="PayPayから予期しない応答がありました。しばらく待ってから再度お試しください。", color=discord.Color.orange())
            await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(PaypayCog(bot))