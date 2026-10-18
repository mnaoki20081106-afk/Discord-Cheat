"""ツムツム代行のスラッシュコマンド(元の games/tsum の bot.py をCog化したもの)。"""
import os

import discord
from discord import app_commands
from discord.ext import commands

import mod_stum

from . import panels


def _int_env(name: str) -> int:
    try:
        return int(os.getenv(name, "0") or "0")
    except ValueError:
        return 0


class TsumCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ツムツム代行", description="無料のツムツム代行パネルを表示します")
    async def tsum_panel(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=panels.panel_embed("tsum"), view=mod_stum.TsumPanelView())

    @app_commands.command(name="ツムツム無料30万", description="1人1回の30万コイン無料メニューを表示します")
    async def tsum_trial_panel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎁 30万コイン無料メニュー",
            description=("1人1回まで利用できます。\n"
                         "ボタンを押してLINEログイン後、自動で30万コイン処理を実行します。"),
            color=0x06C755,
        )
        await interaction.response.send_message(embed=embed, view=mod_stum.TsumTrialPanelView())

    @app_commands.command(name="ツムツム状態", description="Bot設定と登録コマンドの状態を確認します")
    async def tsum_status(self, interaction: discord.Interaction):
        owner = _int_env("OWNER_ID")
        if owner and interaction.user.id != owner:
            return await interaction.response.send_message("❌ OWNER専用です。", ephemeral=True)
        await interaction.response.send_message(
            f"Bot: `{self.bot.user}`\n"
            f"LOG_CHANNEL_ID: `{_int_env('LOG_CHANNEL_ID')}`\n"
            f"メニュー数: `{len(mod_stum.MENUS)}`",
            ephemeral=True)


async def setup(bot: commands.Bot):
    bot.add_view(mod_stum.TsumPanelView())
    bot.add_view(mod_stum.TsumTrialPanelView())
    await bot.add_cog(TsumCommands(bot))
