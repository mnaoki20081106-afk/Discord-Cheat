import discord
from discord import app_commands
import json
import os

CONFIG_FILE = "config.json"

def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"log_channel_id": None, "allowed_user_ids": []}

def save_config(data):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def load_items():
    try:
        with open("Items.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {
            "menu1": [
                {"name": "XP9999999", "price": 30},
                {"name": "NP9999", "price": 30},
                {"name": "猫缶50000", "price": 30},
                {"name": "バトルアイテム全種999", "price": 30},
                {"name": "ネコビタン全種999", "price": 30},
                {"name": "城塞材全種999", "price": 30},
                {"name": "キャッツアイ全999", "price": 30},
                {"name": "マタタビ全種99", "price": 30},
                {"name": "木箱全種09", "price": 150},
                {"name": "にゃんこチケ&レアチケ999", "price": 30},
                {"name": "プラチナ29", "price": 30},
                {"name": "レジェチケ", "price": 30},
                {"name": "イベチケ&福チケ999", "price": 30},
                {"name": "リーダーシップ", "price": 30},
                {"name": "地底迷宮メダル全種999", "price": 30},
            ],
            "menu2": [
                {"name": "全キャラ開放", "price": 30},
                {"name": "エラーキャラ削除", "price": 30},
                {"name": "全キャラLvMAX", "price": 30},
                {"name": "全キャラ最高形態", "price": 30},
                {"name": "全キャラ本能全開放", "price": 50},
                {"name": "メインステージ全クリア", "price": 30},
                {"name": "本能全開放", "price": 30},
                {"name": "IDレジェンドをクリア", "price": 50},
                {"name": "貴レジェンドをクリア", "price": 50},
                {"name": "メインゾンビステージ全クリア", "price": 30},
                {"name": "IDレジェンド全クリア", "price": 30},
                {"name": "真レジェンド全クリア", "price": 50},
                {"name": "ガマトトLvMAX", "price": 30},
                {"name": "ガマトト初手全レジェンド化", "price": 30},
                {"name": "にゃんこ神社LvMAX", "price": 30},
                {"name": "にゃんこ神社全開放", "price": 30},
                {"name": "プレイ時間カウスト", "price": 30},
                {"name": "編成スロット救急拡張", "price": 30},
                {"name": "ユーザーランク補助受取", "price": 30},
                {"name": "金お宝", "price": 50},
                {"name": "オートセーブ全解放LvMAX", "price": 30},
                {"name": "ゴールド会員化", "price": 30},
                {"name": "ガマトト助手追加", "price": 30},
            ]
        }

def save_items(data):
    with open("Items.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def is_allowed():
    async def predicate(interaction: discord.Interaction) -> bool:
        if await interaction.client.is_owner(interaction.user):
            return True
        
        config = load_config()
        allowed_ids = config.get("allowed_user_ids", [])
        if interaction.user.id not in allowed_ids:
            await interaction.response.send_message("🚫 あなたはこのBotの機能を利用する権限がありません。", ephemeral=True)
            return False
        
        return True
    return app_commands.check(predicate)

def load_orders():
    try:
        with open("orders.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_orders(data):
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

async def setup(bot):
    pass