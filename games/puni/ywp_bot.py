#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ぷにぷに 自動周回 Discord Bot

パネルのボタン → モーダル（メール/パスワード/ステージID/周回数）→ 裏で周回 → DMで通知。

起動:
    python bot.py
トークン:
    環境変数 DISCORD_TOKEN、または同フォルダの config.json の {"token": "..."}
"""

import asyncio
import collections
import io
import json
import math
import os
import random
import re
import string
import time
import traceback
import unicodedata
from datetime import datetime, timedelta

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

try:
    from aiohttp_socks import ProxyConnector    # プロキシ回し用のaiohttpコネクタ（SOCKS対応）
except ImportError:                             # 未設定なら無くても動く
    ProxyConnector = None

import ywp_auto
from ywp_auto import (BATTLE_TIME_MIN, BATTLE_TIME_SPREAD, COOLDOWN_MIN, MAX_CLEAR_TIME,
                      MIN_BATTLE_CYCLE, RANK_NAMES, RC, TIME_SPREAD, Client, create_save,
                      jitter, link_email, log_line, log_tag, login_email_prepared,
                      current_event_stages, is_trusted, rows, save_udkey_for, server_elapsed,
                      setup_log,
                      trust_set_manual, trust_wins, vip_gacha_status, VipUnavailable)
from ywp_master import cached_stage_ids, fetch_all_stage_ids
import puni_pay
import proxy_pool

# ============================================================================
# 設定
# ============================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

DEFAULT_BATTLE_TIME = BATTLE_TIME_MIN   # バトル時間の既定値（秒）
DEFAULT_COOLDOWN = 3.0                  # クールダウンの既定値（秒）
REQUEST_DELAY = 0.5                     # リクエスト前待機（秒・±30%）

WALLET_REFRESH_EVERY = 20       # 所持数を取り直す間隔（周）。1回につき再ログインが走る
WALLET_REFRESH_NEAR = 3         # 目標まで残りこの周数を切ったら毎周確認する
MAX_CONCURRENT_JOBS = 3         # 全体で同時に走らせるジョブ数の上限（待ちの分は別）
MAX_JOBS_PER_USER = 5           # 1ユーザーで同時に投げられるジョブ数（複垢代行用）
PROGRESS_INTERVAL = 6.0         # DM進捗メッセージの最短更新間隔（秒）
REFRESH_WAIT = 45.0             # 「🔄 更新」が所持数の取り直しを待つ上限（秒）
FATAL_CODES = (202, 30)         # これが出たら周回を打ち切る
TOKEN_CODES = (32,)             # tokenズレ。再ログインで復帰を試みる
TOKEN_RETRY = 3                 # rc=32で再ログインを試みる回数（超えたら中止）
ERROR_RETRY = 3                 # 連続エラー時の再試行回数。これを超えたらジョブ中止
HITODAMA_LOW = 5                # ひとだま合計がこの数以下になったら自動購入を試みる
HITODAMA_BUY = 500              # ひとだま補充の目標個数（50個パックを繰り返し買う）
HITODAMA_RICH = ywp_auto.HITODAMA_RICH        # 大きいパックを買うYマネーの下限
# ひとだまがこの数以下のあいだは、レベル上限の解放よりひとだまを優先する
# （解放しても HITODAMA_RICH 以上のYマネーが残るときだけ解放する）
LEVEL_HITODAMA_GUARD = 50
HITODAMA_POOR_BUY = ywp_auto.HITODAMA_POOR_BUY  # 余裕がないときの1回あたり補充上限

COLOR_INFO = 0x5865F2
COLOR_RUN = 0xFEE75C
COLOR_OK = 0x57F287
COLOR_WARN = 0xFAA61A
COLOR_NG = 0xED4245

# Botのオーナー。サーバーの権限に関係なく管理系の操作（パネル設置・設定変更）ができる
# 自分のDiscordユーザーIDを入れる（例: 123456789012345678,）。空でもサーバー管理権限があれば操作できる
OWNER_IDS = frozenset({
})
ADMIN_ONLY_MSG = '❌ この操作にはサーバー管理権限（またはBotオーナー）が必要です。'


def is_admin(user) -> bool:
    """Botオーナー、またはサーバー管理権限（manage_guild）を持つ人"""
    if getattr(user, 'id', None) in OWNER_IDS:
        return True
    perms = getattr(user, 'guild_permissions', None)
    return bool(perms is not None and perms.manage_guild)

SESSION_TTL = 6 * 3600          # ログイン情報(udkey)をメモリに保持する時間（秒）
STAGE_PAGE_LIMIT = 6            # ステージ一覧の最大メッセージ数
SAVE_SELECT_TIMEOUT = 180       # セーブデータが複数あるとき選択を待つ秒数

# user_id -> [FarmJob]（複垢代行で1人が複数ジョブを並走させる）
JOBS: dict[int, "list[FarmJob]"] = {}
# user_id -> {'udkey','userId','name','at'}  ※ディスクには書かない。再起動で消える
SESSIONS: dict[int, dict] = {}
SEM = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

# --- アカウント自動作成（旧 main.py 由来） ---
L5_REG_URL = 'https://auth.level5-id.com/user_registration?locale=ja'
L5_VERIFY_SUBJECT = '［LEVEL5 ID］アカウント新規登録用URL送付のお知らせ'
GM_API_URL = 'https://api.guerrillamail.com/ajax.php'

PASSWORD_LEN = 10                # 自動生成パスワードの文字数
MAIL_WAIT_TIMEOUT = 120.0        # 登録メールを待つ最大秒数
MAIL_POLL_INTERVAL = 5.0         # メールボックスの再確認間隔（秒）
MAX_CONCURRENT_ACCOUNTS = 2      # 同時に作成できるアカウント数の上限
MAX_ACCOUNTS_PER_RUN = 999999   # 1回の操作で指定できる作成数の上限
ACCOUNT_TXT_THRESHOLD = 10       # この件数以上まとめて作ったら .txt にしてDM送信
PLAYER_NAME_MAX = 16             # アカウント名の最大文字数（長すぎるとサーバーが弾く）

# --- 久垢作成（アカウント作成＋セーブ作成＋初回ステージクリアまでやる） ---
AGED_STAGE_ID = 1001001          # 久垢作成でクリアさせるステージ
AGED_CLEAR_RETRY = 3             # ステージクリアの再試行回数
AGED_RETRY_WAIT = 4.0            # クリア再試行の間隔（秒）

# --- 回し放題垢作成（作成＋第1章16面クリア＋まわし放題ガシャ） ---
VIP_GACHA_COUNT = 240            # 既定の連数。サーバーの上限（limitTurnCnt）を超える指定は上限にする
VIP_STAGE_GAP = (2.0, 5.0)       # 面と面のあいだの待ち（秒）
VIP_STAGE_COUNT = 6              # 回し放題垢で先にクリアする面数（イベステ解放の16面の先頭から）
VIP_PRICE = 0                    # 有料版の1垢の料金（円）。0 は未設定で、有料版は受け付けない
VIP_PAID_MAX = 10                # 有料版で1回に注文できる垢の数
VIP_EXIST_PRICE = 0              # 有料版「既存の垢に240連」の料金（円/1垢）。0 は未設定で受け付けない
VIP_PAID_RETRY = 2               # 有料版で失敗した垢を作り直す回数（最低。注文が多いと半数まで）


async def lease_proxy():
    """プロキシプール（proxy.txt）から1枠借りる。満枠のときは空くまで待つ。

    proxy.txt が空なら None（=直接接続）。戻り値の lease.url を Client や
    prepare_login の proxy に渡し、使い終わったら lease.release() で返す
    （1プロキシ同時3セッションまで。周回・垢作成・回し放題など全操作で共通利用）
    """
    lease = await asyncio.to_thread(proxy_pool.acquire)
    if lease is not None:
        print(f'[proxy] 貸出 {proxy_pool.mask(lease.url)}')
    return lease

# 使用済みの取引番号。同じPayPay口座（PAY管理）で受け取るほかのBOT（自動代行・AutoTsum）と
# 同じファイルを使い、別のBOTで使った取引番号を通さない。config.json の used_orders_file で変えられる
_SHARED_USED_ORDERS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   '自動代行', 'SETTING', 'used_orders.json')


def _used_orders_path() -> str:
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            p = str(json.load(f).get('used_orders_file') or '').strip()
    except (OSError, ValueError, AttributeError):
        p = ''
    if p:
        return p if os.path.isabs(p) else os.path.join(BASE_DIR, p)
    if os.path.isdir(os.path.dirname(_SHARED_USED_ORDERS)):
        return _SHARED_USED_ORDERS
    return os.path.join(BASE_DIR, 'data', 'used_orders.json')


USED_ORDERS = puni_pay.UsedOrderStore(_used_orders_path())

# --- 自動編成（このステージ1つだけを周回するときだけ使える） ---
# 今のイベントの最深部（種別 20 の面）。refresh_event_preset がイベントに合わせて書き換える
AUTO_DECK_STAGE = 29704006       # 【裏】ステージ51（ボス）。ぷにぷに百物語 YOG48 VS HOG48（9/30 まで）
AUTO_DECK_ASK_TIMEOUT = 120      # 「有効にしますか？」の返事待ち（秒）。過ぎたら無効で開始
# 周回開始時と仲間が増えたときに、デッキを攻撃力の高い順に組み直す（1=する / 0=しない）。
# rc=4 はデッキの攻撃力に対して速すぎるクリアで出るので、強いデッキほど安全に速く回せる
STRONG_DECK = 1
# ただし今のデッキに、このランク以上の妖怪が STRONG_DECK_KEEP_COUNT 体以上いれば組み直さない
# （0 体にすると、この判定はしない）。ランクは RANK_NAMES の番号（10=ZZ）
STRONG_DECK_KEEP_RANK = 10
STRONG_DECK_KEEP_COUNT = 3
# 硬い面（まだ育っていない垢だと、弾かれない速さで1戦 MAX_CLEAR_TIME 秒超）に当たったら、
# 1回だけ安全にクリアしてから GRIND_BACK 面前を安全な速さで回し、TRUST_BATTLES 勝に届いたら
# （育った垢になったら）速く戻る（start_grind）
GRIND_BACK = 2
TRUST_BATTLES = ywp_auto.TRUST_BATTLES        # この勝ち数で「育った垢」（速さの制限なし）
# 好物の使い方（周回ごとに選ぶ。job.food_mode）
FOOD_MODES = {'off': '使わない', 'new': 'まだ持っていない妖怪だけ', 'all': '持っていても使う（必殺技Lv上げ）'}
# ガチャ: この面を初めてクリアしたときに、持っているコインを全部使って引く
GACHA_STAGE = 1001015

ACCOUNT_SEM = asyncio.Semaphore(MAX_CONCURRENT_ACCOUNTS)
ACCOUNT_RUNNING: set[int] = set()   # アカウント作成実行中のユーザーID

# LEVEL5 ID 登録フォームへ送るブラウザ風ヘッダー（旧 main.py から）
BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) '
                  'Gecko/20100101 Firefox/126.0',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,'
              'image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.7,en;q=0.3',
    'Content-Type': 'application/x-www-form-urlencoded',
    'Sec-GPC': '1',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'same-origin',
    'Sec-Fetch-User': '?1',
    'Priority': 'u=1',
}


# ============================================================================
# 可変設定（/管理パネル から編集 → settings.json に保存して再起動後も残る）
# ============================================================================
# ここに載せた項目は稼働中でも書き換えられる。実体はこのモジュールの変数を
# 差し替えるだけなので、参照側（関数の中や f-string）は今までどおり定数名で
# 読める。※関数のデフォルト引数やクラス定義時に評価される箇所だけは、
# 呼び出し時に読み直すよう書き換えてある。
SETTINGS_FILE = os.path.join(BASE_DIR, 'settings.json')

def parse_rank(raw) -> int:
    """ランクの入力（'ZZ'・'uz+'・'ＺＺ'・'10' など）を番号にする"""
    s = unicodedata.normalize('NFKC', str(raw)).strip().upper()
    if s.isdigit():
        return int(s)
    for code, name in RANK_NAMES.items():
        if s == name:
            return code
    raise ValueError(s)


def rank_label(code) -> str:
    return f'{RANK_NAMES.get(code, "?")}（{code}）'


SETTING_DEFS: dict[str, dict] = {
    'LEVEL_HITODAMA_GUARD': {
        'label': 'ひとだま優先: この数以下ならレベル上限解放を控える', 'type': int, 'min': 0, 'max': 100000,
        'help': 'ひとだまがこの数以下のときは、上限解放のあとも大パック代（HITODAMA_RICH）が残る場合だけ解放',
    },
    'HITODAMA_LOW': {
        'label': 'ひとだま自動購入のしきい値', 'type': int, 'min': 0, 'max': 100000,
        'help': 'ひとだま合計がこの数「以下」になったらYマネーで自動購入します。'
                '0にすると残り0個になるまで買いません',
    },
    'HITODAMA_BUY': {
        'label': 'ひとだま補充の目標個数', 'type': int, 'min': 1, 'max': 100000,
        'help': '自動購入で何個まで買い足すか',
    },
    'HITODAMA_RICH': {
        'label': '大きいパックを買うYマネーの下限', 'type': int, 'min': 0, 'max': 100000000,
        'help': '所持Yマネーがこれ以上なら単価の良い50個パック(6,000)を買う。'
                '下回るあいだは5個パック(1,000)だけにして財布を空にしない',
    },
    'HITODAMA_POOR_BUY': {
        'label': 'Yマネーが少ないときの補充上限（個）', 'type': int, 'min': 1, 'max': 1000,
        'help': '上の下限を下回っているときに1回で買い足す上限。'
                '数戦ぶんだけ買ってつなぎ、残りは周回で貯める',
    },
    'WALLET_REFRESH_EVERY': {
        'label': '所持数を取り直す間隔（周）', 'type': int, 'min': 1, 'max': 1000,
        'help': '小さくすると所持数が正確になりますが、1回につき再ログインが走ります',
    },
    'MAX_JOBS_PER_USER': {
        'label': '1人あたりの同時ジョブ数', 'type': int, 'min': 1, 'max': 20,
        'help': '複垢代行用。全体の同時稼働数はコード側の固定値です',
    },
    'ERROR_RETRY': {
        'label': 'エラー時の再試行回数', 'type': int, 'min': 0, 'max': 20,
        'help': '連続エラーがこの回数を超えたらジョブを中止します',
    },
    'TOKEN_RETRY': {
        'label': 'tokenズレ(rc=32)の再ログイン回数', 'type': int, 'min': 0, 'max': 10,
        'help': 'この回数まで再ログインで復帰を試みます',
    },
    'REFRESH_WAIT': {
        'label': '「🔄更新」の待ち上限（秒）', 'type': float, 'min': 5, 'max': 300,
        'help': '所持数の取り直しをこの秒数まで待ちます',
    },
    'PROGRESS_INTERVAL': {
        'label': 'DM進捗の更新間隔（秒）', 'type': float, 'min': 2, 'max': 120,
        'help': '短くすると細かく更新されますがレート制限に当たりやすくなります',
    },
    'DEFAULT_COOLDOWN': {
        'label': 'クールダウンの既定値（秒）', 'type': float,
        'min': COOLDOWN_MIN, 'max': 600,
        'help': f'バトル終了→次バトルの待ち。{COOLDOWN_MIN:g}秒未満にはできません',
    },
    'ACCOUNT_TXT_THRESHOLD': {
        'label': 'アカウント作成をtxtにする件数', 'type': int, 'min': 1, 'max': 10000,
        'help': 'この件数以上まとめて作ると .txt にしてDMします',
    },
    'AGED_STAGE_ID': {
        'label': '久垢作成でクリアするステージID', 'type': int, 'min': 1, 'max': 99999999,
        'help': '久垢作成で最初にクリアさせるステージ',
    },
    'AGED_CLEAR_RETRY': {
        'label': '久垢のステージクリア再試行回数', 'type': int, 'min': 1, 'max': 10,
        'help': 'クリアに失敗したとき何回まで挑み直すか',
    },
    'AUTO_DECK_STAGE': {
        'label': '自動編成を使えるステージID', 'type': int, 'min': 1, 'max': 99999999,
        'help': 'このステージだけを周回するときに「ドロップと自動編成」を聞く',
    },
    'STRONG_DECK': {
        'label': 'デッキを攻撃力の高い順に組み直す（1=する / 0=しない）', 'type': int,
        'min': 0, 'max': 1,
        'help': '周回開始時と仲間が増えたとき。自動編成中はリーダーをそのままにして残り4体を強い順',
    },
    'TRUST_BATTLES': {
        'label': '育った垢とみなす勝ち数', 'type': int, 'min': 100, 'max': 100000,
        'help': 'この回数勝った垢は速さの制限なしで回す。それまでは弾かれない安全な速さ（30まんは約130勝で解禁）',
    },
    'GRIND_BACK': {
        'label': '硬い面で育てるときに戻る面の数', 'type': int, 'min': 1, 'max': 10,
        'help': '硬い面の何面前を回して垢を育てるか（2 = 2つ前）',
    },
    'STRONG_DECK_KEEP_RANK': {
        'label': '組み直さない条件: このランク以上', 'type': parse_rank, 'min': 1, 'max': 14,
        'fmt': rank_label, 'kind': 'ランク名（Z / ZZ / ZZZ / UZ / UZ+ / UZ++ など）か番号',
        'help': 'ZZ・ZZZ・UZ+ などで入力。このランク以上が下の体数以上デッキにいれば組み直さない',
    },
    'STRONG_DECK_KEEP_COUNT': {
        'label': '組み直さない条件: 何体以上', 'type': int, 'min': 0, 'max': 5,
        'help': 'デッキに上のランク以上がこの体数以上いれば組み直さない。0 で判定しない（毎回組み直す）',
    },
    'VIP_PRICE': {
        'label': '回し放題垢（有料版）の1垢の料金（円）', 'type': int, 'min': 0, 'max': 100000,
        'help': 'PayPayで受け取る1垢あたりの料金。0 にすると有料版の受付を止める',
    },
    'VIP_PAID_MAX': {
        'label': '回し放題垢（有料版）の1回の注文で作れる垢の数', 'type': int, 'min': 1, 'max': 100,
        'help': '1回の支払いで注文できる垢の数の上限',
    },
    'VIP_STAGE_COUNT': {
        'label': '回し放題垢で先にクリアする面数', 'type': int, 'min': 1, 'max': 16,
        'help': '第1章の先頭から何面クリアしてからガチャを引くか（16 で 1002001 まで＝イベステ解放）',
    },
    'VIP_EXIST_PRICE': {
        'label': '既存の垢に240連（有料版）の料金（円）', 'type': int, 'min': 0, 'max': 100000,
        'help': 'お客様の垢で まわし放題ガシャを引く代行の料金。0 にすると受付を止める',
    },
}

# 既定値は「起動時にコードへ書かれていた値」。初期化ボタンでここへ戻す
SETTING_DEFAULTS: dict[str, object] = {k: globals()[k] for k in SETTING_DEFS}


def setting_show(key: str, value=None) -> str:
    """設定値の表示（ランクは名前つき）"""
    v = setting_value(key) if value is None else value
    fmt = SETTING_DEFS[key].get('fmt')
    return fmt(v) if fmt else str(v)


def setting_value(key: str):
    return globals()[key]


def coerce_setting(key: str, raw) -> object:
    """入力文字列を設定値に変換する。範囲外は ValueError"""
    d = SETTING_DEFS[key]
    try:
        v = d['type'](str(raw).strip().replace(',', ''))
    except (TypeError, ValueError):
        kind = d.get('kind') or ('整数' if d['type'] is int else '数値')
        raise ValueError(f'{kind}で入力してください')
    lo, hi = d.get('min'), d.get('max')
    fmt = d.get('fmt') or (lambda x: f'{x:g}')
    if lo is not None and v < lo:
        raise ValueError(f'{fmt(lo)} 以上にしてください')
    if hi is not None and v > hi:
        raise ValueError(f'{fmt(hi)} 以下にしてください')
    return v


def apply_setting(key: str, value):
    globals()[key] = value
    # ywp_auto 側にも実体があるものは向こうにも反映する
    if key in YWP_AUTO_SETTINGS:
        setattr(ywp_auto, key, value)
    # 有料版パネルに出している料金・上限を、設置済みのパネルにも反映する
    if key in ('VIP_PRICE', 'VIP_PAID_MAX', 'VIP_EXIST_PRICE', 'VIP_STAGE_COUNT'):
        try:
            asyncio.get_running_loop().create_task(refresh_paid_panels())
        except RuntimeError:
            pass        # 起動前（settings.json の読み込み）


# bot.py と ywp_auto.py の両方に同名の変数があり、実際に読まれるのは
# ywp_auto 側のもの。/管理パネル で変えたときに取り残されないよう列挙する。
YWP_AUTO_SETTINGS = ('HITODAMA_RICH', 'HITODAMA_POOR_BUY', 'TRUST_BATTLES')


def load_settings():
    """settings.json があれば読み込んで反映する（壊れていても起動は止めない）"""
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f'[settings] 読み込みに失敗（既定値で起動します）: {e}')
        return
    if not isinstance(data, dict):
        return
    for k, v in data.items():
        if k not in SETTING_DEFS:
            continue
        try:
            apply_setting(k, coerce_setting(k, v))
        except ValueError as e:
            print(f'[settings] {k}={v!r} は不正なので既定値のまま: {e}')


def save_settings() -> bool:
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump({k: setting_value(k) for k in SETTING_DEFS}, f,
                      ensure_ascii=False, indent=2)
        return True
    except OSError as e:
        print(f'[settings] 保存に失敗: {e}')
        return False


load_settings()


# --- アカウント作成の稼働停止／再開（管理パネルから） ---
# 停止中は新しい作成を受け付けず、実行中の作成は作り途中の1件を終えたところで
# 打ち切って、それまでに作れたぶんを通常どおりDM（txt）で渡す。
# Botを再起動する前に止めておけば、作成途中で落ちてパスワードを失うことがない。
# 再起動しても止めたままにするためファイルに残す。
ACCOUNT_PAUSE_FILE = os.path.join(BASE_DIR, 'account_pause.json')
ACCOUNT_PAUSE: dict = {'paused': False, 'by': None, 'at': None}


def load_account_pause():
    try:
        with open(ACCOUNT_PAUSE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            ACCOUNT_PAUSE.update({k: data.get(k) for k in ACCOUNT_PAUSE})
            ACCOUNT_PAUSE['paused'] = bool(ACCOUNT_PAUSE['paused'])
    except (OSError, ValueError):
        pass


# --- メンテ停止（maintenance.json）。周回も垢作成も止める ---
# 例: [{"start": "2026-09-30 13:00", "end": null}]。end が null のあいだは止めたまま（自動では明けない。
# メンテ明けにいきなり回して垢が死ぬのを避けるため）。管理パネルの「メンテ停止を解除」で end が入る。
# 30秒ごとに読み直すので、手で書き換えてもよい
MAINT_FILE = os.path.join(BASE_DIR, 'maintenance.json')
MAINT_LEAD = 300        # 開始の何秒前から新しいバトル・垢作成を始めない（開始時刻に1戦も動かさない）


def _maint_items() -> list:
    try:
        with open(MAINT_FILE, encoding='utf-8') as f:
            items = json.load(f)
        return items if isinstance(items, list) else []
    except (OSError, ValueError):
        return []


def _maint_save(items: list) -> bool:
    try:
        with open(MAINT_FILE, 'w', encoding='utf-8') as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def maint_window(lead: int = MAINT_LEAD):
    """今（lead 秒の前倒し込み）がメンテ停止中なら (開始, 終了 or None)、でなければ None"""
    now = datetime.now()
    for it in _maint_items():
        try:
            st = datetime.strptime(it['start'], '%Y-%m-%d %H:%M')
            en = datetime.strptime(it['end'], '%Y-%m-%d %H:%M') if it.get('end') else None
        except (KeyError, TypeError, ValueError):
            continue
        if st - timedelta(seconds=lead) <= now and (en is None or now < en):
            return st, en
    return None


def maint_text(w) -> str:
    st, en = w
    return f'{st:%m/%d %H:%M}〜' + (f'{en:%H:%M}' if en else '（管理者が解除するまで）')


def maint_start_now(user) -> bool:
    """今すぐメンテ停止にする（解除されるまで続く）"""
    items = _maint_items()
    items.append({'start': datetime.now().strftime('%Y-%m-%d %H:%M'), 'end': None, 'by': str(user)})
    log_line(f'メンテ停止を開始: by {user}')
    return _maint_save(items)


def maint_release(user) -> bool:
    """止めているメンテ（これから始まる予定も含む）を全部解除する"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    items = _maint_items()
    for it in items:
        if not it.get('end') or it['end'] > now:
            it['end'] = now
            it['released_by'] = str(user)
    log_line(f'メンテ停止を解除: by {user}')
    return _maint_save(items)


async def wait_maintenance(job) -> bool:
    """メンテの時間帯なら終わるまで待つ（通信しない）。中止されたら True"""
    w = maint_window()
    if not w:
        return False
    prev = job.status
    job.status = f'🛠 メンテのため停止中（{maint_text(w)}）'
    log_line(f'メンテのため停止 #{job.jid}: {maint_text(w)}')
    try:
        await job.user.send(f'🛠 **#{job.jid}** メンテ（{maint_text(w)}）のため周回を止めました。'
                            '管理者が「メンテ停止を解除」すると、止まったところから再開します。')
    except discord.HTTPException:
        pass
    await update_progress(job, force=True)
    while maint_window() and not job.cancel.is_set():
        await asyncio.sleep(30)
    if job.cancel.is_set():
        return True
    log_line(f'メンテ停止の解除 周回を再開 #{job.jid}')
    job.status = prev if prev and not prev.startswith('🛠') else '⚔ 周回中'
    try:
        await job.user.send(f'▶ **#{job.jid}** メンテ停止が解除されたので、周回を再開します。')
    except discord.HTTPException:
        pass
    return False


def set_account_pause(paused: bool, user) -> bool:
    """停止／再開を切り替えて保存する。保存に失敗しても切り替え自体は効かせる"""
    ACCOUNT_PAUSE.update(paused=paused, by=f'{user} ({getattr(user, "id", "?")})',
                         at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    log_line(f'アカウント作成を{"停止" if paused else "再開"}: by {ACCOUNT_PAUSE["by"]}')
    try:
        with open(ACCOUNT_PAUSE_FILE, 'w', encoding='utf-8') as f:
            json.dump(ACCOUNT_PAUSE, f, ensure_ascii=False, indent=2)
        return True
    except OSError as e:
        print(f'[account] 停止状態の保存に失敗: {e}')
        return False


def account_pause_text() -> str:
    if not ACCOUNT_PAUSE['paused']:
        return '▶ 受付中'
    return f'⏸ **停止中**（{ACCOUNT_PAUSE["at"]}〜）'


load_account_pause()


def resolve_token() -> str | None:
    tok = (os.environ.get('DISCORD_TOKEN') or '').strip()
    if tok:
        return tok
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            tok = (cfg.get('token') or '').strip()
            if tok:
                return tok
        except (json.JSONDecodeError, OSError) as e:
            print(f'[token] config.json の読み込みに失敗: {e}')
    return None


# ============================================================================
# セッション（メモリのみ）
# ============================================================================

def remember_session(user_id: int, c: Client, name: str | None = None):
    """周回/一覧のログイン成功時にudkeyを控えておき、次回パス入力を省く"""
    try:
        wallet = c.wallet_text()
    except (ValueError, AttributeError):
        wallet = ''
    SESSIONS[user_id] = {'udkey': c.udkey, 'userId': c.userId, 'name': name,
                         'wallet': wallet, 'at': time.time()}


async def acquire_client(user_id: int) -> Client | None:
    """保持済みudkeyで再ログイン。無効/期限切れならNone（=パス再入力が必要）

    プロキシを1枠借りて（proxy.txt）Clientに結び付けて返す。呼び出し側は
    使い終わったら proxy_pool.release_client(c) で枠を返すこと。
    """
    s = SESSIONS.get(user_id)
    if not s or time.time() - s['at'] > SESSION_TTL:
        SESSIONS.pop(user_id, None)
        return None
    lease = await lease_proxy()
    c = Client(s['udkey'], proxy=lease.url if lease is not None else None)
    if lease is not None:
        proxy_pool.bind(c, lease)
    try:
        await asyncio.to_thread(c.login, s['userId'])
    except Exception as e:
        proxy_pool.release_client(c)
        print(f'[session] 再ログイン失敗 user={user_id}: {str(e)[:120]}')
        SESSIONS.pop(user_id, None)
        return None
    s['at'] = time.time()
    return c


# ============================================================================
# ログイン（セーブデータ選択付き）
# ============================================================================

class SaveSelectView(discord.ui.View):
    """ログイン時にセーブデータが複数あった場合の選択プルダウン"""

    def __init__(self, accs: list, owner_id: int):
        super().__init__(timeout=SAVE_SELECT_TIMEOUT)
        self.owner_id = owner_id
        self.choice: str | None = None
        options = []
        for i, a in enumerate(accs[:25]):       # セレクトは1メニュー25個まで
            name = str(a.get('playerName') or 'プレイヤー名不明')
            desc = f"userId: {a.get('userId')}"
            if i == 0:
                desc += '（未指定時の既定）'
            options.append(discord.SelectOption(
                label=name[:100], value=str(a.get('userId')),
                description=desc[:100]))
        select = discord.ui.Select(
            placeholder='ログインするセーブデータを選択', options=options)
        select.callback = self._picked
        self.select = select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                'この選択メニューは別の人のログイン用です。', ephemeral=True)
            return False
        return True

    async def _picked(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.choice = self.select.values[0]
        self.stop()


def split_email_save(raw: str) -> tuple[str, str | None]:
    """「メール#セーブ指定」を (メール, セーブ指定) に分ける

    代行などでセーブが複数あるアカウントのどれで回すかを事前に指定する書き方。
    `a@b.c#ぷに4453`（プレイヤー名の部分一致）や `a@b.c#3007405804870`（userId）。
    # が無ければ (入力そのまま, None)。
    """
    s = (raw or '').strip().replace('＃', '#')
    if '#' in s:
        email, hint = s.split('#', 1)
        email, hint = email.strip(), hint.strip()
        if email:
            return email, (hint or None)
    return s, None


def _save_list_text(accs: list) -> str:
    return ', '.join(f'{a.get("playerName") or "名称不明"}(`{a.get("userId")}`)'
                     for a in accs[:10])


async def prepare_login(email: str, password: str, owner_id: int, send_prompt,
                        cancel_event: asyncio.Event | None = None,
                        save_hint: str | None = None,
                        proxy: str | None = None):
    """udkey発行→連携→セーブ一覧→（複数なら選択）まで。 (Client, userId, セーブ一覧) を返す

    userId が None ならセーブが1つ（login時に自動で先頭が選ばれる）。
    save_hint（メール#セーブ指定）があるときは一致するセーブを自動選ぶ。
    proxy 指定時（プロキシプールから借りた出口）、udkey発行・連携・ログインの通信が
    その出口を通る。
    ここでは login.nhn をまだ叩かない。実際のログインは呼び出し側で
    c.login(userId) として行う（周回は同時実行枠を取ってからログインする）。
    send_prompt(view, accs) は選択プルダウンを載せたメッセージを送って
    そのメッセージを返すasync関数。
    """
    c, accs = await asyncio.to_thread(login_email_prepared, email, password, proxy)
    # userId が採番されていない gdkey（セーブの器だけ作って中身が無いもの）は
    # ログインしてもゲームデータが降らないため、選択対象から除外する
    accs = [a for a in accs if a.get('userId')]
    if not accs:
        raise RuntimeError(
            'このアカウントにはセーブデータがありません。\n'
            '「🆕 アカウント作成」で作成すれば、最初から周回可能なセーブ付きになります。')

    if save_hint:
        # userId完全一致 → プレイヤー名の部分一致（大小は区別しない）の順で判定
        hint = save_hint.strip()
        hits = [a for a in accs if str(a.get('userId')) == hint]
        if not hits:
            h = hint.casefold()
            hits = [a for a in accs
                    if h in str(a.get('playerName') or '').casefold()]
        if not hits:
            raise RuntimeError(
                f'セーブ指定「{hint}」に一致するセーブデータがありません。\n'
                f'このアカウントのセーブ: {_save_list_text(accs)}')
        if len(hits) > 1:
            raise RuntimeError(
                f'セーブ指定「{hint}」が複数のセーブに一致します。'
                'userIdで指定してください。\n'
                f'一致したセーブ: {_save_list_text(hits)}')
        return c, str(hits[0].get('userId')), accs

    if len(accs) <= 1:
        return c, None, accs

    view = SaveSelectView(accs, owner_id)
    msg = await send_prompt(view, accs)
    waits = [asyncio.create_task(view.wait())]
    if cancel_event is not None:
        waits.append(asyncio.create_task(cancel_event.wait()))
    await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    for w in waits:
        w.cancel()
    view.stop()

    if view.choice is None:
        if cancel_event is not None and cancel_event.is_set():
            note = '⏹ 中止されたため、ログインを取りやめました。'
        else:
            note = (f'⏰ {SAVE_SELECT_TIMEOUT // 60}分以内に選択されなかったため、'
                    'ログインを取りやめました。')
        try:
            await msg.edit(content=note, view=None)
        except discord.HTTPException:
            pass
        raise RuntimeError(note)
    chosen = next((a for a in accs if str(a.get('userId')) == view.choice), {})
    label = chosen.get('playerName') or view.choice
    try:
        await msg.edit(content=f'✅ セーブデータ **{label}** でログインします',
                       view=None)
    except discord.HTTPException:
        pass
    return c, view.choice, accs


# ============================================================================
# ステージ一覧（旧 ywp_stages.py 由来）
# ============================================================================

# 分類ルール。上から順に判定され、最初に当たったものに入る（自由に編集可）
STAGE_CATEGORIES = [
    {'name': '📖 メインステージ',
     'match': lambda sid: sid.startswith(('1001', '1002', '1003'))},
    {'name': '📖 メイン（2章以降）',
     'match': lambda sid: sid.startswith('1901')},
    # 常設マップは 5001〜5053 と 5101〜5128（ywp_user_map 実測）
    {'name': '🎯 サブステージ',
     'match': lambda sid: len(sid) >= 7 and sid[:2] in ('50', '51')},
    {'name': '🎪 イベントステージ',
     'match': lambda sid: sid.startswith('9002')},
    {'name': '💪 強敵ステージ',
     'match': lambda sid: sid.startswith(('2880', '2890'))},
    {'name': '🌙 夜叉ステージ',
     'match': lambda sid: sid.startswith('2900')},
    {'name': '❓ その他',
     'match': lambda sid: True},
]


# ============================================================================
# イベント名／マップ名の解決
#   stageId = mapId * 1000 + 面数   （例: 29701005 = map 29701 の 5面）
#   マップ名とイベント名は login.nhn の応答に入っている ywp_mst_map から取る。
#   ただし ywp_mst_map は「その時点で開催中のマップ」しか返ってこないので、
#   一度見た分を data/map_names.json に貯めて回を跨いで使い回す。
# ============================================================================

MAP_NAME_FILE = os.path.join(BASE_DIR, 'data', 'map_names.json')
_MAP_CACHE: "dict[int, dict] | None" = None
UNKNOWN_EVENT = '❓ イベント名なし'


def _load_map_cache() -> dict:
    global _MAP_CACHE
    if _MAP_CACHE is not None:
        return _MAP_CACHE
    try:
        with open(MAP_NAME_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        _MAP_CACHE = {int(k): v for k, v in raw.items()} if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        _MAP_CACHE = {}
    return _MAP_CACHE


def _save_map_cache():
    cache = _load_map_cache()
    if not cache:
        return
    os.makedirs(os.path.dirname(MAP_NAME_FILE), exist_ok=True)
    tmp = MAP_NAME_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({str(k): v for k, v in cache.items()}, f, ensure_ascii=False)
        os.replace(tmp, MAP_NAME_FILE)
    except OSError as e:
        log_line(f'[map] マップ名キャッシュの保存に失敗: {e}')


MST_MAP_RAW_FILE = os.path.join(BASE_DIR, 'data', 'mst_map_raw.txt')


def dump_mst_map_raw(raw):
    """ywp_mst_map が区切り文字列で来たときに生値を1回だけ落としておく。

    列の並びが分かれば parse_mst_map に dict 以外の経路を足せる。
    （推測でパースすると嘘の名前が一覧に出るので、今は採用していない）
    """
    if not raw or os.path.exists(MST_MAP_RAW_FILE):
        return
    try:
        os.makedirs(os.path.dirname(MST_MAP_RAW_FILE), exist_ok=True)
        with open(MST_MAP_RAW_FILE, 'w', encoding='utf-8') as f:
            f.write(str(raw))
        log_line(f'[map] ywp_mst_map が文字列で来たので生値を {MST_MAP_RAW_FILE} に保存しました')
    except OSError:
        pass


def parse_mst_map(save) -> dict:
    """login.nhn 応答の ywp_mst_map を {mapId: {...}} にする。

    dict のリストで来る場合と、行='*' 列='|' の文字列で来る場合の両方に備える。
    """
    out = {}
    raw = (save or {}).get('ywp_mst_map')
    if not raw:
        return out

    if isinstance(raw, str):
        # 区切り文字列で来た場合は列の並びが分からない。ここで推測すると
        # 出所の怪しい名前が一覧に載ってしまうので、あえて何も採用しない。
        # （生の値は下の dump_mst_map_raw() で確認できる）
        dump_mst_map_raw(raw)
        return out

    if isinstance(raw, dict) and 'tableData' in raw:
        dump_mst_map_raw(raw.get('tableData'))
        return out

    if isinstance(raw, dict):
        raw = raw.get('data') or raw.get('rows') or []
    for m in raw if isinstance(raw, list) else []:
        if not isinstance(m, dict):
            continue
        try:
            mid = int(m.get('mapId'))
        except (TypeError, ValueError):
            continue
        out[mid] = {
            'name': str(m.get('mapName') or '').strip(),
            # description にイベントのタイトルが入っている
            'event': str(m.get('description') or '').replace('\n', ' ').strip(),
            'eventId': int(m.get('eventId') or 0),
            'sort': int(m.get('sort') or 0),
            'mapType': int(m.get('mapType') or 0),
            'openDt': str(m.get('openDt') or ''),
            'closeDt': str(m.get('closeDt') or ''),
        }
    return out


def remember_maps(save) -> dict:
    """今回のログインで見えたマップをキャッシュに足して、全体を返す"""
    cache = _load_map_cache()
    fresh = parse_mst_map(save)
    if fresh:
        cache.update(fresh)
        _save_map_cache()
    return cache


EVENT_LABEL_MAX = 50


def event_labels(maps: dict) -> dict:
    """{eventId: イベント見出し} を作る。

    description には 29701〜29704 のように本物のイベントタイトルが入る場合と、
    とっくんマップのように単なる説明文が入る場合がある。
    同じ eventId に複数マップがぶら下がっているときだけ description を
    イベント名として採用し、単独マップならマップ名そのものを見出しにする。
    """
    by_event: dict = {}
    for mid, v in maps.items():
        eid = v.get('eventId') or 0
        if eid:
            by_event.setdefault(eid, []).append((mid, v))

    out = {}
    for eid, items in by_event.items():
        descs = [v['event'] for _, v in items if v.get('event')]
        if len(items) >= 2 and descs:
            # 最も多い description をイベント名とする
            label = max(set(descs), key=descs.count)
        else:
            label = items[0][1].get('name') or ''
        label = ' '.join(label.split())[:EVENT_LABEL_MAX]
        if label:
            out[eid] = label
    return out


def stage_parts(sid: int, maps: dict, labels: dict | None = None) -> tuple:
    """stageId を (グループキー, イベント見出し, マップ名, 面数, mapId) に分解する"""
    mid, no = divmod(int(sid), 1000)
    info = maps.get(mid) or {}
    name = info.get('name') or ''
    eid = info.get('eventId') or 0
    labels = event_labels(maps) if labels is None else labels
    label = labels.get(eid) or ' '.join((info.get('event') or '').split())[:EVENT_LABEL_MAX]
    if label:
        return ('e%d' % eid if eid else 'm%d' % mid), label, name, no, mid
    # マスタで名前が引けないものは従来のプレフィックス分類を見出しに使う
    cat = next((c['name'] for c in STAGE_CATEGORIES if c['match'](str(sid))), UNKNOWN_EVENT)
    return cat, cat, name, no, mid


FALLBACK_EVENT_NAMES = frozenset([c['name'] for c in STAGE_CATEGORIES] + [UNKNOWN_EVENT])


def is_named_event(event: str) -> bool:
    """マスタから本物のイベント名が引けたか（従来カテゴリ名の流用でないか）"""
    return event not in FALLBACK_EVENT_NAMES


def stage_matches(sid: int, kw: str, maps: dict, labels: dict | None = None) -> bool:
    """検索キーワードを ID / イベント名 / マップ名 のどれかに当てる"""
    if kw in str(sid):
        return True
    _, label, name, _, mid = stage_parts(sid, maps, labels)
    k = kw.casefold()
    return k in label.casefold() or k in name.casefold() or kw in str(mid)


def group_stages_by_event(stage_ids: list, maps: dict) -> list:
    """[(イベント見出し, 件数, [(マップ名, mapId, [(面数, stageId), ...]), ...]), ...]

    イベント名つき（= マスタで名前が引けた）を先に、残りを従来カテゴリ順で後ろに置く。
    """
    labels = event_labels(maps)
    tree: dict = {}
    for sid in stage_ids:
        key, label, name, no, mid = stage_parts(sid, maps, labels)
        node = tree.setdefault(key, {'label': label, 'maps': {}})
        node['maps'].setdefault((mid, name), []).append((no, sid))

    fallbacks = [c['name'] for c in STAGE_CATEGORIES] + [UNKNOWN_EVENT]

    def order(key):
        node = tree[key]
        # 名前つきイベントを優先。同格はマスタの sort（新しいイベントが先）→mapId
        rank = fallbacks.index(key) if key in fallbacks else -1
        mids = [mid for mid, _ in node['maps']]
        sort = max((maps.get(m, {}).get('sort') or 0) for m in mids) if mids else 0
        return (1 if rank >= 0 else 0, rank, -sort, min(mids) if mids else 0)

    out = []
    for key in sorted(tree, key=order):
        node = tree[key]
        groups = [(name, mid, sorted(items))
                  for (mid, name), items in sorted(node['maps'].items(),
                                                   key=lambda kv: kv[0][0])]
        out.append((node['label'], sum(len(g[2]) for g in groups), groups))
    return out


LOCK_MARK = '🔒'


def named_event_stages(ids, maps: dict) -> set:
    """ids のうち、名前つきイベント（ywp_mst_map で名前が引けたマップ）に属するもの"""
    labels = event_labels(maps)
    return {s for s in ids if is_named_event(stage_parts(s, maps, labels)[1])}


def lock_mark(locked: set):
    """build_event_pages 用。未解放の面にだけ 🔒 を付ける関数を返す"""
    if not locked:
        return None
    return lambda sid: LOCK_MARK if sid in locked else ''


def build_event_pages(grouped: list, limit: int = 1900, mark=None) -> list:
    """イベント別のページを組む（1メッセージ = 1ページ）

    1行 = 1マップ。「マップ名（1面）ID（2面）ID …」の形で、長ければ折り返す。
    mark(stageId) が返す文字列を各IDの後ろに付ける（未解放の 🔒 など）。
    """
    pages, cur = [], ''

    def flush(line):
        nonlocal cur
        if len(cur) + len(line) + 1 > limit:
            if cur.strip():
                pages.append(cur.rstrip())
            cur = ''
        cur += line + '\n'

    for event, total, groups in grouped:
        flush(f'**{event}**（{total}件）')
        for name, mid, items in groups:
            head = f'▸ {name or f"map {mid}"}'
            buf = head
            for no, sid in items:
                cell = f'（{no}面）`{sid}`{mark(sid) if mark else ""}'
                if len(buf) + len(cell) + 1 > limit - 40:
                    flush(buf)
                    buf = ' ' * 2      # 折り返し行はインデントだけ
                buf += cell
            flush(buf)
        flush('')
    if cur.strip():
        pages.append(cur.rstrip())
    return pages


def parse_stages(stage_raw) -> dict:
    """ywp_user_stage（行='*' 列='|'）を {stageId: {'cols':[], 'cleared':bool}} に"""
    result = {}
    if not stage_raw:
        return result
    for row in str(stage_raw).split('*'):
        if not row:
            continue
        cols = row.split('|')
        if not cols:
            continue
        try:
            sid = int(cols[0])
        except ValueError:
            continue
        if not (1 <= sid <= 99999999):
            continue
        result[sid] = {'cols': cols, 'cleared': len(cols) >= 2 and cols[1] == '1'}
    return result


async def send_stage_list(interaction: discord.Interaction, c: Client,
                          keyword: str = '', show_all: bool = False,
                          include_master: bool = False, master_hint: bool = True):
    """ログイン済みClientからステージ一覧をephemeralで送る

    master_hint: 未解放が出ないときに「マスタ込み」を案内するか
                 （マスタ込みが失敗してここに来た場合は案内しても無意味なので False）
    """
    stages = parse_stages(c.save.get('ywp_user_stage'))
    if not stages:
        return await interaction.followup.send(
            '❌ `ywp_user_stage` が取得できませんでした。', ephemeral=True)

    if include_master:
        return await send_master_stage_list(interaction, c, stages, keyword)

    cleared_n = sum(1 for v in stages.values() if v['cleared'])
    maps = remember_maps(c.save)
    master = cached_stage_ids(c)       # 通信しない。「マスタ込み」で一度取れていれば入る
    locked = set()
    if show_all:
        ids, label = sorted(stages), '全'
        # セーブには解放済みの面しか無い（新規垢だと各マップ1面だけ）。
        # マスタがあれば、名前つきイベントのマップに限って未解放面も足す。
        # 全マップぶん足すと数千件になるので、イベントだけに絞る。
        if master:
            locked = named_event_stages(master - set(stages), maps)
        # 今のイベントの面は、まだ解放していなくても必ず出す（ステージのマスタから。
        # 前に「マスタ込み」で取ったぶんには、あとから始まったイベントが入っていない）
        ev_ids, _, _ = current_event_stages()
        locked |= {s for s in ev_ids if s not in stages}
        ids = sorted(set(ids) | locked)
    else:
        ids, label = sorted(s for s, v in stages.items() if v['cleared']), 'クリア済み'

    kw = keyword.strip()
    if kw:
        # IDだけでなくイベント名・マップ名でも引けるようにする
        labels = event_labels(maps)
        ids = [s for s in ids if stage_matches(s, kw, maps, labels)]

    if not ids:
        return await interaction.followup.send(
            f'❌ 「{label}ステージ{f" / 検索: {kw}" if kw else ""}」に一致するステージがありません。',
            ephemeral=True)

    grouped = group_stages_by_event(ids, maps)
    locked_shown = locked & set(ids)
    pages = build_event_pages(grouped, mark=lock_mark(locked_shown))
    truncated = len(pages) > STAGE_PAGE_LIMIT
    pages = pages[:STAGE_PAGE_LIMIT]
    named = sum(1 for ev, _, _ in grouped if is_named_event(ev))

    e = discord.Embed(
        title=f'🎮 {label}ステージ一覧（イベント別）',
        description=(f'**表示**: {len(ids)}件 / {len(grouped)}イベント\n'
                     f'**クリア済み**: {cleared_n}件 / 全{len(stages)}件\n'
                     + (f'**{LOCK_MARK} 未解放（イベント）**: {len(locked_shown)}件\n'
                        if locked_shown else '')
                     + f'**検索**: {kw if kw else "（なし）"}'),
        color=COLOR_INFO,
        timestamp=datetime.now(),
    )
    if show_all and not master and master_hint:
        e.add_field(
            name=f'{LOCK_MARK} 未解放の面が出ていません',
            value=('セーブには**解放済みの面しか入らない**ので、新しい垢だと各マップ1面だけになります。'
                   '表示範囲で **「マスタ込み」** を選ぶと未解放も出ます。'
                   '一度取れれば、以降はこの「全部」でもイベントの未解放面が🔒付きで出ます。'),
            inline=False)
    if named < len(grouped):
        e.add_field(
            name='ℹ イベント名が出ないものについて',
            value=('イベント名は `ywp_mst_map`（ログイン応答）から取っていて、'
                   'ここには**開催中のマップしか入りません**。一度見たぶんは '
                   '`data/map_names.json` に貯まるので、使うほど名前が埋まります。'),
            inline=False)
    if truncated:
        e.add_field(name='⚠', value=f'件数が多いため最初の{STAGE_PAGE_LIMIT}ページのみ表示しています。'
                                     '検索キーワードで絞り込んでください。', inline=False)
    sess = SESSIONS.get(interaction.user.id, {})
    name, wallet = sess.get('name'), sess.get('wallet')
    if name:
        e.set_footer(text=f'プレイヤー: {name}' + (f' ／ {wallet}' if wallet else ''))
    else:
        e.set_footer(text='パネルの「周回開始」からこのIDを使えます')

    await interaction.followup.send(embed=e, ephemeral=True)
    for p in pages:
        try:
            await interaction.followup.send(p, ephemeral=True)
        except discord.HTTPException:
            break


# ============================================================================
# 全ステージ（マスタ込み）
# ============================================================================

STATUS_CLEARED = 'クリア済み'
STATUS_UNCLEARED = '未クリア'
STATUS_LOCKED = '未解放'
STATUS_ORDER = (STATUS_CLEARED, STATUS_UNCLEARED, STATUS_LOCKED)


def stage_status(sid: int, stages: dict) -> str:
    """セーブに行が無い＝まだ解放されていないステージ"""
    info = stages.get(sid)
    if info is None:
        return STATUS_LOCKED
    return STATUS_CLEARED if info['cleared'] else STATUS_UNCLEARED


def build_master_report(stages: dict, master_ids: set, keyword: str = '',
                        maps: dict | None = None):
    """(イベント別集計, 添付用テキスト, 対象ID数) を返す

    件数が数千行になるので、Discordのメッセージではなく .txt 添付で全件を渡す。
    添付は「イベント名 / マップ名 / 面数 / stageId / 状態」のTSV。
    """
    maps = maps if maps is not None else _load_map_cache()
    ids = sorted(set(stages) | set(master_ids))
    kw = keyword.strip()
    if kw:
        labels = event_labels(maps)     # 件数が多いので1回だけ組む
        ids = [s for s in ids if stage_matches(s, kw, maps, labels)]
    if not ids:
        return {}, '', 0

    summary, lines = {}, []
    for event, total, groups in group_stages_by_event(ids, maps):
        counts = dict.fromkeys(STATUS_ORDER, 0)
        body = []
        for name, mid, items in groups:
            for no, sid in items:
                st = stage_status(sid, stages)
                counts[st] += 1
                body.append(f'{event}\t{name or f"map {mid}"}\t{no}面\t{sid}\t{st}')
        summary[event] = (total, counts)
        lines.append(f'■ {event}  ({total}件)')
        lines.extend(body)
        lines.append('')

    header = [
        'ぷにぷに 全ステージ一覧（イベント別 / マスタ込み）',
        f'生成: {datetime.now():%Y-%m-%d %H:%M:%S}',
        f'対象: {len(ids)}件' + (f' / 検索: {kw}' if kw else ''),
        f'{STATUS_LOCKED} = マスタにあるが自分のセーブに行が無い（周回不可）',
        'stageId = mapId * 1000 + 面数',
        '列: イベント名 / マップ名 / 面数 / stageId / 状態',
        '',
    ]
    return summary, '\n'.join(header + lines), len(ids)


async def send_master_stage_list(interaction: discord.Interaction, c: Client,
                                 stages: dict, keyword: str = ''):
    """getMaster.nhn のステージマスタと突き合わせて全ステージを出す"""
    master_ids, key, source = await asyncio.to_thread(fetch_all_stage_ids, c)

    if not master_ids:
        note = ('マスタ取得に失敗しました（候補キーが全滅）。'
                '各キーの生レスポンスを `data/master_probe.json` に書き出したので、'
                'そこで正しいキーを確認して `ywp_master.py` の `CANDIDATE_KEYS` '
                'に追加してください。'
                if source == 'failed' else
                '直近でマスタ取得に失敗しているため、今回は再試行を見送りました。'
                '（`data/master_cache.json` を消すと再探索します）')
        await interaction.followup.send(
            f'⚠ {note}\nセーブにあるぶんだけで一覧を出します。',
            ephemeral=True)
        return await send_stage_list(interaction, c, keyword, show_all=True,
                                     master_hint=False)

    maps = remember_maps(c.save)
    summary, text, total = build_master_report(stages, master_ids, keyword, maps)
    if not total:
        return await interaction.followup.send(
            f'❌ 「検索: {keyword.strip()}」に一致するステージがありません。', ephemeral=True)

    cleared_n = sum(1 for v in stages.values() if v['cleared'])
    locked_n = len(set(master_ids) - set(stages))

    e = discord.Embed(
        title='🗂 全ステージ一覧（イベント別 / マスタ込み）',
        description=(f'**マスタ総数**: {len(master_ids)}件\n'
                     f'**{STATUS_CLEARED}**: {cleared_n}件 / '
                     f'**{STATUS_UNCLEARED}**: {len(stages) - cleared_n}件 / '
                     f'**{STATUS_LOCKED}**: {locked_n}件\n'
                     f'**表示**: {total}件 / {len(summary)}イベント'
                     + (f'\n**検索**: {keyword.strip()}' if keyword.strip() else '')),
        color=COLOR_INFO,
        timestamp=datetime.now(),
    )
    SHOWN = 20          # Embedのフィールド上限(25)に収める
    for name, (n, counts) in list(summary.items())[:SHOWN]:
        e.add_field(
            name=f'{name[:60]}（{n}件）',
            value=(f'✅ {counts[STATUS_CLEARED]} / '
                   f'⬜ {counts[STATUS_UNCLEARED]} / '
                   f'🔒 {counts[STATUS_LOCKED]}'),
            inline=True)
    if len(summary) > SHOWN:
        e.add_field(name='⚠',
                    value=f'イベントが{len(summary)}件あるため上位{SHOWN}件のみ集計表示。'
                          '全件は添付の .txt にあります。', inline=False)
    e.set_footer(text=f'getMaster.nhn key={key} / '
                      f'{"キャッシュ" if source == "cache" else "新規取得"}'
                      ' — 全件は添付の .txt を見てください')

    fname = f'stages_{interaction.user.id}.txt'
    f = discord.File(io.BytesIO(text.encode('utf-8')), filename=fname)
    try:
        await interaction.followup.send(embed=e, file=f, ephemeral=True)
    except discord.HTTPException as ex:
        await interaction.followup.send(
            f'⚠ 一覧ファイルの送信に失敗しました: `{str(ex)[:150]}`', ephemeral=True)

    # 名前つきイベント（開催中のイベントなど）だけは画面にも出す。
    # 全件は数千行あるので .txt に任せ、見たいものが多いイベント分だけ直接見せる。
    all_ids = set(stages) | set(master_ids)
    kw = keyword.strip()
    if kw:
        labels = event_labels(maps)
        all_ids = {s for s in all_ids if stage_matches(s, kw, maps, labels)}
    ev_ids = named_event_stages(all_ids, maps)
    if not ev_ids:
        return
    locked = ev_ids - set(stages)
    pages = build_event_pages(group_stages_by_event(sorted(ev_ids), maps),
                              mark=lock_mark(locked))
    for p in pages[:STAGE_PAGE_LIMIT]:
        try:
            await interaction.followup.send(p, ephemeral=True)
        except discord.HTTPException:
            break


# ============================================================================
# アカウント自動作成（旧 main.py 由来）
# ============================================================================

class GuerrillaMail:
    """GuerrillaMail 公式APIの最小ラッパー（非同期・aiohttp版）

    旧 main.py が使っていた guerrillamail ライブラリは現在入手できないため、
    同じ公式API (api.guerrillamail.com/ajax.php) を直接叩いて同等のことをする。
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.sid_token: str | None = None
        self.address: str | None = None

    async def _call(self, func: str, **params) -> dict:
        params['f'] = func
        params.setdefault('ip', '127.0.0.1')
        params.setdefault('agent', 'guerrillamail-api-client')
        if self.sid_token:
            params.setdefault('sid_token', self.sid_token)
        async with self.session.get(GM_API_URL, params=params) as r:
            data = await r.json(content_type=None)
        if not isinstance(data, dict):
            raise RuntimeError(f'GuerrillaMail API の応答が不正です (status={r.status})')
        tok = data.get('sid_token')
        if tok:
            self.sid_token = tok
        return data

    async def create_address(self) -> str:
        """新しい捨てメールアドレスを取得する"""
        data = await self._call('get_email_address')
        self.address = (data.get('email_addr') or '').strip()
        if not self.address:
            raise RuntimeError('捨てメールアドレスを取得できませんでした')
        return self.address

    async def check_email(self) -> list:
        """受信箱の一覧を取得する"""
        data = await self._call('check_email', seq=0)
        return data.get('list') or []

    async def fetch_body(self, mail_id) -> str:
        """指定したメールの本文を取得する"""
        data = await self._call('fetch_email', email_id=str(mail_id))
        return data.get('mail_body') or ''


def generate_password(length: int = PASSWORD_LEN) -> str:
    """英数字のみのランダムパスワードを生成する（旧 main.py と同じ方式）"""
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(length))


def random_player_name() -> str:
    """指定が無いときのアカウント名（従来どおり ぷに+4桁）"""
    return 'ぷに' + ''.join(random.choice(string.digits) for _ in range(4))


def resolve_player_name(raw: str | None, index: int) -> str:
    """入力されたアカウント名を1件ぶんに解決する

    空欄なら従来どおりランダム。`{n}` を含めると連番に置き換わるので、
    まとめて作るときに `テスト{n}` → テスト1, テスト2... と分けられる。
    """
    raw = (raw or '').strip()
    if not raw:
        return random_player_name()
    if '{n}' in raw:
        return raw.replace('{n}', str(index))
    return raw


def fmt_jp_datetime(dt: datetime) -> str:
    return dt.strftime('%Y年%m月%d日 %H時%M分')


class AccountRecord:
    """作成できたアカウント1件ぶんの記録"""

    def __init__(self, email: str, password: str, save_name: str | None = None,
                 user_id=None, client: Client | None = None,
                 created_at: datetime | None = None):
        self.email = email
        self.password = password
        self.save_name = save_name
        self.user_id = user_id
        self.client = client
        self.created_at = created_at or datetime.now()
        # 久垢作成のときだけ使う。True=クリア済み / False=失敗 / None=対象外
        self.cleared: bool | None = None
        self.clear_error: str | None = None
        self.clear_target: str | None = None    # クリアさせた面の表示（None なら AGED_STAGE_ID）
        # 回し放題垢のときだけ使う。True=引けた / False=引けなかった / None=対象外
        self.vip_ok: bool | None = None
        self.vip_text: str | None = None
        self.vip_closed = False     # まわし放題ガシャが開催されていなかった
        self.vip_tally: collections.Counter = collections.Counter()   # 引いた妖怪（vip_tally）


def find_auth_token(html: str) -> str | None:
    """登録ページのHTMLからCSRFトークン（authenticity_token）を取り出す"""
    for pat in (r'name="authenticity_token"[^>]*value="([^"]+)"',
                r'value="([^"]+)"[^>]*name="authenticity_token"',
                r'name="csrf-token"\s+content="([^"]+)"',
                r'content="([^"]+)"[^>]*name="csrf-token"'):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


async def fetch_auth_token(session: aiohttp.ClientSession, url: str,
                           headers: dict) -> str | None:
    """url をGETしてCSRFトークンを抽出する"""
    try:
        async with session.get(url, headers=headers) as r:
            if r.status != 200:
                return None
            html = await r.text()
    except aiohttp.ClientError as e:
        print(f'[account] トークン取得に失敗 {url}: {str(e)[:120]}')
        return None
    return find_auth_token(html)


async def wait_verification_email(gm: GuerrillaMail) -> str | None:
    """LEVEL5からの登録メール本文を待つ。規定時間内に届かなければNone"""
    await asyncio.sleep(10)   # 届くまで少し待つ（旧 main.py と同じ初回待ち）
    deadline = time.time() + MAIL_WAIT_TIMEOUT
    while True:
        try:
            items = await gm.check_email()
        except Exception as e:
            print(f'[account] メール一覧の取得に失敗: {str(e)[:120]}')
            items = []
        for it in items:
            if not isinstance(it, dict):
                continue
            if (it.get('mail_subject') or '') != L5_VERIFY_SUBJECT:
                continue
            mail_id = it.get('mail_id') or it.get('guid')
            if not mail_id:
                continue
            try:
                body = await gm.fetch_body(mail_id)
            except Exception:
                continue
            if body:
                return body
        if time.time() >= deadline:
            return None
        await asyncio.sleep(MAIL_POLL_INTERVAL)


async def create_level5_account(progress, password: str | None = None,
                                player_name: str | None = None,
                                proxy: str | None = None) -> AccountRecord:
    """
    GuerrillaMail の捨てメールで LEVEL5 ID を新規作成し、周回用のセーブデータ
    （ゲーム内ユーザー）まで作って AccountRecord を返す。
    セーブ作成に失敗した場合は save_name / client が None になる。
    progress: await progress('テキスト') で進捗を通知するコールバック
    password: 固定パスワード。省略・空欄のときは毎回ランダム生成する
    player_name: ゲーム内のアカウント名。省略時は ぷに+4桁 をランダム生成
    proxy: 指定時（プロキシプールから借りた出口）、登録フロー・捨てメール・
           セーブ作成の通信すべてがそのプロキシを通る
    """
    # trust_env=True で ywp_auto.set_proxy() が立てた HTTP_PROXY を拾う
    # （LEVEL5 ID の登録フローと捨てメールも同じ出口を通す）。
    # proxy 指定のときは全体設定ではなく、その出口だけを通る（SOCKS も可）
    if proxy:
        if ProxyConnector is None:
            raise RuntimeError('aiohttp_socks が入っていません。pip install aiohttp_socks を実行してください')
        session = aiohttp.ClientSession(connector=ProxyConnector.from_url(proxy))
    else:
        session = aiohttp.ClientSession(trust_env=True)
    async with session:
        # --- 捨てメールアドレスを生成 ---
        await progress('📧 捨てメールアドレスを生成中...')
        gm = GuerrillaMail(session)
        email = await gm.create_address()
        print(f'[account] 一時メール: {email}')

        # --- 仮登録 ---
        await progress('📤 LEVEL5 ID 仮登録リクエスト送信中...')
        token = await fetch_auth_token(session, L5_REG_URL, BROWSER_HEADERS)
        if not token:
            raise RuntimeError('仮登録ページからCSRFトークンを取得できませんでした')
        async with session.post(L5_REG_URL, headers=BROWSER_HEADERS, data={
            'utf8': '✓',
            'authenticity_token': token,
            'form[game_id]': '',
            'form[client_id]': '',
            'form[country_id]': '287',
            'form[email]': email,
            'form[mail_received]': 'true',
        }) as r:
            if r.status != 200:
                raise RuntimeError(f'仮登録に失敗しました (status={r.status})')

        # --- 登録メールの到着を待つ ---
        await progress(
            f'📬 登録メールの到着を待っています...（最大 {int(MAIL_WAIT_TIMEOUT)} 秒）')
        body = await wait_verification_email(gm)
        if body is None:
            raise RuntimeError('登録メールが規定時間内に届きませんでした')

        m = re.search(r'https://auth\.level5-id\.com/user_registration/verify/\w+', body)
        if not m:
            raise RuntimeError('メール本文から検証URLを見つけられませんでした')
        verify_url = m.group(0)

        # --- 検証URL（＝パスワード入力フォーム）を取得 ---
        # ※register URL はPOST専用でGETすると404になるため、CSRFトークンは
        #   検証ページのフォームから取得する（トークンはセッションごとに変わる）
        await progress('📝 パスワード入力ページを取得中...')
        async with session.get(verify_url, headers=BROWSER_HEADERS) as r:
            if r.status != 200:
                raise RuntimeError(f'検証ページの取得に失敗しました (status={r.status})')
            verify_html = await r.text()
        token = find_auth_token(verify_html)
        if not token:
            raise RuntimeError('検証ページからCSRFトークンを取得できませんでした')
        fm = re.search(r'<form[^>]*action="([^"]+)"', verify_html)
        if fm and fm.group(1):
            action = fm.group(1)
            register_url = (action if action.startswith('http')
                            else 'https://auth.level5-id.com' + action)
        else:
            register_url = verify_url.replace('verify', 'register') + '?locale=ja'

        # --- パスワード登録（本登録） ---
        await progress('🔐 パスワードを登録中...')
        password = (password or '').strip() or generate_password()
        headers = dict(BROWSER_HEADERS)
        headers['Referer'] = verify_url
        async with session.post(register_url, headers=headers, data={
            'utf8': '✓',
            'authenticity_token': token,
            'form[password]': password,
            'form[password_confirmation]': password,
        }) as r:
            result_html = await r.text()
            if r.status != 200:
                raise RuntimeError(f'本登録に失敗しました (status={r.status})')
            # 200でもパスワードフォームが再表示されていれば登録失敗扱い
            if 'form[password]' in result_html:
                raise RuntimeError('本登録が拒否されました（フォームが再表示されました）')
        print(f'[account] 本登録完了: {email}')

    # --- セーブデータ作成（このL5IDに紐づけて、メアド/パスで周回可能にする） ---
    player_name = (player_name or '').strip() or random_player_name()
    await progress(f'🎮 セーブデータ「{player_name}」を作成中...')

    def _make_save():
        c, uid = create_save(player_name, proxy=proxy)
        # 作ったセーブ（gdkey）を今作ったLEVEL5 IDに連携する
        link_email(c.udkey, email, password, proxy=proxy)
        # このUDkeyを以後のログインでも使い回す（周回のたびに端末が増えないように）
        save_udkey_for(email, c.udkey)
        return c, uid

    try:
        c, uid = await asyncio.to_thread(_make_save)
        print(f'[account] セーブ作成完了: {email} userId={uid} ({player_name})')
        return AccountRecord(email, password, player_name, uid, c)
    except Exception as e:
        # LEVEL5 ID自体は作成済みなので、セーブ失敗は警告付きで続行する
        print(f'[account] セーブ作成失敗 {email}: {str(e)[:200]}')
        await progress(f'⚠ セーブデータの作成に失敗しました: `{str(e)[:150]}`\n'
                       '（LEVEL5 IDは作成済みです）')
        return AccountRecord(email, password)


# メニュー（ywp_user_menufunc の ID）と、それが開くステージ。この順に開く。
# サーバーはそのステージで妖怪が友だちになった gameEnd でだけ開ける
# （ywp_auto.befriend_pick 参照）。2026-09-25 までの bot は友だちを申告して
# いなかったので、その間に進めた垢は何周してもショップ等が開かない。
MENU_UNLOCK_STAGES = ((1, 1001004), (2, 1001006), (4, 1001008), (3, 1001012))


def menus_to_reopen(c: Client) -> list[tuple[int, int]]:
    """開き損ねたメニューのうち、ステージは開いていて取り返せるもの"""
    mf = c.progress_detail().get('ywp_user_menufunc', {})
    stages = c.stages()
    return [(m, s) for m, s in MENU_UNLOCK_STAGES
            if mf.get(str(m)) == '0' and s in stages]


async def unlock_menus(c: Client, progress=None) -> dict:
    """開き損ねたメニュー（ショップ/ガシャ等）を開け直す

    該当ステージを順に1回ずつクリアし直す（友だちの申告が付くので開く）。
    2026-09-27 に、12面まで進んで全部閉じていた垢で 4→6→8→12面を再クリアし、
    4つとも開いて落ちたフラグが無いことを確認済み。
    戻り値は menufunc の変化 {ID: (前, 後)}。途中で失敗したら RuntimeError。
    """
    todo = menus_to_reopen(c)
    if not todo:
        return {}
    before = c.progress_detail()
    log_line('メニューの開け直し: ' + ', '.join(f'menufunc {m}←{s}' for m, s in todo))
    for n, (m, s) in enumerate(todo):
        if n:
            # gameEnd→次の gameEnd が10秒を切ると rc=3（パラメータ不正）で弾かれる
            # （2026-09-27 に 9.9秒・9.1秒で発生）。実機のようにマップに戻る間を空ける
            await asyncio.sleep(random.uniform(8.0, 15.0))
        if progress:
            await progress(f'🔓 メニューを開放中（ステージ `{s}` をクリア）...')
        await clear_stage(c, s, progress)
    await asyncio.to_thread(c.refresh)
    changed = Client.detail_diff(before, c.progress_detail())
    log_line('  開け直しの結果: '
             + (Client.detail_diff_text(changed) if changed else 'フラグが1つも動かなかった'))
    return changed.get('ywp_user_menufunc', {})


async def clear_stage(c: Client, stage_id: int, progress=None,
                      attempts: int = AGED_CLEAR_RETRY, inspect: bool = True):
    """stage_id を1回クリアする（久垢作成用）。できなければ RuntimeError

    バトル時間の下限は通常の周回と同じものを守る（BATTLE_TIME_MIN 参照）。
    inspect: クリア後にセーブを取り直して進行フラグの変化をログに残す（1面ごとに1往復増える）
    """
    last = ''
    for n in range(1, attempts + 1):
        if progress and n > 1:
            await progress(f'⚔ ステージ `{stage_id}` に再挑戦中...（{n}/{attempts}）')

        try:
            rc, js, btype = await asyncio.to_thread(c.battle_start, stage_id)
        except Exception as e:
            last = f'通信エラー: {str(e)[:120]}'
            await asyncio.sleep(AGED_RETRY_WAIT)
            continue

        code = js.get('resultCode') if isinstance(js, dict) else None
        if code != 0:
            if is_hitodama_shortage(js):
                raise RuntimeError('ひとだま不足でステージを開始できませんでした')
            last = f'gameStart rc={code} ({RC.get(code, "不明")})'
            if code in FATAL_CODES:
                raise RuntimeError(last)
            await asyncio.sleep(AGED_RETRY_WAIT)
            continue

        # バトル時間。サーバーが計る gameStart→gameEnd の間隔で下限を判定する。
        # 最短のクリア秒は c.min_clear_time（今は一律 BATTLE_TIME_MIN）
        need = c.min_clear_time(stage_id, js)
        wait = jitter(BATTLE_TIME_MIN, BATTLE_TIME_MIN, spread=BATTLE_TIME_SPREAD)
        if need > wait:
            wait = need * random.uniform(1.0, 1.12)
        t0 = time.time()
        await asyncio.sleep(wait)
        shortfall = need - (server_elapsed(js) or (time.time() - t0))
        if shortfall > 0:
            await asyncio.sleep(shortfall)

        try:
            rc, result = await asyncio.to_thread(c.battle_end, stage_id, btype, js)
        except Exception as e:
            last = f'通信エラー: {str(e)[:120]}'
            await asyncio.sleep(AGED_RETRY_WAIT)
            continue

        code = result.get('resultCode') if isinstance(result, dict) else None
        if code == 0 and not inspect:
            return
        if code == 0:
            # クリアでサーバー側の進行（解放フラグ）が動いたかを残す。
            # 「クリアはされるがショップ/ガシャが解放されない」の切り分け用
            before, before_detail = c.save_shape(), c.progress_detail()
            log_line('gameEnd成功。応答に載っていたテーブル: '
                     + ', '.join(sorted(k for k in result if k.startswith('ywp_user')))
                     + ' / それ以外: '
                     + ', '.join(sorted(k for k in result if not k.startswith('ywp_'))))
            try:
                await asyncio.to_thread(c.refresh)
                diff = Client.shape_diff(before, c.save_shape())
                log_line('クリア前後のセーブ変化: '
                         + (', '.join(f'{k} {a}→{b}' for k, (a, b) in diff.items())
                            if diff else '行数の変化なし'))
                changed = Client.detail_diff(before_detail, c.progress_detail())
                log_line('  進行フラグの変化: '
                         + (Client.detail_diff_text(changed) if changed
                            else 'なし（menufunc/tutorial が1つも動いていない）'))
            except Exception as e:
                log_line(f'クリア後のセーブ取り直しに失敗: {str(e)[:120]}')
            return
        last = f'gameEnd rc={code} ({RC.get(code, "不明")})'
        if code in FATAL_CODES:
            raise RuntimeError(last)
        await asyncio.sleep(AGED_RETRY_WAIT)

    raise RuntimeError(last or 'ステージをクリアできませんでした')


VIP_NAME_RANK = 'Z'     # このランク以上は、引いた妖怪の名前まで並べる（下は数だけ）


def vip_tally(c: Client, ids: list) -> collections.Counter:
    """ガチャで出た妖怪を {(ランク番号, 名前): 体数} に数える（何垢ぶんでも足し合わせられる形）"""
    names = {r[0]: r[1] for r in c.master_table('ywp_mst_youkai') if len(r) > 1}
    return collections.Counter((c.youkai_rank(y) or 0, names.get(str(y), str(y))) for y in ids)


def vip_summary(tally: collections.Counter, name_rank: str | None = None) -> str:
    """vip_tally をランクの高い順にまとめる。name_rank（既定 VIP_NAME_RANK）以上は1ランク1行で名前も並べる

    例: 「UZ 3体: 百鬼あやめ(ハロウィン)×2、猫又おかゆ(浴衣)」…「SS 14体 / S 211体」
    """
    by_rank: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    for (rank, name), n in tally.items():
        by_rank[rank][name] += n
    lines, rest = [], []
    for rank in sorted(by_rank, reverse=True):
        cnt = by_rank[rank]
        head = f'{RANK_NAMES.get(rank, "?")} {sum(cnt.values())}体'
        if rank >= parse_rank(name_rank or VIP_NAME_RANK):
            lines.append(f'**{head}**: ' + '、'.join(n + (f'×{k}' if k > 1 else '')
                                                     for n, k in cnt.most_common()))
        else:
            rest.append(head)
    return '\n'.join(lines + ([' / '.join(rest)] if rest else []))


def vip_stages() -> list[int]:
    """回し放題垢で先にクリアする面（プリセット「イベステ解放」の先頭から VIP_STAGE_COUNT 面）"""
    ids = PRESETS['イベステ解放']['ids']
    return ids[:max(1, min(int(VIP_STAGE_COUNT), len(ids)))]


async def make_vip_account(rec: AccountRecord, count: int, progress):
    """回し放題垢: プリセット「イベステ解放」の先頭 VIP_STAGE_COUNT 面を新しい垢の安全な速さでクリアしてから、
    まわし放題ガシャを count 連引く（2026-10-01 捨て垢で同じ流れを確認済み）"""
    if rec.client is None:
        rec.cleared = False
        rec.clear_error = 'セーブデータが作れなかったためクリアできません'
        await progress('⚠ セーブが無いのでステージをクリアできません')
        return
    c = rec.client
    stages = vip_stages()
    rec.clear_target = f'第1章 {len(stages)}面（`{stages[0]}`〜`{stages[-1]}`）'
    sid = stages[0]
    try:
        for n, sid in enumerate(stages, 1):
            # 1面に1分近くかかるので、面ごとに止める合図を見る
            if ACCOUNT_PAUSE['paused'] or maint_window():
                raise RuntimeError('メンテ/管理者の停止のため中断' if maint_window()
                                   else '管理者がアカウント作成を停止したため中断')
            if n > 1:
                await asyncio.sleep(random.uniform(*VIP_STAGE_GAP))
            await progress(f'⚔ 第1章をクリア中... `{sid}`（{n}/{len(stages)}）')
            await clear_stage(c, sid, progress, inspect=False)
        rec.cleared = True
    except Exception as e:
        rec.cleared = False
        rec.clear_error = f'{sid}: {str(e)[:230]}'
        await progress(f'⚠ ステージクリア失敗: `{rec.clear_error[:150]}`')
        return

    await progress(f'🎰 まわし放題ガシャを {count}連 引いています...')
    await asyncio.sleep(random.uniform(5.0, 10.0))      # マップからガチャ画面へ
    rec.vip_ok, rec.vip_text, rec.vip_tally, rec.vip_closed = await vip_pull(c, count, rec.email)
    if not rec.vip_ok:
        await progress(f'⚠ まわし放題ガシャ失敗: `{rec.vip_text[:150]}`')


async def vip_pull(c: Client, count: int, who: str):
    """ログイン済みの垢で まわし放題ガシャを count 連引く（新規垢・既存垢の共通）

    戻り値: (引けたか, 表示用の文, vip_tally, 開催期間外だったか)
    """
    try:
        gid, name, cnt, ids = await asyncio.to_thread(c.pull_vip_gacha, count)
        tally = await asyncio.to_thread(vip_tally, c, ids)
    except VipUnavailable as e:
        log_line(f'まわし放題ガシャ {who}: 引けない {e}')
        return False, str(e)[:250], collections.Counter(), e.closed
    except Exception as e:
        log_line(f'まわし放題ガシャ {who}: 失敗 {str(e)[:200]}')
        return False, str(e)[:250], collections.Counter(), False
    log_line(f'まわし放題ガシャ {who}: {name} {cnt}連 {len(ids)}体')
    text = (f'**{name}** {cnt}連' + (f'（指定{count}は上限{cnt}に合わせた）' if cnt < count else '')
            + f'\n{vip_summary(tally)}')
    return True, text, tally, False


def account_result_embed(rec: AccountRecord, index: int = 1, total: int = 1,
                         kind: str = '通常垢') -> discord.Embed:
    title = f'🆕 {kind} 作成完了'
    if total > 1:
        title += f'（{index}/{total}件目）'
    ok = rec.cleared is not False and rec.vip_ok is not False
    e = discord.Embed(color=COLOR_OK if ok else COLOR_WARN, title=title, timestamp=rec.created_at)
    e.add_field(name='メールアドレス', value=f'`{rec.email}`', inline=False)
    e.add_field(name='パスワード', value=f'||{rec.password}||', inline=False)
    e.add_field(name='📅 作成日時', value=fmt_jp_datetime(rec.created_at), inline=False)
    if rec.save_name:
        e.add_field(
            name='セーブデータ',
            value=f'✅ **{rec.save_name}** を作成して連携済み — このアドレスと'
                  'パスワードでそのまま周回できます',
            inline=False,
        )
    else:
        e.add_field(
            name='セーブデータ',
            value='⚠ 作成に失敗しました（LEVEL5 IDのみ作成済み）',
            inline=False,
        )
    target = rec.clear_target or f'`{AGED_STAGE_ID}`'
    if rec.cleared is True:
        e.add_field(name='ステージ',
                    value=f'✅ {target} をクリア済み', inline=False)
    elif rec.cleared is False:
        e.add_field(name='ステージ',
                    value=f'⚠ {target} のクリアに失敗しました\n'
                          f'`{(rec.clear_error or "")[:200]}`', inline=False)
    if rec.vip_ok is not None:
        e.add_field(name='🎰 まわし放題ガシャ',
                    value=(f'✅ {rec.vip_text}' if rec.vip_ok
                           else f'⚠ 引けませんでした\n`{(rec.vip_text or "")[:200]}`')[:1024],
                    inline=False)
    e.add_field(
        name='ℹ 補足',
        value='・パスワードはスポイラーで隠してあります（タップで表示）\n'
              '・同じ内容をDMにも送っています',
        inline=False,
    )
    return e


def accounts_txt(records: list[AccountRecord], started_at: datetime,
                 finished_at: datetime, kind: str, ng: int = 0) -> str:
    """まとめ作成したときの .txt 本文（1行目に作成時間帯、あとは メアド:パスワード）"""
    head = [
        f'ぷにぷに {kind} 作成結果',
        f'{fmt_jp_datetime(started_at)} から {fmt_jp_datetime(finished_at)} '
        f'までに作られた垢',
        f'作成数: {len(records)}件' + (f'（ほかに失敗 {ng}件）' if ng else ''),
    ]
    if kind == '久垢':
        done = sum(1 for r in records if r.cleared)
        head.append(f'ステージ{AGED_STAGE_ID} クリア済み: {done}件 / {len(records)}件')
    if kind == '回し放題垢':
        done = sum(1 for r in records if r.vip_ok)
        head.append(f'第1章クリア＋まわし放題ガシャ済み: {done}件 / {len(records)}件')
    head += ['=' * 56, '']
    tail = []
    if kind == '回し放題垢':
        # メアド:パスワード の並びは崩さず、引いた結果は下にまとめる
        tail = ['', '=' * 56, 'まわし放題ガシャの結果', '=' * 56]
        for r in records:
            text = (r.vip_text or '').replace('**', '') if r.vip_ok else \
                f'引けなかった: {r.vip_text or r.clear_error or "不明"}'
            tail += ['', r.email] + [f'  {line}' for line in text.splitlines()]
        tail.append('')
    return '\n'.join(head + [f'{r.email}:{r.password}' for r in records] + [''] + tail)


async def send_accounts_txt(interaction: discord.Interaction,
                            records: list[AccountRecord], started_at: datetime,
                            finished_at: datetime, kind: str, ng: int = 0):
    """メアド:パスワード の一覧を .txt にしてDM（＋保険でその場）に送る"""
    text = accounts_txt(records, started_at, finished_at, kind, ng)
    data = text.encode('utf-8')
    fname = f'accounts_{started_at:%Y%m%d_%H%M}_{len(records)}.txt'
    header = (f'📄 **{kind}** {len(records)}件のメアド/パスワードです。\n'
              f'📅 **{fmt_jp_datetime(started_at)}** 〜 '
              f'**{fmt_jp_datetime(finished_at)}** に作成'
              + (f'\n⚠ ほかに {ng}件が作成失敗しています。' if ng else ''))

    sent = False
    try:
        await interaction.user.send(
            header, file=discord.File(io.BytesIO(data), filename=fname))
        sent = True
    except discord.HTTPException as e:
        print(f'[account] txtのDM送信に失敗 user={interaction.user.id}: {str(e)[:150]}')
    try:
        await interaction.followup.send(
            header if sent else header + '\n⚠ DMに送れなかったのでここに添付します。',
            file=discord.File(io.BytesIO(data), filename=fname), ephemeral=True)
    except discord.HTTPException:
        pass
    return sent


def creation_blocker(user_id: int, name_tpl: str | None) -> str | None:
    """アカウント作成を今受け付けられない理由。受け付けられるなら None"""
    if name_tpl and len(name_tpl.replace('{n}', '')) > PLAYER_NAME_MAX:
        return f'❌ アカウント名は {PLAYER_NAME_MAX} 文字以内で指定してください。'
    if ACCOUNT_PAUSE['paused']:
        return '⏸ 現在、アカウント作成は管理者により**停止中**です。再開されるまでお待ちください。'
    mw = maint_window()
    if mw:
        return (f'🛠 メンテ（{maint_text(mw)}）のため、アカウント作成を止めています。'
                '管理者が再開するまでお待ちください。')
    if user_id in ACCOUNT_RUNNING:
        return '⏳ アカウント作成が実行中です。完了までお待ちください。'
    return None


async def notify_owners(text: str):
    """Botオーナー全員にDMする（有料注文の知らせ・不足の連絡）。送れなくても止めない"""
    for uid in OWNER_IDS:
        try:
            user = bot.get_user(uid) or await bot.fetch_user(uid)
            await user.send(text[:2000])
        except discord.HTTPException as e:
            print(f'[owner] DMに失敗 {uid}: {str(e)[:120]}')


# --- 完了実績の投稿（無料メニューと有料メニューで投稿先を分ける。/実績チャンネル設定） ---
RESULT_FILE = os.path.join(BASE_DIR, 'data', 'result_channels.json')
RESULT_KINDS = {'free': '無料メニュー', 'paid': '有料メニュー'}


def results_conf() -> dict:
    """{'free': チャンネルID, 'paid': チャンネルID, 'free_no': 通し番号, 'paid_no': 通し番号}"""
    try:
        with open(RESULT_FILE, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def results_save(d: dict):
    try:
        os.makedirs(os.path.dirname(RESULT_FILE), exist_ok=True)
        with open(RESULT_FILE, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except OSError as e:
        print(f'[実績] 設定の保存に失敗: {e}')


def results_text() -> str:
    d = results_conf()
    return '\n'.join(f'・{label}: ' + (f'<#{d[k]}>（{int(d.get(f"{k}_no") or 0)}件 投稿済み）'
                                       if d.get(k) else '未設定（投稿しない）')
                     for k, label in RESULT_KINDS.items())


async def post_result(paid: bool, user, menu: str, amount: int = 0,
                      tally: collections.Counter | None = None):
    """完了実績を投稿する。paid で有料メニュー/無料メニューのチャンネルに分ける（未設定なら投稿しない）"""
    try:
        from hub.achievements import post_achievement
    except ImportError:
        post_achievement = None
    if post_achievement is not None:
        extra = [('🎰 引いたキャラ', vip_summary(tally, name_rank='ZZZ'))] if tally else None
        await post_achievement(bot, game='puni', user=user, menu=menu, amount=amount if paid else 0,
                               extra_fields=extra)
        return
    key = 'paid' if paid else 'free'
    d = results_conf()
    cid = d.get(key)
    if not cid:
        return
    try:
        ch = bot.get_channel(int(cid)) or await bot.fetch_channel(int(cid))
    except (discord.HTTPException, ValueError) as e:
        print(f'[実績] チャンネル {cid} を開けません: {str(e)[:120]}')
        return
    no = int(d.get(f'{key}_no') or 0) + 1
    e = discord.Embed(title='✅ 完了実績' + ('' if paid else '（無料）'),
                      description=f'{user.mention} 様のご依頼を完了しました。',
                      color=COLOR_OK, timestamp=datetime.now())
    e.add_field(name='ご依頼内容', value=menu[:1024], inline=False)
    e.add_field(name='ご成約金額', value=f'¥{amount:,}' if paid else '無料', inline=False)
    if tally:
        e.add_field(name='🎰 引いたキャラ', value=vip_summary(tally, name_rank='ZZZ')[:1024], inline=False)
    e.add_field(name='​', value='> ご利用ありがとうございました。またのご依頼をお待ちしております。',
                inline=False)
    e.set_footer(text=f'No.{no} ・ ぷにぷに代行')
    try:
        await ch.send(embed=e, allowed_mentions=discord.AllowedMentions(users=True))
    except discord.HTTPException as ex:
        print(f'[実績] 投稿に失敗 ch={cid}: {str(ex)[:120]}')
        return
    d = results_conf()          # 投稿の間に別の実績が数えられていても番号を飛ばさない
    d[f'{key}_no'] = max(no, int(d.get(f'{key}_no') or 0) + 1)
    results_save(d)


async def start_account_creation(interaction: discord.Interaction, count: int = 1,
                                 password: str | None = None,
                                 player_name: str | None = None,
                                 aged: bool = False, vip: int | None = None,
                                 order: str | None = None, price: int = 0):
    """「アカウント作成」/「久垢作成」/「回し放題垢作成」の共通入口。count件ぶん連続で作成する

    password    固定パスワード。空欄なら1件ごとにランダム生成
    player_name アカウント名。空欄ならランダム。`{n}` は連番に置き換わる
    aged        Trueならセーブ作成後に AGED_STAGE_ID をクリアさせる（久垢）
    vip         連数を渡すと、第1章16面をクリアしてから まわし放題ガシャを引く（回し放題垢）
    order       有料版の取引番号（支払い確認済み）。回し放題まで済んだ垢が count 件そろうまで
                作り直し（VIP_PAID_RETRY）、足りなければお客様とオーナーに知らせる
    price       有料版で受け取った金額（完了実績に載せる）
    ACCOUNT_TXT_THRESHOLD 件以上まとめて作るときは、1件ずつDMせずに
    最後に「メアド:パスワード」の .txt をDMで送る。
    """
    kind = '久垢' if aged else '回し放題垢' if vip else '通常垢'
    fixed_password = (password or '').strip() or None
    name_tpl = (player_name or '').strip() or None
    # 有料版（order あり）は支払いの前に同じ確認を済ませている。払った後は断らない
    if order is None:
        why = creation_blocker(interaction.user.id, name_tpl)
        if why:
            return await interaction.response.send_message(why, ephemeral=True)

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    ACCOUNT_RUNNING.add(interaction.user.id)

    bulk = count >= ACCOUNT_TXT_THRESHOLD   # まとめてtxtで渡すか
    records: list[AccountRecord] = []
    ng_count = 0
    started_at = datetime.now()
    msg = None
    progress_dead = False   # followupトークン切れ等で進捗表示を諦めたらTrue

    async def progress(text: str):
        # 大量作成だとfollowupの期限（15分）を超える。表示が死んでも
        # 作成自体は止めないよう、失敗は静かに無視する
        nonlocal msg, progress_dead
        if progress_dead and msg is None:
            return
        try:
            if msg is None:
                msg = await interaction.followup.send(text, ephemeral=True)
            else:
                await msg.edit(content=text)
        except discord.HTTPException:
            if msg is None:
                progress_dead = True

    stopped_at = None   # 管理者の停止で打ち切ったとき、作らなかった最初の件数目

    def paid_done() -> int:
        return sum(1 for r in records if r.vip_ok)

    # 有料版は、回し放題まで済んだ垢が count 件そろうまで作り直す（失敗が続くときの上限つき）
    tries = count if order is None else count + max(VIP_PAID_RETRY, count // 2)
    try:
        for i in range(1, tries + 1):
            if order is not None and paid_done() >= count:
                break
            head = f'`[{paid_done() + 1 if order is not None else i}/{count}]` '
            if ACCOUNT_PAUSE['paused'] or maint_window():
                stopped_at = i
                break
            async with ACCOUNT_SEM, log_tag(f'account#{i}'):
                if ACCOUNT_PAUSE['paused'] or maint_window():     # 順番待ちの間に止められた
                    stopped_at = i
                    break
                # プロキシを1枠借りる（proxy.txt）。作成〜クリア/ガシャまでの間だけ占有。
                # どの経路で抜けても（失敗・例外）必ず返す
                lease = await lease_proxy()
                try:
                    try:
                        rec = await create_level5_account(
                            lambda t, h=head: progress(h + t),
                            password=fixed_password,          # ← 上書きしない
                            player_name=resolve_player_name(name_tpl, i),
                            proxy=lease.url if lease is not None else None)
                        log_line(f'作成完了 email={rec.email} userId={rec.user_id} '
                                 f'name={rec.save_name} save={"あり" if rec.client else "なし"}'
                                 + (f' via {proxy_pool.mask(lease.url)}' if lease is not None else ''))
                    except Exception as e:
                        ng_count += 1
                        print(f'[account] 作成失敗 user={interaction.user.id}\n'
                              f'{traceback.format_exc()}')
                        await progress(f'{head}❌ {i}件目は失敗: `{str(e)[:200]}`')
                        continue

                    # 久垢はここで初回ステージまでクリアさせる
                    if aged:
                        if rec.client is None:
                            rec.cleared = False
                            rec.clear_error = 'セーブデータが作れなかったためクリアできません'
                            await progress(f'{head}⚠ セーブが無いのでステージをクリアできません')
                        else:
                            await progress(f'{head}⚔ ステージ `{AGED_STAGE_ID}` をクリア中...')
                            log_line(f'久垢クリア開始 stage={AGED_STAGE_ID} email={rec.email}')
                            try:
                                await clear_stage(rec.client, AGED_STAGE_ID,
                                                  lambda t, h=head: progress(h + t))
                                rec.cleared = True
                                log_line('久垢クリア成功')
                                # メニューはここでは開けない（1面だけでは開くものが無い）。
                                # 4/6/8/12面で友だちになると開くので、アプリで進めれば開く。
                                # 2026-09-25 までここで updateTutorialFlg をまとめて送って
                                # いたが、途中の 1|1000 を完了値で上書きしてフラグが落ちた。
                            except Exception as e:
                                rec.cleared = False
                                rec.clear_error = str(e)[:250]
                                print(f'[account] ステージクリア失敗 {rec.email}: {str(e)[:200]}')
                                await progress(f'{head}⚠ ステージクリア失敗: `{str(e)[:150]}`')

                    if vip:
                        log_line(f'回し放題垢 開始 email={rec.email} {vip}連')
                        await make_vip_account(rec, vip, lambda t, h=head: progress(h + t))
                finally:
                    if lease is not None:
                        lease.release()

            # 大量作成でセーブデータを抱え込まないよう、使い終わったら参照を切る
            rec.client = None
            records.append(rec)

            # 少数のときだけ1件ずつ通知する（大量だとDMが埋まるのでtxtにまとめる）
            if not bulk:
                embed = account_result_embed(rec, i, count, kind)
                for send in (lambda: interaction.followup.send(embed=embed, ephemeral=True),
                             lambda: interaction.user.send(embed=embed)):
                    try:
                        await send()
                    except discord.HTTPException:
                        pass
            if order is not None and rec.vip_closed:
                break       # 開催していないので、作り直しても引けない
    except Exception as e:
        print(f'[account] エラー user={interaction.user.id}\n{traceback.format_exc()}')
        try:
            await interaction.followup.send(
                f'❌ アカウント作成中にエラーが発生しました: `{str(e)[:300]}`', ephemeral=True)
        except discord.HTTPException:
            pass
    finally:
        ACCOUNT_RUNNING.discard(interaction.user.id)
        finished_at = datetime.now()

        # 途中で落ちても、作れたぶんは必ず渡す
        if bulk and records:
            try:
                await send_accounts_txt(interaction, records, started_at,
                                        finished_at, kind, ng_count)
            except Exception:
                print(f'[account] txt送信でエラー\n{traceback.format_exc()}')

        summary = (f'🏁 {kind} 作成終了 — ✅ 成功 {len(records)}件'
                   + (f' / ❌ 失敗 {ng_count}件' if ng_count else ''))
        if stopped_at is not None:
            why = 'メンテ' if maint_window() else '管理者の停止'
            made = len(records) if order is not None else stopped_at - 1
            left = count - paid_done() if order is not None else count - stopped_at + 1
            log_line(f'{why}により {kind} 作成を打ち切り: {made}/{count}件で終了')
            summary += (f'\n⏸ {"メンテのため" if why == "メンテ" else "管理者がアカウント作成を停止したため"}、'
                        f'{made}件目までで終了しました（残り {left}件は作っていません）。')
        if aged:
            done = sum(1 for r in records if r.cleared)
            summary += f'\n⚔ ステージ`{AGED_STAGE_ID}`クリア: {done}件 / {len(records)}件'
        if vip:
            done = sum(1 for r in records if r.vip_ok)
            summary += f'\n🎰 第1章クリア＋まわし放題ガシャ: {done}件 / {len(records)}件'
        short = count - paid_done() if order is not None else 0
        if order is not None:
            summary += f'\n🧾 取引番号 `{order}` ／ ご注文 {count}件 → お渡し {paid_done()}件'
            if short > 0:
                summary += (f'\n⚠ **{short}件 足りません。** お手数ですが、この取引番号を添えて'
                            '管理者にご連絡ください。')
        summary += (f'\n📅 {fmt_jp_datetime(started_at)} 〜 {fmt_jp_datetime(finished_at)}')
        if order is not None:
            log_line(f'有料 {kind} 終了 user={interaction.user.id} 取引番号={order} '
                     f'注文{count} お渡し{paid_done()} 作成{len(records)} 失敗{ng_count}')
            # 進捗の表示は15分で消えるので、有料はまとめをDMにも残す
            try:
                await interaction.user.send(summary)
            except discord.HTTPException:
                pass
            if short > 0:
                await notify_owners(
                    f'⚠ 有料 {kind} の不足: {interaction.user} ({interaction.user.id})\n'
                    f'取引番号 `{order}` ／ 注文 {count}件 → お渡し {paid_done()}件（{short}件不足）\n'
                    + '\n'.join(f'・{r.email}: ' + ('OK' if r.vip_ok else
                                                   (r.vip_text or r.clear_error or '不明')[:100])
                                for r in records[-10:]))
        if vip and any(r.vip_ok for r in records):
            total = collections.Counter()
            for r in records:
                total.update(r.vip_tally)
            try:
                await post_result(order is not None, interaction.user,
                                  f'回し放題垢（初期垢を作成して{vip}連）× '
                                  f'{sum(1 for r in records if r.vip_ok)}垢', price, total)
            except Exception:
                print(f'[実績] 投稿でエラー\n{traceback.format_exc()}')
        summary += ('\n📄 メアド/パスワードは .txt にまとめてDMへ送りました。' if bulk and records
                    else '\nメールアドレスとパスワードは上のメッセージとDMで確認してください。')
        await progress(summary)


# ============================================================================
# ステージプリセット（「周回開始」で選択。複数ステージを順番に周回する）
# ============================================================================

PRESETS: dict[str, dict] = {
    # イベントステージ解放用：第1章を最初から順に1周（1002001は第2章の入口）
    'イベステ解放': {
        'aliases': ('第1章',),
        'ids': list(range(1001001, 1001015 + 1)) + [1002001],
        'desc': '第1章 1001001〜1001015 ＋ 1002001 の16ステージを順番に周回'
                '（イベステ解放用）',
    },
    # イベントステージ全51種を順番に周回
    'イベステ全周回': {
        'aliases': ('イベステ',),
        'ids': (list(range(29701001, 29701013 + 1))
                + list(range(29702001, 29702015 + 1))
                + list(range(29703001, 29703017 + 1))
                + list(range(29704001, 29704006 + 1))),
        'desc': 'イベント51種（29701001〜13 / 29702001〜15 / 29703001〜17 / '
                '29704001〜06）を順番に全て周回',
    },
}


def refresh_event_preset():
    """「イベステ全周回」と最深部の面を、今のイベントに合わせる（ywp_auto.current_event_stages）

    イベントが変わると面の番号も変わる（9/30 まで 297xx、10/1 から 298xx）。周回を始めるときに
    毎回呼ぶ。今のイベントが分からなければ何もしない（前の値のまま）。
    """
    global AUTO_DECK_STAGE
    ids, deepest, name = current_event_stages()
    p = PRESETS['イベステ全周回']
    if ids and ids != p['ids']:
        maps = sorted({i // 1000 for i in ids})
        p['ids'] = ids
        p['desc'] = (f'{name or "今のイベント"} の{len(ids)}ステージ（'
                     + ' / '.join(f'{m}' for m in maps) + '）を順番に全て周回')
        log_line(f'イベステ全周回を今のイベントに合わせた: {name} {len(ids)}面 {maps}')
    if deepest and deepest != AUTO_DECK_STAGE:
        log_line(f'最深部の面（自動編成を使える面）: {AUTO_DECK_STAGE} → {deepest}')
        AUTO_DECK_STAGE = deepest


def find_preset(raw: str) -> tuple[str, dict] | None:
    """ステージID欄の文字列がプリセット名/別名なら (名前, 定義) を返す"""
    refresh_event_preset()
    key = raw.strip().replace('　', ' ')
    if not key:
        return None
    for name, p in PRESETS.items():
        if key == name or key in p['aliases']:
            return name, p
    return None


def preset_help() -> str:
    """パネルembed用のプリセット一覧テキスト"""
    refresh_event_preset()
    return '\n'.join(f'　`{name}`（{len(p["ids"])}ステージ）… {p["desc"]}'
                     for name, p in PRESETS.items())


# ============================================================================
# ジョブ
# ============================================================================

class FarmJob:
    """1ユーザー分の周回ジョブ。メール/パスワードはここには持たせない。"""

    def __init__(self, user: discord.abc.User, stage_id: int, count: int | None,
                 battle_time: float, cooldown: float,
                 target_key: str | None = None, target_value: int | None = None,
                 target_delta: int | None = None,
                 stage_ids: list[int] | None = None,
                 preset_name: str | None = None):
        self.user = user
        self.jid = 1                # ユーザー内でのジョブ番号（#1, #2...）
        # プリセット（複数ステージを順番に周回）。単一指定なら [stage_id] と同じ
        self.stage_ids = list(stage_ids) if stage_ids else [stage_id]
        self.preset_name = preset_name
        self.stage_pos = 0          # 完結したバトル数＝リスト内の進行位置
        self.stage_id = stage_id    # 現在周回中のステージ（= stage_ids[stage_pos % n]）
        self.count = count            # None=無限（打ち切りなし）。目標指定時は「これ以上は回さない」上限
        self.battle_time = battle_time
        self.cooldown = cooldown

        # 目標指定（'ypoint' / 'ymoney'）。target_delta は開始時の値に足して確定させる
        self.target_key = target_key
        self.target_value = target_value
        self.target_delta = target_delta

        self.cancel = asyncio.Event()
        # 稼働中は立っている。下ろすと次のバトルを始める前で待機に入る
        # （開始済みのバトルは必ず閉じてから止まる）
        self.running = asyncio.Event()
        self.running.set()
        self.paused_total = 0.0     # 一時停止していた累計秒。経過時間から除く
        self.paused_at = 0.0        # 今の一時停止に入った時刻（0なら停止中でない）
        self.pauses = 0             # 一時停止した回数
        self.status = '待機中'
        self.done = 0
        self.success = 0
        self.fail = 0
        self.event_point = 0
        self.hitodama_buys = 0      # ひとだま自動購入を実行した回数
        # ドロップと自動編成（AUTO_DECK_STAGE を単独で周回するときだけ聞く）
        self.drop_asked = False     # 聞いたか（聞いて無効なら、この面では友だち申告もしない）
        self.auto_deck = False
        self.befriends = 0          # 仲間になった回数（どのジョブでも数える）
        self.befriend_counts: collections.Counter = collections.Counter()   # 妖怪ID→回数
        self.item_counts: collections.Counter = collections.Counter()       # アイテムID→個数
        self.item_prev: dict | None = None      # 前回の gameEnd 時点の持ち物（差分用）
        # 好物（あげた回数・そのうち仲間になった回数・買った個数と Yマネー）とガチャ
        self.food_fed = 0
        self.food_won = 0
        self.food_bought = 0
        self.food_spent = 0
        self.gacha_youkai: list = []            # ガチャで出た妖怪ID（引いた順）
        self.food_mode = 'off'      # 自動編成の面で好物を使うか（FOOD_MODES。開始時に聞く）
        # 硬い面の手前で垢を育てる（start_grind）
        self.grind_stage: int | None = None     # 育成中に回している面
        self.grind_left = 0                     # 育った垢まであと何勝
        self.grind_for: int | None = None       # この面が硬かったので下がった
        self.grind_home: int | None = None      # 単独周回で、終わったら戻る面
        self.grind_battles = 0                  # 育成に使った戦数（周回数に数えない）
        # レベル上限の自動解放（周回開始時に聞く）とミッション報酬の自動受け取り
        self.auto_levelup = False
        self.level_unlocks = 0
        self.level_unlock_ymoney = 0
        self.level_open_skip: set = set()       # 解放しないと決めた (妖怪ID, レベル)
        self.level_wait_logged = False          # ひとだま優先で見送ったことをログに出したか
        self.mission_receipts = 0
        self.mission_ymoney = 0
        self.present_receipts = 0       # 郵便をまとめて受け取った件数
        self.last_mission_at = 0.0
        # 再起動をまたいで続けるための情報（jobs_state.json に保存。パスワードは持たない）
        self.resume_email: str | None = None   # ログイン用。udkeys.json の端末IDを引く鍵
        self.save_uid = None                   # 周回しているセーブの userId
        self.resumed = False                   # 再起動後に復元したジョブか
        self.resume_elapsed = 0.0              # 復元時点までの稼働秒
        self.interrupted = False               # Botの停止で中断した（復元待ち）
        self.proxy_lease = None                # 周回中に占有するプロキシの枠（proxy.txt）
        # 編成（先頭がリーダー）と各妖怪のレベル。開始時のレベルは上がり幅の表示用
        self.deck_order: list[str] = []
        self.deck_levels: dict[str, int] = {}
        self.deck_levels_start: dict[str, int] = {}
        self.leader_sets = 0        # リーダーにした回数
        self.last_leader = None     # 最後にリーダーにした妖怪ID
        self.deck_sorts = 0         # デッキを強い順に組み直した回数
        self.player_name = None
        self.last_error = None
        self.started_at = time.time()

        # 所持数。セーブはログイン時にしか降ってこないので、節目ごとに取り直す
        self.start_points: dict[str, int] = {}
        self.now_points: dict[str, int] = {}
        self.points_at = 0.0          # now_points を取った時刻
        self.points_done = 0          # now_points を取った時点の周回数
        self.refreshes = 0
        self.want_refresh = False     # 「更新」が押されたら次の周回前に取り直す
        self.refreshed = asyncio.Event()   # 取り直しが済んだら立つ（更新ボタンの待ち合わせ）
        self.save_shape_prev: dict = {}    # 前回取り直したときのセーブ指紋（進行の差分用）
        self.save_detail_prev: dict = {}   # 同・進行フラグの中身（行数では動かないもの）

        self.message: discord.Message | None = None   # DMの進捗メッセージ
        self.task: asyncio.Task | None = None         # GC防止のため参照を保持
        self._last_edit = 0.0

    @property
    def is_preset(self) -> bool:
        return len(self.stage_ids) > 1

    @property
    def total_battles(self) -> int | None:
        """予定バトル数（無限モードは None）。プリセットは 周回数×ステージ数"""
        if self.count is None:
            return None
        return self.count * len(self.stage_ids)

    @property
    def unit(self) -> str:
        """進捗表示の単位。プリセットは1バトル=1ステージなので「戦」を使う"""
        return '戦' if self.is_preset else '周'

    def stage_display(self) -> str:
        """進捗・結果表示用のステージ表記"""
        grind = (f'\n📈 垢を育成中 `{self.grind_stage}`（育つまであと{self.grind_left}勝・'
                 f'`{self.grind_for}` が硬いため）' if self.grind_left > 0 and self.grind_stage
                 else '')
        if not self.is_preset:
            return f'`{self.grind_home or self.stage_id}`' + grind
        n = len(self.stage_ids)
        lap, idx = divmod(self.stage_pos, n)
        name = f'プリセット「{self.preset_name}」' if self.preset_name else '連続周回'
        return f'{name}\n`{self.stage_ids[idx]}`（{idx + 1}/{n}・{lap + 1}周目）' + grind

    @property
    def is_paused(self) -> bool:
        return not self.running.is_set()

    @property
    def paused_seconds(self) -> float:
        """一時停止していた累計秒（今まさに停止中ならその分も含む）"""
        now = self.paused_total
        if self.paused_at:
            now += time.time() - self.paused_at
        return now

    @property
    def elapsed(self) -> float:
        """稼働していた秒数。一時停止していた時間は含めない

        per_battle / ETA がここから出るので、止めていた分を混ぜると
        1周あたりの所要が実態とかけ離れる。
        """
        return max(0.0, time.time() - self.started_at - self.paused_seconds)

    @property
    def per_battle(self) -> float:
        """1周あたりの所要秒。実測が溜まるまでは設定値から見積もる"""
        if self.done >= 3:
            return self.elapsed / self.done
        avg_b = 1.0 + BATTLE_TIME_SPREAD / 2   # jitter の期待値倍率（バトル）
        avg_c = 1.0 + TIME_SPREAD / 2          # 同（クールダウン）
        est = self.battle_time * avg_b + self.cooldown * avg_c
        # 連戦間隔の下限（MIN_BATTLE_CYCLE）を下回らないように見積もる
        return max(est, MIN_BATTLE_CYCLE) + REQUEST_DELAY

    def gained(self, key: str) -> int | None:
        """開始時からの増加量。まだ測れないなら None"""
        if key not in self.start_points or key not in self.now_points:
            return None
        return self.now_points[key] - self.start_points[key]

    @property
    def target_reached(self) -> bool:
        if self.target_key is None or self.target_value is None:
            return False
        now = self.now_points.get(self.target_key)
        return now is not None and now >= self.target_value

    @property
    def remaining(self) -> int | None:
        """残り周回数の見込み。無限モードや目標指定で実績が足りないうちは None（＝不明）"""
        total = self.total_battles
        cleared = self.stage_pos if self.is_preset else self.done
        capped = None if total is None else max(total - cleared, 0)
        if self.target_key is None or self.target_value is None:
            return capped
        now = self.now_points.get(self.target_key)
        gained = self.gained(self.target_key)
        # ペースは「所持数を取り直した時点の周回数」で割る。現在の done で割ると
        # 取り直しの合間に分母だけ増えてペースが下がり、見積もりが伸びていく
        if now is None or not gained or gained <= 0 or self.points_done <= 0:
            return None
        per = gained / self.points_done               # 1周あたりの増加量
        # 取り直してから進んだぶんは実測がないので、これまでのペースで補う
        est = now + per * max(self.done - self.points_done, 0)
        need = self.target_value - est
        if need <= 0:
            return 0
        need_rounds = math.ceil(need / per)
        return need_rounds if capped is None else min(need_rounds, capped)

    @property
    def eta(self) -> float | None:
        """完了までの残り秒。見込めないときは None"""
        rest = self.remaining
        return None if rest is None else rest * self.per_battle


# ============================================================================
# ジョブ管理（1ユーザーで複数ジョブ＝複垢代行）とエラー判定
# ============================================================================

def jobs_of(user_id: int) -> list[FarmJob]:
    return JOBS.get(user_id) or []


def add_job(job: FarmJob) -> int:
    """ジョブを登録し、ユーザー内で一意の番号（#1, #2...）を割り当てる"""
    running = jobs_of(job.user.id)
    job.jid = max((j.jid for j in running), default=0) + 1
    JOBS.setdefault(job.user.id, []).append(job)
    return job.jid


def drop_job(job: FarmJob):
    lst = JOBS.get(job.user.id)
    if lst is None:
        return
    if job in lst:
        lst.remove(job)
    if not lst:
        JOBS.pop(job.user.id, None)


def find_job(user_id: int, jid=None) -> FarmJob | None:
    """実行中ジョブの検索。jid省略時は最初の1件（旧「中止」ボタン互換用）"""
    for j in jobs_of(user_id):
        if jid is None or str(j.jid) == str(jid):
            return j
    return None


class FarmStop(Exception):
    """周回を即座に打ち切るべき状態（ひとだまの購入失敗など）"""


def hitodama_total(points: dict) -> int | None:
    """ひとだま＋無料ひとだまの合計。どちらも読めなければ None"""
    vals = [points.get(k) for k in ('hitodama', 'freeHitodama')]
    if all(v is None for v in vals):
        return None
    return sum(v for v in vals if v is not None)


def sync_hitodama(job: FarmJob, c: Client, res):
    """バトルの応答に載っている最新の ywp_user_data から、ひとだまの残数を拾う

    gameStart / gameEnd の応答には毎回 ywp_user_data が入っている（2026-09-27 実機）。
    """
    d = res.get('ywp_user_data') if isinstance(res, dict) else None
    if d is None:
        return
    c.save['ywp_user_data'] = d
    try:
        info = c.info()
    except (ValueError, TypeError, AttributeError):
        return
    for k in ('hitodama', 'freeHitodama', 'ymoney'):
        v = info.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            job.now_points[k] = int(v)


def is_hitodama_shortage(js) -> bool:
    """サーバー応答がひとだま不足エラーか（dialogMsg/dialogTitle に含まれるかで判定）"""
    if not isinstance(js, dict):
        return False
    text = f"{js.get('dialogTitle') or ''} {js.get('dialogMsg') or ''}"
    return 'ひとだま' in text


def hitodama_shortage_error(js) -> str:
    extra = ''
    if isinstance(js, dict):
        msg = (js.get('dialogMsg') or js.get('dialogTitle') or '').strip()
        if msg:
            extra = f'\nサーバー通知: {msg[:150]}'
        elif js.get('resultCode') is not None:
            extra = f"\n(resultCode={js.get('resultCode')})"
    return ('🔥 ひとだまが足りずバトルを開始できません。強制停止しました。\n'
            'ひとだまを購入・回復してから再開してください。' + extra)


def retry_wait(job: FarmJob, errs: int) -> float:
    """エラー再試行までの待ち秒。失敗が続くほど長く取る（上限30秒）"""
    return min(jitter(job.cooldown, COOLDOWN_MIN) * (1.0 + 0.5 * errs), 30.0)


TARGET_WORDS = (('ポイント', 'ypoint'), ('ぽいんと', 'ypoint'),
                ('マネー', 'ymoney'), ('まねー', 'ymoney'))


# 無限周回（周回数上限なし）として解釈する語
ENDLESS_WORDS = {'無限', 'むげん', '∞', 'inf', 'infinite', 'endless'}


def parse_count_or_target(raw: str) -> tuple[int | None, str | None, int | None, int | None]:
    """周回数欄の入力を解釈する

      '100'      → 100周
      '無限'     → 中止するまで回し続ける（周回数上限なし）
      'P12000'   → Yポイントが12000になるまで
      'P+3000'   → Yポイントが3000増えるまで
      'M+50000'  → Yマネーが50000増えるまで

    戻り値 (上限周回数, 目標キー, 目標の絶対値, 目標の増加量)
    上限周回数 None は無限（打ち切りなし）を表す。
    """
    s = unicodedata.normalize('NFKC', raw or '').strip().replace(',', '').replace(' ', '')
    if not s:
        raise ValueError('周回数が空です')
    if s.lower() in ENDLESS_WORDS:
        return None, None, None, None

    key, body = None, s
    for word, k in TARGET_WORDS:
        if body.startswith(word):
            key, body = k, body[len(word):]
            break
    if key is None and body[:1].lower() in ('p', 'm'):
        key = 'ypoint' if body[0].lower() == 'p' else 'ymoney'
        body = body[1:]

    is_delta = body.startswith('+')
    if is_delta:
        body = body[1:]
    if not body.isdigit():
        raise ValueError('`100`（周回数）か `P12000` / `P+3000`（目標）の形式で入力してください')
    n = int(body)

    if key is None:
        if n <= 0:
            raise ValueError('周回数は1以上の整数（または「無限」）で入力してください')
        return n, None, None, None
    if n <= 0:
        raise ValueError('目標値は1以上で入力してください')
    return None, key, (None if is_delta else n), (n if is_delta else None)


def fmt_sec(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f'{h}時間{m}分{s}秒' if h else (f'{m}分{s}秒' if m else f'{s}秒')


def progress_bar(done: int, total: int, width: int = 16) -> str:
    if total <= 0:
        return '─' * width
    filled = int(width * done / total)
    return '█' * filled + '░' * (width - filled)


WALLET_LABEL = {name: label for name, label, _ in Client.WALLET}
WALLET_ICON = {'ymoney': '💰', 'ypoint': '🔷', 'hitodama': '🔥'}
YPOINT_ITEM = next(src for name, _, src in Client.WALLET if name == 'ypoint')


def set_player_footer(e: discord.Embed, job: FarmJob):
    """フッターにプレイヤー名と所持数の取得時刻を出す

    所持数はセーブを取り直したときの値なので、いつ時点なのかを明示する。
    """
    if not job.player_name:
        return
    text = f'プレイヤー: {job.player_name}'
    if job.points_at:
        text += f' ／ 所持数は{fmt_sec(time.time() - job.points_at)}前の時点'
    e.set_footer(text=text)


def hourly_text(job: FarmJob, key: str) -> str:
    """開始からの増加量を、所持数を取った時点までの稼働時間（一時停止を除く）で割った毎時"""
    diff = job.gained(key)
    sec = (job.points_at or time.time()) - job.started_at - job.paused_seconds
    if diff is None or sec < 60:
        return '毎時 計測中'
    return f'毎時 **{diff * 3600 / sec:+,.0f}**'


def add_wallet_fields(e: discord.Embed, job: FarmJob):
    """所持数（現在／開始時／増減）を並べる"""
    for key in ('ymoney', 'ypoint', 'hitodama'):
        now, before = job.now_points.get(key), job.start_points.get(key)
        if now is None and before is None:
            continue
        icon, label = WALLET_ICON.get(key, ''), WALLET_LABEL.get(key, key)
        if now is None:
            value = f'{before:,}\n(開始時)'
        else:
            value = f'**{now:,}**'
            diff = job.gained(key)
            if diff is not None:
                # ひとだまは消費で減るので符号をそのまま出す
                value += f'\n{before:,} → {diff:+,}'
            if key == 'ypoint':
                value += '\n' + hourly_text(job, key)
        e.add_field(name=f'{icon} {label}', value=value, inline=True)


def job_headline(job: FarmJob) -> str:
    """進捗バーの行

    周回数指定なら周回数、目標指定なら目標値を基準にバーを出す。
    目標指定で周回数を基準にすると、上限に対する比になって
    ほとんど進まないバーになるため。無限モードはバーなしで周回数のみ。
    """
    if job.target_key is None or job.target_value is None:
        if job.count is None:
            return f'♾️ **{job.done}{job.unit}**（無限モード）'
        if job.is_preset:
            lap = min(job.stage_pos // len(job.stage_ids) + 1, job.count)
            return (f'`{progress_bar(job.stage_pos, job.total_battles)}` '
                    f'**{job.stage_pos}/{job.total_battles}戦 クリア**（{lap}/{job.count}周目）'
                    + (f'・失敗してやり直し {job.fail}回' if job.fail else ''))
        return f'`{progress_bar(job.done, job.count)}` **{job.done}/{job.count}周**'

    label = WALLET_LABEL.get(job.target_key, job.target_key)
    now = job.now_points.get(job.target_key)
    start = job.start_points.get(job.target_key)
    if now is None or start is None or job.target_value <= start:
        bar = progress_bar(0, 0)
        head = f'🎯 {label} **{job.target_value:,}** まで'
    else:
        bar = progress_bar(now - start, job.target_value - start)
        head = (f'🎯 {label} **{now:,} / {job.target_value:,}**'
                f'（あと {max(job.target_value - now, 0):,}）')
    if job.count is None:
        cap = '（周回数上限なし）'
    else:
        cap = (f'（上限 {job.count}周・全{job.total_battles}戦）' if job.is_preset
               else f'（上限 {job.count}周）')
    return f'`{bar}` {head}\n{job.done}{job.unit} 実行済み{cap}'


def eta_text(job: FarmJob) -> str:
    if job.count is None and job.target_key is None:
        return '無限\n（中止で停止）'
    rest, eta = job.remaining, job.eta
    if eta is None:
        return '計測中'
    when = datetime.fromtimestamp(time.time() + eta).strftime('%H:%M')
    return f'約{fmt_sec(eta)}\n({when}ごろ / 残り{rest}{job.unit})'


def progress_embed(job: FarmJob) -> discord.Embed:
    e = discord.Embed(
        title=('⏸ 自動周回 一時停止中 #%d' % job.jid if job.is_paused
               else f'🎮 自動周回 実行中 #{job.jid}'),
        description=job_headline(job),
        color=COLOR_WARN if job.is_paused else COLOR_RUN,
        timestamp=datetime.now(),
    )
    e.add_field(name='状態', value=job.status, inline=True)
    e.add_field(name='ステージ', value=job.stage_display(), inline=True)
    e.add_field(name='経過', value=fmt_sec(job.elapsed), inline=True)
    add_wallet_fields(e, job)
    e.add_field(name='✅ 成功', value=f'{job.success}回', inline=True)
    e.add_field(name='❌ 失敗', value=f'{job.fail}回', inline=True)
    e.add_field(name='🎪 イベントP', value=f'{job.event_point:,}', inline=True)
    e.add_field(name='⏳ 完了予定',
                value='—（一時停止中）' if job.is_paused else eta_text(job), inline=True)
    if job.paused_seconds >= 1:
        e.add_field(name='⏸ 一時停止',
                    value=f'{job.pauses}回 / 計 {fmt_sec(job.paused_seconds)}', inline=True)
    if job.befriends or job.auto_deck:
        e.add_field(name='🤝 仲間になった妖怪',
                    value=befriend_text(job) if job.befriends else 'まだいません',
                    inline=False)
    if job.item_counts:
        e.add_field(name='🎁 手に入れたアイテム', value=items_text(job), inline=False)
    if job.deck_order:
        e.add_field(name='🧩 編成（レベル・開始からの上がり幅）', value=deck_text(job)
                    + (f'\n強い順に組み直した {job.deck_sorts}回' if job.deck_sorts else ''),
                    inline=False)
    if job.auto_levelup or job.level_unlocks:
        e.add_field(name='📈 レベル上限の自動解放',
                    value=(f'{job.level_unlocks}回（Yマネー -{job.level_unlock_ymoney:,}）'
                           if job.level_unlocks else 'まだありません'), inline=True)
    if job.food_fed or job.food_bought or (job.auto_deck and job.food_mode != 'off'):
        e.add_field(name='🍙 好物', value=food_text(job), inline=True)
    if job.gacha_youkai:
        e.add_field(name='🎰 ガチャ', value=gacha_text(job), inline=False)
    if job.mission_receipts:
        e.add_field(name='🎯 ミッション報酬',
                    value=f'{job.mission_receipts}件受け取り（Yマネー +{job.mission_ymoney:,}）',
                    inline=True)
    if job.present_receipts:
        e.add_field(name='📮 郵便', value=f'{job.present_receipts}件受け取り', inline=True)
    set_player_footer(e, job)
    return e


def result_embed(job: FarmJob, aborted: bool = False) -> discord.Embed:
    num = f' #{job.jid}'
    if job.last_error:
        title, color = '❌ 自動周回 失敗' + num, COLOR_NG
    elif job.cancel.is_set():
        title, color = '⏹ 自動周回 中止' + num, COLOR_WARN
    elif aborted or job.fail:
        title, color = '⚠ 自動周回 完了（一部失敗）' + num, COLOR_WARN
    else:
        title, color = '✅ 自動周回 完了' + num, COLOR_OK

    rate = (job.success / job.done * 100) if job.done else 0.0
    e = discord.Embed(
        title=title,
        description=job_headline(job),
        color=color,
        timestamp=datetime.now(),
    )
    e.add_field(name='ステージ', value=job.stage_display(), inline=True)
    e.add_field(name='✅ 成功', value=f'{job.success}回', inline=True)
    e.add_field(name='❌ 失敗', value=f'{job.fail}回', inline=True)
    add_wallet_fields(e, job)
    e.add_field(name='成功率', value=f'{rate:.1f}%', inline=True)
    e.add_field(name='🎪 イベントP', value=f'{job.event_point:,}', inline=True)
    e.add_field(name='所要時間', value=fmt_sec(job.elapsed), inline=True)
    if job.hitodama_buys:
        e.add_field(name='🛒 ひとだま自動購入',
                    value=f'+{job.hitodama_buys:,}個', inline=True)
    if job.befriends or job.auto_deck:
        e.add_field(name='🤝 仲間になった妖怪',
                    value=befriend_text(job) if job.befriends else 'まだいません',
                    inline=False)
    if job.item_counts:
        e.add_field(name='🎁 手に入れたアイテム', value=items_text(job), inline=False)
    if job.deck_order:
        e.add_field(name='🧩 編成（レベル・開始からの上がり幅）', value=deck_text(job)
                    + (f'\n強い順に組み直した {job.deck_sorts}回' if job.deck_sorts else ''),
                    inline=False)
    if job.auto_levelup or job.level_unlocks:
        e.add_field(name='📈 レベル上限の自動解放',
                    value=(f'{job.level_unlocks}回（Yマネー -{job.level_unlock_ymoney:,}）'
                           if job.level_unlocks else 'まだありません'), inline=True)
    if job.food_fed or job.food_bought or (job.auto_deck and job.food_mode != 'off'):
        e.add_field(name='🍙 好物', value=food_text(job), inline=True)
    if job.gacha_youkai:
        e.add_field(name='🎰 ガチャ', value=gacha_text(job), inline=False)
    if job.mission_receipts:
        e.add_field(name='🎯 ミッション報酬',
                    value=f'{job.mission_receipts}件受け取り（Yマネー +{job.mission_ymoney:,}）',
                    inline=True)
    if job.present_receipts:
        e.add_field(name='📮 郵便', value=f'{job.present_receipts}件受け取り', inline=True)
    if job.last_error:
        e.add_field(name='エラー', value=f'```{job.last_error[:900]}```', inline=False)
    set_player_footer(e, job)
    return e


async def update_progress(job: FarmJob, force: bool = False):
    """DMの進捗メッセージを編集

    周回中は自動更新しない。毎周編集するとDiscordのレート制限に当たるため、
    節目（受付・周回開始・中止・完了）だけ編集して、それ以外は
    「🔄 更新」ボタンが押されたときだけ更新する。

    force=False の呼び出しは何もしない。周回ループ側に点在する進捗通知を
    まとめて黙らせるため、呼び出し側は残したままここで止めている。
    """
    if job.message is None or not force:
        return
    now = time.time()
    if now - job._last_edit < PROGRESS_INTERVAL:
        return          # 節目が連続したときの保険
    job._last_edit = now
    try:
        # viewは作成時に付けた永続Viewをそのまま残す
        await job.message.edit(embed=progress_embed(job))
    except discord.HTTPException:
        pass


async def push_progress(job: FarmJob):
    """「🔄 更新」用。間引きを無視して必ず編集する"""
    if job.message is None:
        return
    job._last_edit = time.time()
    try:
        await job.message.edit(embed=progress_embed(job))
    except discord.HTTPException:
        pass


async def report_error(interaction: discord.Interaction, error: Exception, where: str):
    print(f'[{where}] {error}\n{traceback.format_exc()}')
    msg = f'❌ エラーが発生しました: `{str(error)[:200]}`'
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


async def sleep_or_cancel(job: FarmJob, sec: float) -> bool:
    """sec秒待つ。途中で中止されたらTrueを返して即座に抜ける。"""
    if sec <= 0:
        return job.cancel.is_set()
    try:
        await asyncio.wait_for(job.cancel.wait(), timeout=sec)
        return True
    except asyncio.TimeoutError:
        return False


def pause_job(job: FarmJob) -> bool:
    """一時停止を受け付ける。既に停止中なら False"""
    if job.is_paused:
        return False
    job.running.clear()
    job.paused_at = time.time()
    job.pauses += 1
    persist_jobs()
    return True


def resume_job(job: FarmJob) -> bool:
    """再開する。停止していなければ False"""
    if not job.is_paused:
        return False
    if job.paused_at:
        job.paused_total += time.time() - job.paused_at
        job.paused_at = 0.0
    job.running.set()
    persist_jobs()
    return True


# ============================================================================
# 再起動をまたいで周回を続ける
#   動いているジョブの設定と進み具合を1戦ごとに jobs_state.json へ保存し、
#   起動したら同じ番号で復元して続きから回す（一時停止中だったものは一時停止のまま）。
#   パスワードは保存しない。ログインは udkeys.json の端末ID（メールで引く）で行う。
# ============================================================================

JOBS_STATE_FILE = os.path.join(BASE_DIR, 'jobs_state.json')
_JOB_KEYS = ('jid', 'stage_id', 'stage_ids', 'preset_name', 'count', 'battle_time',
             'cooldown', 'target_key', 'target_value', 'drop_asked', 'auto_deck',
             'done', 'success', 'fail', 'stage_pos', 'event_point', 'hitodama_buys',
             'befriends', 'leader_sets', 'last_leader', 'deck_sorts', 'pauses', 'refreshes',
             'player_name', 'start_points', 'deck_levels_start',
             'auto_levelup', 'level_unlocks', 'level_unlock_ymoney',
             'mission_receipts', 'mission_ymoney', 'present_receipts', 'food_fed', 'food_won', 'food_bought',
             'food_spent', 'gacha_youkai', 'food_mode', 'grind_stage', 'grind_left',
             'grind_for', 'grind_home', 'grind_battles')
_RESTORED = False


def job_state(job: FarmJob) -> dict | None:
    """保存する中身。ログイン前（続きから回すのに要る情報がまだ無い）なら None"""
    if not job.resume_email or job.save_uid is None:
        return None
    st = {k: getattr(job, k) for k in _JOB_KEYS}
    st.update(user_id=job.user.id, email=job.resume_email, save_uid=job.save_uid,
              elapsed=job.elapsed, paused=job.is_paused, paused_total=job.paused_seconds,
              befriend_counts={str(k): v for k, v in job.befriend_counts.items()},
              item_counts=dict(job.item_counts), saved_at=time.time())
    return st


def persist_jobs():
    """動いているジョブを全部書き出す（終わった・中止したものは消える）"""
    data = []
    for lst in JOBS.values():
        for j in lst:
            if j.cancel.is_set() or j.interrupted:
                continue
            st = job_state(j)
            if st is not None:
                data.append(st)
    tmp = JOBS_STATE_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, JOBS_STATE_FILE)
    except (OSError, TypeError, ValueError) as e:
        print(f'[job] ジョブ状態の保存に失敗: {e}')


async def restore_jobs():
    """起動時に1回だけ。保存されていたジョブを復元して続きから回す"""
    global _RESTORED
    if _RESTORED:
        return
    _RESTORED = True
    try:
        with open(JOBS_STATE_FILE, 'r', encoding='utf-8') as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(saved, list) or not saved:
        return
    for st in saved:
        try:
            user = await bot.fetch_user(int(st['user_id']))
        except (discord.HTTPException, KeyError, ValueError) as e:
            log_line(f'ジョブ復元: ユーザーを取れないので諦める {st.get("user_id")}: {e}')
            continue
        job = FarmJob(user, st['stage_id'], st['count'], st['battle_time'], st['cooldown'],
                      target_key=st.get('target_key'), target_value=st.get('target_value'),
                      stage_ids=st.get('stage_ids'), preset_name=st.get('preset_name'))
        for k in _JOB_KEYS:
            if k in st:
                setattr(job, k, st[k])
        job.target_delta = None                     # 増加量指定は開始時に絶対値へ確定済み
        job.befriend_counts = collections.Counter(
            {int(k): v for k, v in (st.get('befriend_counts') or {}).items()})
        job.item_counts = collections.Counter(st.get('item_counts') or {})
        job.resume_email, job.save_uid = st['email'], st['save_uid']
        job.resumed, job.resume_elapsed = True, float(st.get('elapsed') or 0)
        job.paused_total = float(st.get('paused_total') or 0)
        if st.get('paused'):
            job.running.clear()
            job.paused_at = time.time()
        job.status = '♻ 再起動後の復元'
        JOBS.setdefault(user.id, []).append(job)    # 番号（jid）はそのまま
        where = f'{job.done}戦目まで済み' + ('・**一時停止のまま**復元（▶ 再開で続きから）'
                                        if job.is_paused else '・続きから再開します')
        log_line(f'ジョブ復元 user={user.id} #{job.jid} stage={job.stage_ids} {where}')
        try:
            await user.send(f'♻ Botが再起動したので、周回 **#{job.jid}** を復元しました（{where}）。\n'
                            '以前の進捗メッセージのボタンは使えないので、下の新しいメッセージを使ってください。')
            job.message = await user.send(embed=progress_embed(job), view=CancelView(job))
        except discord.HTTPException:
            pass
        job.task = asyncio.create_task(
            run_job(job, job.resume_email, None, str(job.save_uid)))
    persist_jobs()


async def wait_while_paused(job: FarmJob) -> bool:
    """一時停止中なら再開（か中止）まで待つ。中止されたら True

    バトルを開始する前にだけ呼ぶ。開始済みのバトルを途中で止めると
    gameEnd を送れずステージが開きっぱなしになるため。
    """
    if not job.is_paused:
        return job.cancel.is_set()

    before = job.status
    job.status = '⏸ 一時停止中（▶ 再開 で続行）'
    await update_progress(job, force=True)

    waiters = [asyncio.create_task(job.running.wait()),
               asyncio.create_task(job.cancel.wait())]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in waiters:
            t.cancel()

    # 中止で抜けた場合もここで停止時間を精算しておく
    resume_job(job)
    if job.cancel.is_set():
        return True
    job.status = before if before and '一時停止' not in before else '⚔ 周回中'
    await update_progress(job, force=True)
    return False


async def recover_token(job: FarmJob, c: Client, save_uid,
                        attempt: int) -> bool:
    """rc=32（tokenズレ）時に同じセーブでログインし直してtokenを取り直す

    同じUDkeyで別のセッション（検証パネルのボタンや別プロセスからのログイン）が
    先に動くと、手元のtokenが無効化されてrc=32になる。loginし直せば新しいtokenが
    発行されるので大抵は復帰できる。ログインに失敗したら False。
    """
    job.status = f'🔄 rc=32 (tokenズレ) → 再ログイン中... ({attempt}/{TOKEN_RETRY})'
    await update_progress(job, force=True)
    try:
        await asyncio.to_thread(c.login, save_uid)
    except Exception as e:
        job.last_error = ('rc=32 (tokenズレ) からの再ログインに失敗したため中止しました: '
                          f'{str(e)[:200]}')
        return False
    return True


# ============================================================================
# 周回本体
# ============================================================================

YOUKAI_MASTER_FILE = os.path.join(BASE_DIR, 'data', 'ywp_mst_youkai.txt')
_YOUKAI_NAMES: dict[int, str] | None = None


def youkai_label(yid) -> str:
    """妖怪IDを「名前（ID）」にする。名前は手元の妖怪マスタ（ywp_mst_youkai の保存分）から引く

    保存より後に追加された妖怪は載っていないので、そのときはIDだけ返す。
    """
    global _YOUKAI_NAMES
    if not yid:
        return 'なし'
    if _YOUKAI_NAMES is None:
        _YOUKAI_NAMES = {}
        try:
            with open(YOUKAI_MASTER_FILE, 'r', encoding='utf-8') as f:
                for m in re.finditer(r'(?:^|\*)(\d{7})\|([^|*]+)\|', f.read()):
                    _YOUKAI_NAMES.setdefault(int(m.group(1)), m.group(2).strip())
        except OSError as e:
            print(f'[youkai] 妖怪マスタを読めません（IDで表示します）: {e}')
    name = _YOUKAI_NAMES.get(int(yid))
    return f'{name}（{yid}）' if name else f'`{yid}`'


ITEM_MASTER_FILE = os.path.join(BASE_DIR, 'data', 'ywp_mst_item.txt')
_ITEM_NAMES: dict[str, str] | None = None


def item_label(item_id) -> str:
    """アイテムIDを名前にする（ywp_mst_item の保存分: 'ID|種類|名前|…'）。無ければID"""
    global _ITEM_NAMES
    if _ITEM_NAMES is None:
        _ITEM_NAMES = {}
        try:
            with open(ITEM_MASTER_FILE, 'r', encoding='utf-8') as f:
                for m in re.finditer(r'(?:^|\*)(\d{5})\|\d+\|([^|*]+)\|', f.read()):
                    _ITEM_NAMES.setdefault(m.group(1), m.group(2).strip())
        except OSError as e:
            print(f'[item] アイテムマスタを読めません（IDで表示します）: {e}')
    return _ITEM_NAMES.get(str(item_id)) or f'`{item_id}`'


def _inventory(v) -> dict[str, int]:
    out = {}
    for r in rows(v):
        if len(r) >= 2:
            try:
                out[r[0]] = int(r[1])
            except ValueError:
                pass
    return out


def on_items(job: FarmJob, c: Client, result: dict):
    """gameEnd の後に呼ぶ。応答の ywp_user_item（持ち物全部）と前回の差で、手に入れた分を数える

    ywp_user_item はログイン時と同じ全件が返る（2026-09-27 実機で一致を確認）ので、
    敵のドロップも報酬もまとめて拾える。使って減ったぶんは数えない。
    """
    now = result.get('ywp_user_item') if isinstance(result, dict) else None
    if now is None:
        return
    now = _inventory(now)
    # Yポイントはアイテム（80102。0個だと行が無い）。毎戦ここで最新にする
    # （ひとだま・Yマネーは sync_hitodama が同じ gameEnd の ywp_user_data から拾う）
    job.now_points['ypoint'] = now.get(str(YPOINT_ITEM), 0)
    job.points_at = time.time()
    job.points_done = job.done
    if job.item_prev is None:
        job.item_prev = _inventory(c.save.get('ywp_user_item'))
    for k, n in now.items():
        gain = n - job.item_prev.get(k, 0)
        if gain > 0:
            job.item_counts[k] += gain
    job.item_prev = now


def items_text(job: FarmJob) -> str:
    total = sum(job.item_counts.values())
    top = ' / '.join(f'{item_label(k)}×{n}' for k, n in job.item_counts.most_common(5))
    more = len(job.item_counts) - 5
    return (f'**{total}個**（{len(job.item_counts)}種）：{top}'
            + (f' ほか{more}種' if more > 0 else ''))


def youkai_name(yid) -> str:
    """名前だけ（無ければID）。編成の表示など短くしたいところ用"""
    label = youkai_label(yid)
    return label.split('（')[0] if '（' in label else label


def _levels(v) -> dict[str, int]:
    """ywp_user_youkai（_diff）の行から {妖怪ID: レベル}。2列目がレベル"""
    out = {}
    for r in rows(v):
        if len(r) >= 2:
            try:
                out[r[0]] = int(r[1])
            except ValueError:
                pass
    return out


def track_deck(job: FarmJob, c: Client, start: dict | None = None,
               result: dict | None = None):
    """編成の並びとレベルを追う

    並びは gameStart の userYoukaiList（デッキ順）、レベルはログイン時の
    ywp_user_youkai と、gameEnd ごとの ywp_user_youkai_diff（出場した妖怪の最新行）から。
    """
    if start is None and result is None:          # 周回開始時：ログインのセーブから
        deck = next((r for r in rows(c.save.get('ywp_user_youkai_deck'))
                     if r and r[0] == '1'), None)
        job.deck_order = [y for y in (deck or [])[1:6] if y not in ('', '0')]
        job.deck_levels = _levels(c.save.get('ywp_user_youkai'))
    else:
        ids = [str(y.get('youkaiId')) for y in (start or {}).get('userYoukaiList') or []]
        if ids:
            job.deck_order = ids
        if isinstance(result, dict):
            job.deck_levels.update(_levels(result.get('ywp_user_youkai_diff')))
    for y in job.deck_order:
        if y in job.deck_levels:
            job.deck_levels_start.setdefault(y, job.deck_levels[y])


def deck_text(job: FarmJob) -> str:
    lines = []
    for i, y in enumerate(job.deck_order):
        lv = job.deck_levels.get(y)
        up = (lv - job.deck_levels_start[y]) if lv is not None and y in job.deck_levels_start else 0
        mark = '👑' if i == 0 else f'{i + 1}.'
        lines.append(f'{mark} {youkai_name(y)} '
                     + (f'**Lv{lv}**' if lv is not None else 'Lv?')
                     + (f'（+{up}）' if up > 0 else ''))
    return '\n'.join(lines)


def befriend_text(job: FarmJob) -> str:
    """進捗・結果に出す「仲間になった妖怪」の行"""
    top = ' / '.join(f'{youkai_label(y)}×{n}' for y, n in job.befriend_counts.most_common(3))
    more = len(job.befriend_counts) - 3
    text = f'**{job.befriends}回**：{top}' + (f' ほか{more}種' if more > 0 else '')
    if job.auto_deck:
        text += (f'\n自動編成：リーダーにした {job.leader_sets}回'
                 + (f'（最新 {youkai_label(job.last_leader)}）' if job.last_leader else ''))
    return text


def reward_youkai_id(result) -> int:
    """gameEnd の応答から仲間になった妖怪ID（なければ 0）

    userGameResultData.rewardYoukaiId に入る（2026-09-27 実機: 仲間になった回は
    2231000 / 2218000、ならなかった回は 0）。
    """
    ugr = result.get('userGameResultData') if isinstance(result, dict) else None
    if isinstance(ugr, str):
        try:
            ugr = json.loads(ugr)
        except ValueError:
            ugr = None
    try:
        return int((ugr or {}).get('rewardYoukaiId') or 0)
    except (TypeError, ValueError):
        return 0


async def auto_deck_after(job: FarmJob, c: Client, start: dict, yid: int) -> str:
    """仲間になった妖怪をリーダーにする（自動編成）。失敗しても周回は続ける

    戻り値は通知に使う結果の一文。
    """
    try:
        code, out = await asyncio.to_thread(c.make_leader, yid, start)
    except Exception as e:
        log_line(f'自動編成に失敗（周回は続ける） youkai={yid}: {str(e)[:150]}')
        return f'⚠ 編成できませんでした（{str(e)[:80]}）'
    if code == 'already':
        log_line(f'仲間になった {yid} はすでにデッキにいるのでそのまま')
        return 'すでにデッキにいるのでそのまま'
    if code == 0:
        job.leader_sets += 1
        job.last_leader = yid
        log_line(f'自動編成: {yid} をリーダーにした（外した妖怪: {out or "なし"}）')
        return 'リーダーにしました' + (f'（外した妖怪：{youkai_label(out)}）' if out else '')
    log_line(f'自動編成: deckEdit rc={code} youkai={yid}（デッキは変わっていない）')
    return f'⚠ 編成できませんでした（rc={code}）'


def deck_kept(c: Client) -> list[str]:
    """今のデッキが「組み直さない条件」（STRONG_DECK_KEEP_*）を満たすなら、その妖怪ID。満たさなければ []"""
    if not STRONG_DECK_KEEP_COUNT:
        return []
    deck, _ = c.current_deck()
    try:
        strong = [y for y in deck if (c.youkai_rank(y) or 0) >= STRONG_DECK_KEEP_RANK]
    except Exception as e:
        log_line(f'ランクを読めなかった（強い順の判定は続ける）: {str(e)[:120]}')
        return []
    return strong if len(strong) >= STRONG_DECK_KEEP_COUNT else []


async def sort_deck_now(job: FarmJob, c: Client, why: str):
    """デッキを攻撃力の高い順に組み直す（STRONG_DECK）。失敗しても周回は続ける

    自動編成中は今のリーダー（仲間にした妖怪）をそのままにして、残り4体を強い順にする。
    """
    if not STRONG_DECK:
        return
    strong = deck_kept(c)
    if strong:
        log_line(f'強い順の編成をしない（{why}）: {rank_label(STRONG_DECK_KEEP_RANK)}以上が'
                 f'{len(strong)}体いる {strong}')
        return
    leader = None
    if job.auto_deck:
        deck, _ = c.current_deck()
        leader = deck[0] if deck else None
    try:
        code, old, new = await asyncio.to_thread(c.sort_deck, leader)
    except Exception as e:
        log_line(f'強い順の編成に失敗（周回は続ける・{why}）: {str(e)[:150]}')
        return
    if code == 'same':
        return
    if code != 0:
        log_line(f'強い順の編成: deckEdit rc={code}（{why}・デッキは変わっていない）')
        return
    job.deck_sorts += 1
    job.deck_order = list(new)
    atk = c.youkai_attack()
    log_line(f'強い順の編成（{why}）: {old} → {new} 攻撃力 '
             f'{sum(atk.get(y, 0) for y in old)} → {sum(atk.get(y, 0) for y in new)}')


def closed_menu_stages(c: Client) -> set[int]:
    """まだ開いていないメニューの面（そこで仲間になると開く。MENU_UNLOCK_STAGES）"""
    mf = c.progress_detail().get('ywp_user_menufunc', {})
    return {s for m, s in MENU_UNLOCK_STAGES if mf.get(str(m)) == '0'}


def set_food_policy(job: FarmJob, c: Client):
    """好物を使う面と使い方を Client に教える。判定はバトルのたびに最新のメニューで行う

    ・自動編成の面: 周回開始時に選んだ job.food_mode（'new' / 'all'。'off' なら使わない）
    ・まだ開いていないメニューの面: 開くために、まだ持っていない妖怪にだけ使う（'new'）
    """
    def policy(stage_id):
        if job.auto_deck and stage_id == job.stage_id and job.food_mode in ('new', 'all'):
            return job.food_mode
        return 'new' if stage_id in closed_menu_stages(c) else None
    c.food_policy = policy


def on_food(job: FarmJob, c: Client, result: dict | None):
    """battle_end の後に呼ぶ。好物の出来事を数え、買った分はアイテム取得に数えない"""
    fed_now = False
    for ev in c.food_events:
        if ev[0] == 'fed':
            job.food_fed += 1
            fed_now = True
        elif ev[0] == 'bought':
            _, iid, cnt, cost = ev
            job.food_bought += cnt
            job.food_spent += cost
            if job.item_prev is not None:
                job.item_prev[str(iid)] = job.item_prev.get(str(iid), 0) + cnt
    c.food_events.clear()
    if fed_now and result and reward_youkai_id(result):
        job.food_won += 1


def food_text(job: FarmJob) -> str:
    text = (f'設定：{FOOD_MODES.get(job.food_mode, job.food_mode)}\n' if job.auto_deck else '')
    text += f'あげた {job.food_fed}回（仲間になった {job.food_won}回）'
    if job.food_bought:
        text += f'\n買った {job.food_bought}個（Yマネー -{job.food_spent:,}）'
    return text


def first_clear(result: dict) -> bool:
    """その面を初めてクリアした gameEnd か（userGameResultData.prevScore が 0）"""
    ugr = result.get('userGameResultData') if isinstance(result, dict) else None
    if isinstance(ugr, str):
        try:
            ugr = json.loads(ugr)
        except ValueError:
            return False
    return isinstance(ugr, dict) and ugr.get('prevScore') == 0 and bool(ugr.get('score'))


def grind_target(job: FarmJob, c: Client, hard: int) -> int | None:
    """hard の GRIND_BACK 面前（プリセットは並び順、単独周回は同じマップのクリア済みをID順）"""
    if job.is_preset and hard in job.stage_ids:
        pos = job.stage_ids.index(hard)
        return job.stage_ids[max(pos - GRIND_BACK, 0)] if pos > 0 else None
    before = sorted(s for s in c.stages() if s < hard and s // 1000 == hard // 1000)
    return before[-min(GRIND_BACK, len(before))] if before else None


async def start_grind(job: FarmJob, c: Client, need: float) -> bool:
    """硬い面（安全な速さだと1戦 MAX_CLEAR_TIME 秒超）をクリアしたあと、手前の面で勝ち数を稼ぐ

    まだ育っていない垢は、デッキの攻撃力に対して速すぎると rc=4 → BAN になるので、
    安全な速さ（min_clear_time）を守る。硬い面は1戦が長すぎるので、GRIND_BACK 面前を
    安全な速さで回して TRUST_BATTLES 勝に届いたら（育った垢になったら）速く戻る。
    手前の面が無ければ False（硬い面をそのまま安全な速さで回す）。
    """
    hard = job.stage_id
    target = grind_target(job, c, hard)
    if not target or target == hard:
        return False
    left = max(TRUST_BATTLES - trust_wins(c.userId), 1)
    if not job.is_preset:
        job.grind_home = job.grind_home or hard
    job.grind_stage, job.grind_left, job.grind_for = target, left, hard
    log_line(f'育成開始 #{job.jid}: {hard} は安全な速さだと1戦{need:.0f}秒 → {target} を'
             f'あと{left}勝（{TRUST_BATTLES}勝で育った垢）')
    try:
        await job.user.send(
            f'📈 **#{job.jid}** ステージ `{hard}` は、この垢だと弾かれない速さで1戦{need:.0f}秒'
            f'かかります。`{target}` を安全な速さで **あと{left}勝** して垢を育ててから戻ります'
            f'（安全な速さで勝ちを重ねた垢は、速く回しても弾かれていないため。{TRUST_BATTLES}勝で育った垢とします）。')
    except discord.HTTPException:
        pass
    return True


async def grind_step(job: FarmJob, c: Client):
    """育成中の1戦に勝ったあと。育った垢になったら硬い面に戻る"""
    job.grind_left = max(TRUST_BATTLES - trust_wins(c.userId), 0)
    if job.grind_left > 0 and not is_trusted(c.userId):
        return
    job.grind_left = 0
    done_for, job.grind_stage = job.grind_for, None
    log_line(f'育成終了 #{job.jid}: {trust_wins(c.userId)}勝で育った垢になった → {done_for} に戻る')
    try:
        await job.user.send(
            f'📈 **#{job.jid}** 垢が育ったので（{trust_wins(c.userId)}勝）、'
            + (f'`{done_for}` の次の面から' if job.is_preset else f'`{done_for}` を')
            + '速く回します。')
    except discord.HTTPException:
        pass


async def gacha_now(job: FarmJob, c: Client):
    """GACHA_STAGE を初めてクリアしたとき。ミッション報酬を受け取ってからコインを全部使い、
    出た妖怪で強い順に組み直す。失敗しても周回は続ける"""
    await receive_missions_now(job, c)
    prev = job.status
    job.status = '🎰 コインでガチャを引いています'
    await update_progress(job, force=True)
    try:
        got = await asyncio.to_thread(c.pull_coin_gachas)
    except Exception as e:
        log_line(f'ガチャに失敗（周回は続ける）: {str(e)[:150]}')
        got = []
    finally:
        job.status = prev
    for _, _, ids in got:
        job.gacha_youkai.extend(ids)
    if got:
        log_line(f'ガチャ {len(got)}回: {[youkai_name(y) for y in job.gacha_youkai[-len(got):]]}')
        await sort_deck_now(job, c, 'ガチャ')


def gacha_text(job: FarmJob) -> str:
    cnt = collections.Counter(job.gacha_youkai)
    top = ' / '.join(f'{youkai_label(y)}' + (f'×{n}' if n > 1 else '')
                     for y, n in cnt.most_common(6))
    more = len(cnt) - 6
    return f'**{len(job.gacha_youkai)}回**：{top}' + (f' ほか{more}種' if more > 0 else '')


async def on_befriend(job: FarmJob, c: Client, start: dict, result: dict):
    """gameEnd の後に呼ぶ。仲間になった妖怪を数え、自動編成のジョブならDMで知らせる"""
    yid = reward_youkai_id(result)
    if not yid:
        return
    job.befriends += 1
    job.befriend_counts[yid] += 1
    if not job.auto_deck:
        # 新しい妖怪がデッキより強ければ入れ替わる（同じ並びなら通信しない）
        await sort_deck_now(job, c, '仲間が増えた')
        return
    outcome = await auto_deck_after(job, c, start, yid)
    try:
        await job.user.send(
            f'🤝 **{youkai_label(yid)}** が仲間になりました'
            f'（#{job.jid}・{job.done}戦目・通算 {job.befriend_counts[yid]}回目）\n'
            f'→ {outcome}')
    except discord.HTTPException:
        pass


# gameEnd が rc=4（サーバーに拒否）になったら、この回数で周回を止める。rc=4 を1回でも
# 出した垢が BAN されていた（2026-09-28: rc=3/rc=4 を出した6垢すべて。1回だけの垢も）
RC4_STOP = 1
MISSION_EVERY = 3600          # ミッション報酬を受け取り直す間隔（秒）。周回開始時にも受け取る
LEVEL_OPEN_MAX = 50           # 自動で上限を解放するのはこのレベル未満まで（Lv50 から先は Yポイント）


async def receive_missions_now(job: FarmJob, c: Client):
    """ミッション報酬と郵便をまとめて受け取る。失敗しても周回は続ける"""
    job.last_mission_at = time.time()
    try:
        mail = await asyncio.to_thread(c.receive_presents)
    except Exception as e:
        mail = []
        log_line(f'郵便の受け取りに失敗（周回は続ける）: {str(e)[:150]}')
    if mail:
        job.present_receipts += len(mail)
        log_line(f'郵便を受け取った: {len(mail)}件 '
                 + ' / '.join(f'{(p.get("bodyText") or "").splitlines()[0][:20]}'
                              f'（種類{p.get("distItemType")}・{p.get("distItemId")}×{p.get("distItemCnt")}）'
                              for p in mail[:6]))
    try:
        got = await asyncio.to_thread(c.receive_missions)
    except Exception as e:
        log_line(f'ミッション報酬の受け取りに失敗（周回は続ける）: {str(e)[:150]}')
        return
    if not got:
        return
    job.mission_receipts += len(got)
    for it in got:
        # itemType 3 = Yマネー（受け取るたびに ymoney が同じだけ増えた。2026-09-28 実機）
        if it.get('itemType') == 3:
            job.mission_ymoney += int(it.get('itemCnt') or 0)
    log_line(f'ミッション報酬を受け取った: {len(got)}件 {got[:6]}')


async def open_deck_levels(job: FarmJob, c: Client, youkai_levels) -> set[str]:
    """上限で止まっている妖怪を Yマネーで解放する。youkai_levels = [(妖怪ID, レベル), …]

    ひとだまが LEVEL_HITODAMA_GUARD 個以下のあいだは、解放しても HITODAMA_RICH 以上の
    Yマネーが残るときだけ解放する（ひとだまを買えずに周回が止まるのを防ぐ）。
    戻り値: 解放できた妖怪と、Yマネーが貯まったらまた解放する妖怪のID（まだ伸びる妖怪）
    """
    opened = set()
    hd = hitodama_total(job.now_points or {})
    if hd is None:
        try:
            hd = hitodama_total(c.points())     # 周回開始時はまだ now_points が無い
        except Exception:
            hd = None
    keep = HITODAMA_RICH if hd is not None and hd <= LEVEL_HITODAMA_GUARD else 0
    for yid, lv in youkai_levels:
        if (yid, lv) in job.level_open_skip:
            continue
        try:
            res, paid = await asyncio.to_thread(c.open_level, yid, lv, LEVEL_OPEN_MAX, keep)
        except Exception as e:
            log_line(f'上限解放に失敗（周回は続ける） {yid} Lv{lv}: {str(e)[:150]}')
            job.level_open_skip.add((yid, lv))
            continue
        if res == 'ok':
            opened.add(str(yid))
            job.level_unlocks += 1
            job.level_unlock_ymoney += paid
            log_line(f'上限解放: {youkai_name(yid)}（{yid}）Lv{lv} の上限を Yマネー{paid}で解放')
        elif res in ('skip:Yマネー不足', 'skip:ひとだま優先'):
            opened.add(str(yid))                    # 貯まったらまた試す（まだ伸びる）
            if res == 'skip:ひとだま優先' and not job.level_wait_logged:
                job.level_wait_logged = True
                log_line(f'上限解放を見送り（ひとだま {hd}個・Yマネーはひとだま代に残す） {yid} Lv{lv}')
        else:
            job.level_open_skip.add((yid, lv))      # 値段不明・Yポイントの段などは試し直さない
            log_line(f'上限解放しない {yid} Lv{lv}: {res}')
    return opened


async def on_level(job: FarmJob, c: Client, result: dict) -> set[str]:
    """gameEnd の後。上限で止まった編成の妖怪（isLockLevel）を解放する。解放できたIDを返す"""
    locked = []
    for y in (result.get('userYoukaiResultList') if isinstance(result, dict) else None) or []:
        if not isinstance(y, dict) or not y.get('isLockLevel') or y.get('isMaxLevel'):
            continue
        lv = ((y.get('after') or {}).get('level')) or ((y.get('before') or {}).get('level'))
        if isinstance(lv, int) and lv < LEVEL_OPEN_MAX:
            locked.append((str(y.get('youkaiId')), lv))
    if locked:
        return await open_deck_levels(job, c, locked)
    return set()


def deck_locked_levels(c: Client) -> list[tuple[str, int]]:
    """ログイン時のセーブから、上限で止まっている編成の妖怪（行の9列目が1）"""
    deck = next((r for r in rows(c.save.get('ywp_user_youkai_deck')) if r and r[0] == '1'), None)
    ids = [y for y in (deck or [])[1:6] if y not in ('', '0')]
    yk = {r[0]: r for r in rows(c.save.get('ywp_user_youkai')) if r}
    out = []
    for y in ids:
        r = yk.get(y)
        if r and len(r) > 8 and r[8] == '1':
            try:
                out.append((y, int(r[1])))
            except ValueError:
                pass
    return out


async def maybe_unlock_menus(job: FarmJob, c: Client):
    """ショップ等が開き損ねていたら開け直す（unlock_menus）。失敗しても周回は続ける"""
    if not menus_to_reopen(c):
        return
    prev = job.status

    async def progress(text):
        job.status = text
        await update_progress(job, force=True)

    try:
        changed = await unlock_menus(c, progress)
        opened = [m for m, (a, b) in changed.items() if b == '1']
        log_line(f'メニューを開け直した: {opened or "なし"}')
    except Exception as e:
        log_line(f'メニューの開け直しに失敗（周回は続ける）: {str(e)[:200]}')
    job.status = prev


async def maybe_buy_hitodama(job: FarmJob, c: Client):
    """ひとだまが HITODAMA_LOW 個以下になったら HITODAMA_BUY 個まで自動購入する

    buyHitodama.nhn（Yマネー）。パックは5個=1000 / 20個=3000 / 50個=6000の3種で、
    単価の良い50個パック(goodsId=1003)を繰り返して目標個数まで買い足す。
    1つも買えなかった（Yマネー不足など）場合は FarmStop で周回を止める。
    部分的に買えた場合はそのまま周回を続け、次の節目でまた試す。
    """
    total = hitodama_total(job.now_points)
    if total is None or total > HITODAMA_LOW:
        return
    job.status = f'🛒 ひとだま {total}個 → {HITODAMA_BUY}個まで自動購入中...'
    await update_progress(job, force=True)
    log_line(f'ひとだま自動購入 開始 現在={total} 目標={HITODAMA_BUY} '
             f'所持={c.wallet_text()}')
    try:
        added = await asyncio.to_thread(c.buy_hitodama, HITODAMA_BUY)
    except Exception as e:
        msg = str(e)[:200]
        log_line(f'ひとだま自動購入 失敗: {msg}')
        if 'Yマネー不足' in msg:
            # 止めずに、ミッション報酬と自然回復を待って続ける
            await wait_hitodama(job, c)
            return
        # ひとだまはショップ（menufunc 3）が閉じていても、ある程度進めた垢なら買える。
        # 進行が足りない垢は買えないので、Yマネー不足と区別できるよう可能性を出しておく
        raise FarmStop(f'🔥 ひとだまの自動購入に失敗したため停止しました: {msg}\n'
                       'Yマネーは足りているのにこれが出る場合、そのアカウントは'
                       'まだひとだまを買える段階まで進んでいない可能性があります。')
    job.hitodama_buys += added
    await asyncio.to_thread(c.refresh)
    job.now_points = c.points()
    job.points_at = time.time()
    job.points_done = job.done
    job.status = f'🛒 ひとだまを{added:,}個補充しました'


HITODAMA_WAIT_MAX = 1800     # ひとだまの回復を1回に待つ上限（秒）


async def wait_hitodama(job: FarmJob, c: Client) -> bool:
    """Yマネーでひとだまを買えないとき、ミッション報酬と自然回復を待つ（止めない）

    ミッション報酬でYマネーが入れば買う。入らなければ、サーバーが返す次の回復までの秒数
    （ywp_user_data.hitodamaRecoverSec）だけ待って取り直す。1個でも使えるようになったら True。
    中止されたら False。
    """
    while not job.cancel.is_set():
        if await wait_maintenance(job):
            return False
        await receive_missions_now(job, c)
        try:
            await asyncio.to_thread(c.refresh)
        except Exception as e:
            log_line(f'ひとだま回復待ち: 取り直しに失敗（待って再試行）: {str(e)[:120]}')
        job.now_points = c.points()
        job.points_at, job.points_done = time.time(), job.done
        total = hitodama_total(job.now_points) or 0
        info = c.info()
        ymoney = int(info.get('ymoney') or 0)
        if total >= 1:
            log_line(f'ひとだま回復待ち おわり: ひとだま {total}個')
            job.status = '⚔ 周回中'
            return True
        if ymoney >= 1000:
            try:
                added = await asyncio.to_thread(c.buy_hitodama, HITODAMA_BUY)
                job.hitodama_buys += added
                continue
            except Exception as e:
                log_line(f'ひとだま回復待ち: 購入に失敗 {str(e)[:120]}')
        sec = int(info.get('hitodamaRecoverSec') or 0)
        wait = min(max(sec + 20, 60), HITODAMA_WAIT_MAX) if sec > 0 else 600
        job.status = (f'🔥 ひとだま回復待ち（Yマネー{ymoney:,}では買えないので自然回復を待っています・'
                      f'約{max(wait // 60, 1)}分）')
        log_line(f'ひとだま回復待ち: ひとだま0個・Yマネー{ymoney} → {wait}秒待つ（回復まで {sec}秒）')
        await update_progress(job, force=True)
        if await sleep_or_cancel(job, wait):
            return False
    return False


async def refresh_points(job: FarmJob, c: Client, i: int) -> bool:
    """必要なら所持数を取り直す。取り直したらTrue

    セーブはログインし直さないと降ってこないので毎周は無理。節目だけ叩く。
    目標指定で残りが少ないときは、行き過ぎないよう間隔を詰める。
    """
    if i == 1:
        # ログイン直後の値がそのまま最新。待っている更新ボタンは即座に返す
        if job.want_refresh:
            job.want_refresh = False
            job.refreshed.set()
        return False
    due = job.want_refresh or (i - 1) % WALLET_REFRESH_EVERY == 0
    if not due and job.target_key is not None:
        rest = job.remaining
        if rest is not None and rest <= WALLET_REFRESH_NEAR:
            due = True      # 目標が近い。毎周確かめる
    if not due:
        return False

    job.want_refresh = False
    prev = job.status
    job.status = '🔄 所持数を確認中'
    try:
        _t0 = time.time()
        await asyncio.to_thread(c.refresh)
        job.now_points = c.points()
        job.points_at = time.time()
        job.points_done = job.done
        job.refreshes += 1
        job.refreshed.set()          # 更新ボタンの待ち合わせを解除
        # 周回でサーバー側の進行が動いているかを見る。クリアしているのに
        # ショップ/ガシャが解放されない、のような症状の切り分け用
        shape, detail = c.save_shape(), c.progress_detail()
        if job.save_shape_prev:
            diff = Client.shape_diff(job.save_shape_prev, shape)
            log_line(f'{job.done}周ぶんのセーブ変化: '
                     + (', '.join(f'{k} {a}→{b}' for k, (a, b) in diff.items())
                        if diff else '行数の変化なし'))
            # menufunc のように行数が変わらず値だけ動くものを別で拾う
            changed = Client.detail_diff(job.save_detail_prev, detail)
            if changed:
                log_line('  進行フラグの変化: ' + Client.detail_diff_text(changed))
        job.save_shape_prev, job.save_detail_prev = shape, detail
        print(f'[job] 所持数更新 user={job.user.id} #{i} '
              f'{time.time() - _t0:.1f}s')
        await maybe_buy_hitodama(job, c)
    except FarmStop:
        job.status = prev
        raise
    except Exception as e:
        # 取り直しに失敗しても周回自体は続ける（表示が古くなるだけ）
        print(f'[job] 所持数の取り直し失敗 user={job.user.id}: {str(e)[:120]}')
    job.status = prev
    return True


async def run_job(job: FarmJob, email: str, password: str,
                  save_hint: str | None = None):
    with log_tag(f'job#{job.jid}/{job.user.id}'):
        log_line(f'ジョブ開始 stage={job.stage_ids} count={job.count} '
                 f'target={job.target_key}:{job.target_value or job.target_delta} '
                 f'battle={job.battle_time} cd={job.cooldown} hint={save_hint}')
        await _run_job(job, email, password, save_hint)


async def _run_job(job: FarmJob, email: str, password: str,
                   save_hint: str | None = None):
    try:
        if await wait_maintenance(job):         # メンテ中はログインもしない
            return
        # --- ログイン準備＆セーブデータ選択（複数ある場合のみ） ---
        # 同時実行枠を取る前に選択まで済ませる（枠を占有したまま待たせない）。
        # ここではまだlogin.nhnを叩かない（ログインは周回開始の直前に）
        job.status = '🔑 ログイン準備中...'
        await update_progress(job, force=True)

        async def _send_prompt(view, accs):
            return await job.user.send(
                content=(f'🗂 このアカウントには **{len(accs)}件** のセーブデータがあります。\n'
                         'ログインするセーブデータを選択してください'
                         f'（{SAVE_SELECT_TIMEOUT // 60}分でタイムアウト）'),
                view=view)

        # プロキシを1枠借りる（proxy.txt）。周回が終わるまでの間だけ占有
        job.proxy_lease = await lease_proxy()
        if job.proxy_lease is not None:
            log_line(f'プロキシ経由で周回: {proxy_pool.mask(job.proxy_lease.url)}')

        try:
            c, save_uid, _ = await prepare_login(email, password, job.user.id,
                                                _send_prompt, cancel_event=job.cancel,
                                                save_hint=save_hint,
                                                proxy=(job.proxy_lease.url
                                                       if job.proxy_lease else None))
        except Exception as e:
            if not job.cancel.is_set():
                job.last_error = f'ログイン失敗: {str(e)[:300]}'
            return
        finally:
            email = password = None   # 参照を残さない

        # 同じセーブを別の周回が回している最中なら始めない。2つ同時だとログインがぶつかり、
        # 片方のバトルが rc=32 で失敗する（2026-09-29 11:38: #1 と #3 がぶつかった）。
        # 一時停止中の周回は通信しないので、その間に別の周回を回すのは構わない
        other = next((j for lst in JOBS.values() for j in lst
                      if j is not job and not j.cancel.is_set() and not j.is_paused
                      and str(j.save_uid or '') == str(save_uid or '-')), None)
        if other is not None:
            job.last_error = (f'このセーブ（{other.player_name or save_uid}）はすでに #{other.jid} で'
                              '周回中です。同じ垢で2つ同時に回すとログインがぶつかって失敗する'
                              f'（rc=32）ので、先に #{other.jid} を一時停止か中止してください')
            return

        if SEM.locked():
            job.status = f'順番待ち（同時実行上限 {MAX_CONCURRENT_JOBS}）'
            await update_progress(job, force=True)

        async with SEM:
            if job.cancel.is_set():
                return

            # --- ログイン（選択したセーブデータで） ---
            job.status = '🔑 ログイン中...'
            job.started_at = time.time()
            await update_progress(job, force=True)
            try:
                await asyncio.to_thread(c.login, save_uid)
            except Exception as e:
                job.last_error = f'ログイン失敗: {str(e)[:300]}'
                return
            job.save_uid = c.userId
            if job.resumed:
                # 復元したジョブは、止まる前までの稼働時間を引き継ぐ
                job.started_at = time.time() - job.resume_elapsed - job.paused_seconds

            try:
                job.player_name = c.raw_info().get('playerName')
            except (ValueError, AttributeError):
                pass
            set_food_policy(job, c)
            job.item_prev = _inventory(c.save.get('ywp_user_item'))
            # 開き損ねたメニュー（ショップ等）があれば周回の前に開け直す
            await maybe_unlock_menus(job, c)
            if job.drop_asked and not job.auto_deck:
                # 「ドロップと自動編成」を無効で始めた。この面では友だち申告もしない
                c.befriend_off.add(job.stage_id)
            await sort_deck_now(job, c, '周回開始')
            track_deck(job, c)
            # ミッション報酬を受け取り、上限で止まっている編成の妖怪を解放してから回す
            await receive_missions_now(job, c)
            if job.auto_levelup:
                await open_deck_levels(job, c, deck_locked_levels(c))
            # 開始時点の所持数を控える。以降の増減はここからの差分で出す
            # （復元したジョブは最初に始めたときの値を引き継ぐ）
            if not job.resumed or not job.start_points:
                job.start_points = c.points()
            job.now_points = c.points()
            job.points_at = time.time()
            # 開始時点でひとだまが減っていたら先に補充する
            # （最初の所持数確認の節目を待たずに尽きてしまうのを防ぐ）
            await maybe_buy_hitodama(job, c)
            if job.target_key is not None and job.target_delta is not None:
                base = job.start_points.get(job.target_key)
                if base is None:
                    job.last_error = (f'{WALLET_LABEL.get(job.target_key, job.target_key)}'
                                      'の現在値が読めないため、増加量指定は使えません。'
                                      '周回数か絶対値で指定してください。')
                    return
                job.target_value = base + job.target_delta
            remember_session(job.user.id, c, job.player_name)

            # --- ステージ確認 ---
            # プリセットは未クリアのステージも順番に進む（解放周回が目的）ため
            # 事前チェックしない。battle_start の応答で判定する
            stages = c.stages()
            info = c.raw_info()
            tut = c.save.get('ywp_user_tutorial_list') or ''
            log_line(f'ログイン完了 uid={save_uid} name={job.player_name} '
                     f'解放ステージ={len(stages)}件 所持={c.wallet_text()} '
                     f'nowStage={info.get("nowStageId")} '
                     f'チュートリアル={len(tut)}字')
            if not stages:
                # 作りたてのセーブはここに来る。チュートリアルを終えるまで
                # ywp_user_stage が空なので、どのIDを指定しても開始できない
                job.last_error = (
                    'このセーブには解放済みステージが1件もありません。\n'
                    '作成直後のアカウントはチュートリアルが終わるまで周回できません。'
                    '「久垢」で作るか、ゲーム内で最初のステージまで進めてください。')
                log_line('中止: ywp_user_stage が空（チュートリアル未完了とみられる）')
                return
            if job.is_preset and not job.resumed and job.stage_pos == 0:
                # 新しく始めたプリセットは、クリア済みの面を飛ばして、まだクリアしていない最初の面から
                # （前は毎回 29701001【通】からやり直していた）。全部クリア済みなら最初から回す。
                # クリア済み = ywp_user_stage の6列目（ベストスコア）が 0 より大きい
                cleared = {s for s, r in stages.items() if len(r) > 5 and r[5] not in ('', '0')}
                first = next((k for k, s in enumerate(job.stage_ids) if s not in cleared), None)
                if first:
                    job.stage_pos = first
                    log_line(f'プリセット: クリア済みの {first}面を飛ばして {job.stage_ids[first]} から始める')
                    try:
                        await job.user.send(f'⏩ **#{job.jid}** クリア済みの{first}面を飛ばして、'
                                            f'まだクリアしていない `{job.stage_ids[first]}` から始めます。')
                    except discord.HTTPException:
                        pass
            if (not job.is_preset and job.stage_id not in stages
                    and job.stage_id not in current_event_stages()[0]):
                # 今のイベントの面は、まだ解放していなくても始める（開いていなければ
                # gameStart でサーバーが断るので、そこで止まる）
                # 入力IDと前方一致するものを優先して候補表示
                pref = str(job.stage_id)[:4]
                cand = [s for s in sorted(stages) if str(s).startswith(pref)][:8] \
                    or sorted(stages)[:8]
                job.last_error = (f'ステージID {job.stage_id} は未クリアか存在しません。\n'
                                  f'解放済み {len(stages)}件 / 候補: '
                                  + ', '.join(f'`{s}`' for s in cand))
                log_line(f'中止: stage={job.stage_id} が未解放 / 解放済み={sorted(stages)[:20]}')
                return

            # --- 周回ループ ---
            job.status = '⚔ 周回中'
            await update_progress(job, force=True)

            errs = 0       # 連続エラー数。成功でリセット。再試行は ERROR_RETRY 回まで
            tok_errs = 0   # rc=32（tokenズれ）の連続回数。成功でリセット
            i = job.done if job.resumed else 0     # 復元したジョブは続きの戦数から
            persist_jobs()
            last_end = None   # 前回gameEnd完了時刻（1バトルの実測間隔の測定用）
            while True:
                i += 1
                # count が None（無限モード）のときは上限なしで回し続ける。
                # プリセットは「周回数×ステージ数」をクリアし終えたら終わる。
                # 失敗した回は同じステージをやり直すので、試した回数（i）では数えない
                total = job.total_battles
                # レベル上げの戦数は周回数に数えない。レベル上げ中は終わらない
                grinding = bool(job.grind_left > 0 and job.grind_stage)
                if total is not None and not grinding and (
                        job.stage_pos >= total if job.is_preset
                        else i - job.grind_battles > total):
                    break
                if job.cancel.is_set():
                    break
                # 一時停止はここ（バトル開始前）でだけ効かせる
                if await wait_while_paused(job):
                    break
                if await wait_maintenance(job):      # メンテの5分前から新しいバトルを始めない
                    break
                # プリセット：完結したバトル数に応じて周回対象ステージを選ぶ
                if job.is_preset:
                    job.stage_id = job.stage_ids[job.stage_pos % len(job.stage_ids)]
                if grinding:
                    job.stage_id = job.grind_stage      # 硬い面の手前で垢を育てる
                    job.grind_battles += 1
                elif job.grind_home:
                    job.stage_id, job.grind_home = job.grind_home, None

                await refresh_points(job, c, i)
                if job.target_reached:
                    job.status = '🎯 目標到達'
                    break
                # ひとだまが尽きていたら、バトルを始める前に買う。残数は毎戦の応答で
                # 追っている（sync_hitodama）。前は3戦ごとの取り直しでしか気づけず、
                # 尽きたまま gameStart して rc=5 で失敗していた（2026-09-28）
                await maybe_buy_hitodama(job, c)

                if await sleep_or_cancel(job, REQUEST_DELAY * random.uniform(0.7, 1.3)):
                    break

                wait = jitter(job.battle_time, BATTLE_TIME_MIN,
                              spread=BATTLE_TIME_SPREAD)

                # --- gameStart ---
                try:
                    rc, js, btype = await asyncio.to_thread(c.battle_start, job.stage_id)
                except Exception as e:
                    js, btype = {}, None
                    job.status = f'⚠ 通信エラー: {str(e)[:60]}'
                code = js.get('resultCode') if isinstance(js, dict) else None
                sync_hitodama(job, c, js)

                if code != 0:
                    # ひとだま不足: 止めずに買うか自然回復を待ち、同じ周をやり直す
                    if is_hitodama_shortage(js):
                        log_line(f'ひとだま不足でバトルを始められなかった（待って続ける） #{job.jid}')
                        if await wait_hitodama(job, c):
                            i -= 1
                            continue
                        break
                    job.fail += 1
                    job.done = i
                    errs += 1
                    if code is not None:
                        job.status = f'⚠ gameStart 失敗 (rc={code}: {RC.get(code, "不明")})'
                    if code in TOKEN_CODES:
                        # tokenズれは再ログインで新しいtokenを取り直せる
                        tok_errs += 1
                        if tok_errs <= TOKEN_RETRY and await recover_token(
                                job, c, save_uid, tok_errs):
                            if await sleep_or_cancel(job, retry_wait(job, tok_errs)):
                                break
                            continue    # 同じ周をもう一度
                        job.last_error = ('rc=32 (tokenズれ) が続くため中止しました'
                                          f'（再ログインを{TOKEN_RETRY}回試みました）')
                        break
                    if code in FATAL_CODES:
                        job.last_error = f'致命的エラー rc={code} ({RC.get(code, "不明")}) のため中止しました'
                        break
                    if errs > ERROR_RETRY:
                        job.last_error = (f'エラーが{errs}回連続したため中止しました'
                                          f'（再試行は{ERROR_RETRY}回まで）')
                        break
                    print(f'[job] gameStart失敗 user={job.user.id} #{i} '
                          f'rc={code} errs={errs}')
                    if await sleep_or_cancel(job, retry_wait(job, errs)):
                        break
                    continue

                # gameStartが通った = tokenは生きている
                tok_errs = 0

                # バトル時間。中止されても開始済みのバトルは最低時間まで待って閉じる
                # （最短は c.min_clear_time。通常の面はデッキの攻撃力で延びる）
                need = c.min_clear_time(job.stage_id, js)
                too_hard = need > MAX_CLEAR_TIME
                if need > wait:
                    wait = need * random.uniform(1.0, 1.08)
                if too_hard:
                    job.status = (f'🐢 この垢はまだ育っていないので、弾かれない速さで {need:.0f}秒かけて'
                                  f'クリアします（このあと {GRIND_BACK}面前で垢を育てます）')
                    await update_progress(job, force=True)
                t0 = time.time()
                # 好物はバトルの待ち時間の中であげる（あげる回も1戦の長さは変わらない）
                await asyncio.to_thread(c.maybe_feed, job.stage_id, js)
                cancelled = await sleep_or_cancel(job, max(0.0, wait - (time.time() - t0)))
                # 下限はサーバーが計る gameStart→gameEnd の間隔で判定する
                # （手元の実測だと往復ぶん短いので、割っていても気づけない）
                shortfall = need - (server_elapsed(js) or (time.time() - t0))
                if shortfall > 0:
                    await asyncio.sleep(shortfall)

                try:
                    # クリア秒は battle_end が送信直前に測る。ここで渡すと
                    # スレッド切り替え待ちのぶんだけ短く申告してしまう
                    rc, result = await asyncio.to_thread(
                        c.battle_end, job.stage_id, btype, js)
                    code = result.get('resultCode')
                except Exception as e:
                    code, result = None, {}
                    job.status = f'⚠ 通信エラー: {str(e)[:60]}'
                sync_hitodama(job, c, result)

                job.done = i
                now = time.time()
                if last_end is not None:
                    # 1バトルの実測間隔（gameEnd→gameEnd）。効率診断用のログ
                    print(f'[job] user={job.user.id} #{i} stage={job.stage_id} '
                          f'cycle={now - last_end:.1f}s rc={code}')
                last_end = now
                if code == 0:
                    errs = 0
                    tok_errs = 0
                    job.success += 1
                    job.event_point += int(result.get('eventPoint') or 0)
                    job.status = '⚔ 周回中'
                    on_items(job, c, result)
                    on_food(job, c, result)
                    track_deck(job, c, js, result)
                    await on_befriend(job, c, js, result)
                    if job.auto_levelup:
                        await on_level(job, c, result)
                    if time.time() - job.last_mission_at >= MISSION_EVERY:
                        await receive_missions_now(job, c)
                    if job.stage_id == GACHA_STAGE and first_clear(result):
                        await gacha_now(job, c)
                    if grinding:
                        job.status = f'📈 垢を育成中（`{job.grind_for}` が硬いため）'
                        await grind_step(job, c)
                else:
                    on_food(job, c, None)
                    # ひとだま不足は再試行しても解決しないので即中止
                    if is_hitodama_shortage(result):
                        job.fail += 1
                        job.last_error = hitodama_shortage_error(result)
                        job.status = '🔥 ひとだま不足'
                        break
                    job.fail += 1
                    errs += 1
                    print(f'[job] gameEnd失敗 user={job.user.id} #{i} '
                          f'rc={code} errs={errs}')
                    if code is not None:
                        job.status = f'⚠ gameEnd 失敗 (rc={code}: {RC.get(code, "不明")})'

                # プリセットはクリアできたときだけ次のステージへ進む。失敗したら同じ
                # ステージをやり直す（前は失敗でも進み、クリアしていない先の面が
                # 開いておらず rc=5 が続いていた。2026-09-28）
                if job.is_preset and code == 0 and not grinding:
                    job.stage_pos += 1
                persist_jobs()      # 再起動しても続きから回せるよう1戦ごとに残す

                if code in TOKEN_CODES:
                    # gameEndのtokenズれ。バトルは開始済みなのでこの周は失敗扱いのまま、
                    # 再ログインして次の周から続ける
                    tok_errs += 1
                    if not (tok_errs <= TOKEN_RETRY
                            and await recover_token(job, c, save_uid, tok_errs)):
                        job.last_error = ('rc=32 (tokenズれ) が続くため中止しました'
                                          f'（再ログインを{TOKEN_RETRY}回試みました）')
                        break
                if code in FATAL_CODES:
                    job.last_error = f'致命的エラー rc={code} ({RC.get(code, "不明")}) のため中止しました'
                    break
                if cancelled:
                    break
                if code == 0 and too_hard:
                    # 硬い面。手前の面で垢を育ててから戻る（手前が無ければ、この面を安全な速さで続ける）
                    await start_grind(job, c, need)
                if code == 4 and c.rc4_count.get(job.stage_id, 0) >= RC4_STOP:
                    # rc=4 を出した垢は BAN されている（2026-09-28）。1回でもすぐ止める
                    job.last_error = (f'ステージ `{job.stage_id}` の gameEnd が rc=4（サーバーに'
                                      '拒否）で弾かれたため止めました。rc=4 を出したアカウントは'
                                      '停止（BAN）されることがあるので、同じ面はしばらく回さないで'
                                      'ください')
                    break
                if code != 0 and errs > ERROR_RETRY:
                    job.last_error = (f'ステージ `{job.stage_id}` でエラーが{errs}回連続したため'
                                      f'中止しました（再試行は{ERROR_RETRY}回まで）'
                                      + ('。先のステージには進んでいません' if job.is_preset else ''))
                    break

                # バトル終了 → 次のバトルまで。失敗が続いた周は長めに空けて再試行
                if job.count is None or job.grind_left > 0 or (
                        job.stage_pos < job.total_battles if job.is_preset
                        else i - job.grind_battles < job.total_battles):
                    gap = (retry_wait(job, errs) if code != 0
                           else jitter(job.cooldown, COOLDOWN_MIN))
                    if await sleep_or_cancel(job, gap):
                        break

            # 最終的な増減を出すため、締めにもう一度だけ取り直す
            if job.done:
                job.want_refresh = True
                await refresh_points(job, c, job.done + 1)
                if job.target_reached and not job.last_error:
                    job.status = '🎯 目標到達'

    except asyncio.CancelledError:
        if job_state(job) is not None and not job.cancel.is_set():
            job.interrupted = True      # 次の起動で restore_jobs が続きから回す
        else:
            job.last_error = 'Botの停止によりジョブが中断されました'
        raise
    except FarmStop as e:
        job.last_error = str(e)
    except Exception:
        job.last_error = traceback.format_exc()[-400:]
        print(f'[job] 予期しないエラー user={job.user.id}\n{traceback.format_exc()}')
    finally:
        if job.proxy_lease is not None:
            job.proxy_lease.release()
            job.proxy_lease = None
        job.refreshed.set()      # 更新ボタンが待ちっぱなしにならないように
        drop_job(job)
        if not job.interrupted:
            persist_jobs()       # 終わった・中止したジョブを保存から消す
        await finish_job(job)


async def finish_job(job: FarmJob):
    """進捗メッセージを結果に差し替え、完了をDM通知する"""
    if job.interrupted:
        # Botの再起動で止まっただけ。結果にはせず、起動後の復元を待つ
        job.status = '♻ Botの再起動で中断（起動後に自動で続きから再開します）'
        try:
            if job.message is not None:
                await job.message.edit(embed=progress_embed(job))
        except Exception:
            pass
        return
    embed = result_embed(job)
    jump = None
    try:
        if job.message is not None:
            await job.message.edit(embed=embed, view=None)
            jump = job.message.jump_url
        else:
            job.message = await job.user.send(embed=embed)
            return
    except discord.HTTPException:
        pass

    # 編集ではプッシュ通知が飛ばないので、短い通知を別メッセージで送る
    head = '❌ 周回失敗' if job.last_error else ('⏹ 周回中止' if job.cancel.is_set() else '✅ 周回完了')
    stage_txt = (f'プリセット「{job.preset_name}」（{job.stage_pos}/{len(job.stage_ids)}ステージ）'
                 if job.is_preset else f'ステージ `{job.stage_id}`')
    body = (f'{head} **#{job.jid}** — {stage_txt} / '
            f'成功 **{job.success}** ・失敗 **{job.fail}** ・所要 {fmt_sec(job.elapsed)}')
    try:
        await job.user.send(f'{body}\n{jump}' if jump else body, embed=None if jump else embed)
    except discord.HTTPException:
        pass


# ============================================================================
# UI
# ============================================================================

async def do_refresh(interaction: discord.Interaction, job: FarmJob):
    """「🔄 更新」の中身

    ここでは通信しない。クライアントは周回ループが握っていて、横から叩くと
    tokenの受け渡しが壊れる（rc=32）。取り直しを依頼して、周回ループが
    済ませるのを待ってから最新の内容で描き直す。
    """
    if job.cancel.is_set() or find_job(job.user.id, job.jid) is None:
        return await interaction.response.send_message(
            '⏹ このジョブは既に終了しています。', ephemeral=True)

    await interaction.response.defer()
    job.refreshed.clear()
    job.want_refresh = True
    try:
        await asyncio.wait_for(job.refreshed.wait(), timeout=REFRESH_WAIT)
        note = None
    except asyncio.TimeoutError:
        # 長いバトル中や再試行待ちだと間に合わないことがある
        note = (f'⏳ {REFRESH_WAIT:g}秒以内に所持数を取り直せませんでした。'
                '表示は前回取得時のままです。')

    try:
        await interaction.edit_original_response(embed=progress_embed(job))
    except discord.HTTPException:
        pass
    if note:
        try:
            await interaction.followup.send(note, ephemeral=True)
        except discord.HTTPException:
            pass


class YesNoView(discord.ui.View):
    """周回開始時の「有効にしますか？」（押した人だけが答えられる）"""

    def __init__(self, owner_id: int, yes_text: str, no_text: str):
        super().__init__(timeout=AUTO_DECK_ASK_TIMEOUT)
        self.owner_id = owner_id
        self.yes_text, self.no_text = yes_text, no_text
        self.choice: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def _answer(self, interaction: discord.Interaction, on: bool):
        self.choice = on
        try:
            await interaction.response.edit_message(
                content=self.yes_text if on else self.no_text, view=None)
        except discord.HTTPException:
            pass
        self.stop()

    @discord.ui.button(label='有効にする', emoji='✅', style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, True)

    @discord.ui.button(label='無効にする', emoji='✖', style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, False)


class DropDeckConfirmView(discord.ui.View):
    """AUTO_DECK_STAGE を周回するときの「ドロップと自動編成を有効にしますか？」"""

    def __init__(self, owner_id: int):
        super().__init__(timeout=AUTO_DECK_ASK_TIMEOUT)
        self.owner_id = owner_id
        self.choice: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def _answer(self, interaction: discord.Interaction, on: bool):
        self.choice = on
        text = ('✅ ドロップと自動編成を**有効**にして開始します' if on
                else '✖ ドロップと自動編成は**無効**で開始します')
        try:
            await interaction.response.edit_message(content=text, view=None)
        except discord.HTTPException:
            pass
        self.stop()

    @discord.ui.button(label='有効にする', emoji='✅', style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, True)

    @discord.ui.button(label='無効にする', emoji='✖', style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, False)


class FoodModeView(discord.ui.View):
    """自動編成の面で好物をどう使うか（FOOD_MODES）

    job を渡すと周回中の変更用。選んだ時点でそのジョブに反映する（次のバトルから効く）。
    """

    def __init__(self, owner_id: int, job: FarmJob | None = None):
        super().__init__(timeout=AUTO_DECK_ASK_TIMEOUT)
        self.owner_id = owner_id
        self.job = job
        self.choice: str | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def _answer(self, interaction: discord.Interaction, mode: str):
        self.choice = mode
        text = f'🍙 好物：**{FOOD_MODES[mode]}** で開始します'
        job = self.job
        if job is not None:
            if job.cancel.is_set() or find_job(job.user.id, job.jid) is None:
                text = '❌ このジョブはもう終わっています。'
            else:
                old, job.food_mode = job.food_mode, mode
                persist_jobs()
                log_line(f'好物の使い方を変更 #{job.jid}: {old} → {mode}')
                text = (f'🍙 **#{job.jid}** の好物を「**{FOOD_MODES[mode]}**」に変更しました'
                        '（次のバトルから）。')
        try:
            await interaction.response.edit_message(content=text, view=None)
        except discord.HTTPException:
            pass
        self.stop()

    @discord.ui.button(label='まだ持っていない妖怪だけ', emoji='🆕', style=discord.ButtonStyle.success)
    async def new(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, 'new')

    @discord.ui.button(label='持っていても使う', emoji='⬆', style=discord.ButtonStyle.primary)
    async def all(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, 'all')

    @discord.ui.button(label='使わない', emoji='✖', style=discord.ButtonStyle.secondary)
    async def off(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, 'off')


class CancelView(discord.ui.View):
    """「中止」ボタン。ジョブ番号入りの custom_id で対象ジョブを区別する

    job を渡さない呼び出し方は旧仕様（再起動前のメッセージに残ったボタン用）。
    """

    def __init__(self, job: FarmJob | None = None):
        super().__init__(timeout=None)
        self.job = job
        if job is None:
            suffix, tag = '', ''
        else:
            suffix, tag = f':{job.user.id}:{job.jid}', f' #{job.jid}'

        refresh = discord.ui.Button(label=f'更新{tag}', style=discord.ButtonStyle.primary,
                                    emoji='🔄', custom_id=f'ywp:refresh{suffix}')
        refresh.callback = self._refresh
        self.add_item(refresh)

        pause = discord.ui.Button(label='一時停止', style=discord.ButtonStyle.secondary,
                                  emoji='⏸', custom_id=f'ywp:pause{suffix}')
        pause.callback = self._pause
        self.add_item(pause)

        resume = discord.ui.Button(label='再開', style=discord.ButtonStyle.success,
                                   emoji='▶', custom_id=f'ywp:resume{suffix}')
        resume.callback = self._resume
        self.add_item(resume)

        button = discord.ui.Button(label=f'中止{tag}', style=discord.ButtonStyle.danger,
                                   emoji='⏹', custom_id=f'ywp:cancel{suffix}')
        button.callback = self._cancel
        self.add_item(button)

        # 好物の使い方は自動編成のジョブだけ（それ以外の面では選べない）
        if job is not None and job.auto_deck:
            food = discord.ui.Button(label='好物', style=discord.ButtonStyle.secondary,
                                     emoji='🍙', custom_id=f'ywp:food{suffix}')
            food.callback = self._food
            self.add_item(food)

    async def _food(self, interaction: discord.Interaction):
        job = self.job
        if job is None or job.cancel.is_set() or find_job(job.user.id, job.jid) is None:
            return await interaction.response.send_message(
                '❌ 実行中のジョブがありません（既に終了しています）', ephemeral=True)
        await interaction.response.send_message(
            f'🍙 **#{job.jid}** の好物の使い方を選んでください'
            f'（今：**{FOOD_MODES.get(job.food_mode, job.food_mode)}**）。\n'
            '好物をあげれば仲間になるバトルだけあげます。変更は次のバトルから効きます。',
            view=FoodModeView(interaction.user.id, job), ephemeral=True)

    async def _pause(self, interaction: discord.Interaction):
        job = self.job or find_job(interaction.user.id)
        if job is None:
            return await interaction.response.send_message(
                '❌ 実行中のジョブがありません（既に終了しています）', ephemeral=True)
        if job.cancel.is_set():
            return await interaction.response.send_message(
                '⏹ このジョブは中止処理中です。', ephemeral=True)
        if not pause_job(job):
            return await interaction.response.send_message(
                '⏸ 既に一時停止しています。**▶ 再開** で続行できます。', ephemeral=True)
        await interaction.response.send_message(
            f'⏸ **#{job.jid}**（ステージ `{job.stage_id}`）を一時停止しました。\n'
            '進行中のバトルを正常終了させてから止まります。'
            '**▶ 再開** を押すまで次のバトルは始めません。', ephemeral=True)

    async def _resume(self, interaction: discord.Interaction):
        job = self.job or find_job(interaction.user.id)
        if job is None:
            return await interaction.response.send_message(
                '❌ 実行中のジョブがありません（既に終了しています）', ephemeral=True)
        if not resume_job(job):
            return await interaction.response.send_message(
                '▶ 一時停止していません（そのまま周回中です）。', ephemeral=True)
        await interaction.response.send_message(
            f'▶ **#{job.jid}**（ステージ `{job.stage_id}`）を再開しました。'
            f'（停止していた時間 {fmt_sec(job.paused_total)}）', ephemeral=True)

    async def _refresh(self, interaction: discord.Interaction):
        job = self.job or find_job(interaction.user.id)
        if job is None:
            return await interaction.response.send_message(
                '❌ 実行中のジョブがありません（既に終了しています）', ephemeral=True)
        await do_refresh(interaction, job)

    async def _cancel(self, interaction: discord.Interaction):
        job = self.job or find_job(interaction.user.id)
        if job is None:
            return await interaction.response.send_message(
                '❌ 実行中のジョブがありません（既に終了しています）', ephemeral=True)
        if job.cancel.is_set():
            return await interaction.response.send_message(
                '⏳ 既に中止を受け付けています。進行中のバトルを閉じてから停止します。', ephemeral=True)
        job.cancel.set()
        job.status = '⏹ 中止中...'
        await interaction.response.send_message(
            f'⏹ **#{job.jid}**（ステージ `{job.stage_id}`）の中止を受け付けました。'
            '進行中のバトルを正常終了させてから停止します。', ephemeral=True)


class JobsCancelView(discord.ui.View):
    """複数ジョブの「状況確認」用。ジョブごとに中止ボタンを並べる"""

    def __init__(self, jobs: list[FarmJob]):
        super().__init__(timeout=300)
        self.jobs = jobs[:5]        # ボタンは1行に5個まで
        uid = jobs[0].user.id if jobs else 0
        refresh = discord.ui.Button(
            label='更新', style=discord.ButtonStyle.primary, emoji='🔄', row=0,
            custom_id=f'ywp:refreshall:{uid}')
        refresh.callback = self._refresh_all
        self.add_item(refresh)

        pause_all = discord.ui.Button(
            label='全部一時停止', style=discord.ButtonStyle.secondary, emoji='⏸', row=0,
            custom_id=f'ywp:pauseall:{uid}')
        pause_all.callback = self._pause_all
        self.add_item(pause_all)

        resume_all = discord.ui.Button(
            label='全部再開', style=discord.ButtonStyle.success, emoji='▶', row=0,
            custom_id=f'ywp:resumeall:{uid}')
        resume_all.callback = self._resume_all
        self.add_item(resume_all)

        for job in self.jobs:
            button = discord.ui.Button(
                label=f'⏹ #{job.jid} {job.player_name or job.stage_id}'[:80],
                style=discord.ButtonStyle.danger, row=1,
                custom_id=f'ywp:cancel:{job.user.id}:{job.jid}')
            button.callback = self._make_callback(job)
            self.add_item(button)

    def _live(self) -> list[FarmJob]:
        return [j for j in self.jobs
                if not j.cancel.is_set() and find_job(j.user.id, j.jid) is not None]

    async def _pause_all(self, interaction: discord.Interaction):
        live = self._live()
        n = sum(1 for j in live if pause_job(j))
        if not live:
            return await interaction.response.send_message(
                '⏹ 表示中のジョブは既に終了しています。', ephemeral=True)
        await interaction.response.send_message(
            f'⏸ {n}件を一時停止しました'
            + (f'（残り{len(live) - n}件は停止済み）' if n < len(live) else '')
            + '。進行中のバトルを閉じてから止まります。', ephemeral=True)

    async def _resume_all(self, interaction: discord.Interaction):
        live = self._live()
        n = sum(1 for j in live if resume_job(j))
        if not live:
            return await interaction.response.send_message(
                '⏹ 表示中のジョブは既に終了しています。', ephemeral=True)
        await interaction.response.send_message(
            f'▶ {n}件を再開しました'
            + (f'（残り{len(live) - n}件は動作中でした）' if n < len(live) else '') + '。',
            ephemeral=True)

    async def _refresh_all(self, interaction: discord.Interaction):
        """並んでいるジョブをまとめて取り直してから描き直す"""
        live = [j for j in self.jobs
                if not j.cancel.is_set() and find_job(j.user.id, j.jid) is not None]
        if not live:
            return await interaction.response.send_message(
                '⏹ 表示中のジョブは既に終了しています。', ephemeral=True)

        await interaction.response.defer()
        for j in live:
            j.refreshed.clear()
            j.want_refresh = True
        # 全部揃うのを待つ。1件でも遅いと全体が待たされるので上限は共通
        waiters = [asyncio.create_task(j.refreshed.wait()) for j in live]
        _, pending = await asyncio.wait(waiters, timeout=REFRESH_WAIT)
        for t in pending:
            t.cancel()          # 残すと待ちっぱなしのタスクが積む
        try:
            await interaction.edit_original_response(embed=jobs_list_embed(live))
        except discord.HTTPException:
            pass

    def _make_callback(self, job: FarmJob):
        async def callback(interaction: discord.Interaction):
            if job.cancel.is_set():
                return await interaction.response.send_message(
                    '⏳ 既に中止を受け付けています。進行中のバトルを閉じてから停止します。',
                    ephemeral=True)
            job.cancel.set()
            job.status = '⏹ 中止中...'
            await interaction.response.send_message(
                f'⏹ **#{job.jid}**（ステージ `{job.stage_id}`）の中止を受け付けました。',
                ephemeral=True)
        return callback


def jobs_list_embed(jobs: list[FarmJob]) -> discord.Embed:
    """複数ジョブのサマリー（状況確認・/周回状況 用）"""
    e = discord.Embed(
        title=f'🎮 自動周回 実行中（{len(jobs)}件）',
        color=COLOR_RUN,
        timestamp=datetime.now(),
    )
    for job in jobs[:10]:
        name = (f'`#{job.jid}` 「{job.preset_name}」 `{job.stage_id}`' if job.is_preset
                else f'`#{job.jid}` ステージ `{job.stage_id}`')
        if job.player_name:
            name += f'（{job.player_name}）'
        e.add_field(name=name, value=f'{job_headline(job)}\n{job.status}',
                    inline=False)
    e.set_footer(text='下のボタンで個別に中止できます')
    return e


# ============================================================================
# 周回開始の2段階フロー用: ログイン結果の一時保持
# ============================================================================

PENDING_TTL = 900          # ログイン→周回設定モーダル送信までに許容する時間（秒）
PENDING_FARM: dict[int, 'FarmLoginCtx'] = {}


class FarmLoginCtx:
    """LoginModalで確認したアカウント/セーブ情報（周回設定モーダルに渡す）

    パスワードはモーダル送信までの間だけメモリ上に置く（保存・ログ出力なし）。
    周回本体（run_job）では毎回改めてログインし直すため、この情報は使い捨て。
    """
    __slots__ = ('email', 'password', 'accs', 'save_uid', 'save_name',
                 'preset_name', 'at')


class FarmModal(discord.ui.Modal, title='ぷにぷに 自動周回'):
    """周回設定。ctx（ログイン済み）があるときはメール/パス欄の代わりに
    セーブデータを名前から選ぶセレクトを出す（モーダルは5コンポーネントまで）"""

    def __init__(self, preset_name: str | None = None,
                 ctx: FarmLoginCtx | None = None):
        super().__init__()
        refresh_event_preset()
        self.preset_name = preset_name if preset_name in PRESETS else None
        self.preset = PRESETS[self.preset_name] if self.preset_name else None
        self._ctx = ctx

        if ctx is None:
            # フォールバック: ログイン済み情報が無いときは従来どおり入力させる
            self.email_label = discord.ui.Label(
                text='メールアドレス',
                description='ぷにぷにに連携しているメールアドレス（セーブ指定: 例 a@b.c#ぷに4453）',
                component=discord.ui.TextInput(
                    placeholder='example@example.com（セーブ指定: a@b.c#ぷに4453）',
                    max_length=120),
            )
            self.add_item(self.email_label)
            self.password_label = discord.ui.Label(
                text='パスワード',
                description='保存・ログ出力はされません',
                component=discord.ui.TextInput(
                    placeholder='ログイン用パスワード', max_length=100),
            )
            self.add_item(self.password_label)

        # プリセット指定時はステージID欄の代わりにプリセットのステージ群を
        # 順番に周回する
        if self.preset is None:
            self.stage_label = discord.ui.Label(
                text='ステージID',
                description='クリア済みのステージID、またはプリセット名（例: イベステ全周回）',
                component=discord.ui.TextInput(placeholder='例: 10010101', max_length=12),
            )
            self.add_item(self.stage_label)

        self.count_label = discord.ui.Label(
            text='周回数 / 目標',
            description=('周回数、または「無限」（中止するまで回す）。'
                         'P12000=Yポイント12000到達、P+3000=3000増加、Mはマネー。'
                         'プリセット時は「全ステージを何周するか」（1=各ステージ1回ずつ）'),
            component=discord.ui.TextInput(
                placeholder='10 / 無限 / P12000 / P+3000 / M+50000', default='10',
                max_length=16),
        )
        self.add_item(self.count_label)
        self.timing_label = discord.ui.Label(
            text='バトル時間 , クールダウン（秒・任意）',
            description=(f'空欄で既定値。バトルは{BATTLE_TIME_MIN:g}秒、'
                         f'クールダウンは{COOLDOWN_MIN:g}秒を下回りません'
                         f'（1戦あたり約10〜12秒）'),
            component=discord.ui.TextInput(
                placeholder=f'{DEFAULT_BATTLE_TIME:g},{DEFAULT_COOLDOWN:g}',
                required=False, max_length=20),
        )
        self.add_item(self.timing_label)

        # ログイン済みのときはセーブデータを名前から選択する
        if ctx is not None and ctx.accs:
            uid = str(ctx.save_uid) if ctx.save_uid else str(ctx.accs[0].get('userId'))
            opts = list(ctx.accs[:25])       # セレクトは1メニュー25個まで
            if uid not in {str(a.get('userId')) for a in opts}:
                # 選択済みセーブが25件目以降のときは末尾と差し替えて必ず含める
                extra = next((a for a in ctx.accs if str(a.get('userId')) == uid), None)
                if extra is not None:
                    opts = opts[:-1] + [extra]
            options = []
            for a in opts:
                options.append(discord.SelectOption(
                    label=str(a.get('playerName') or 'プレイヤー名不明')[:100],
                    value=str(a.get('userId')),
                    description=f"userId: {a.get('userId')}"[:100],
                    default=str(a.get('userId')) == uid))
            self.save_label = discord.ui.Label(
                text='セーブデータ',
                description='周回で使うセーブデータを選択してください',
                component=discord.ui.Select(
                    placeholder='周回するセーブデータ', options=options),
            )
            self.add_item(self.save_label)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        # --- ステージ/プリセット確定 ---
        refresh_event_preset()
        stage_ids, preset_name = None, self.preset_name
        if self.preset is not None:
            stage_ids = list(self.preset['ids'])
            stage_id = stage_ids[0]
        else:
            raw = self.stage_label.component.value.strip()
            found = find_preset(raw)
            if found is not None:
                # 手動欄にプリセット名がタイピングされた場合も展開する
                preset_name, p = found
                stage_ids = list(p['ids'])
                stage_id = stage_ids[0]
            else:
                try:
                    stage_id = int(raw)
                    if stage_id <= 0:
                        raise ValueError
                except ValueError:
                    return await interaction.followup.send(
                        '❌ ステージIDは正の整数で入力するか、プリセットを選択してください',
                        ephemeral=True)

        try:
            count, tkey, tvalue, tdelta = parse_count_or_target(
                self.count_label.component.value)
        except ValueError as e:
            return await interaction.followup.send(f'❌ {e}', ephemeral=True)

        battle_time, cooldown = DEFAULT_BATTLE_TIME, DEFAULT_COOLDOWN
        raw = self.timing_label.component.value.strip()
        if raw:
            parts = [p.strip() for p in raw.replace('，', ',').split(',') if p.strip()]
            try:
                if len(parts) >= 1:
                    battle_time = float(parts[0])
                if len(parts) >= 2:
                    cooldown = float(parts[1])
            except ValueError:
                return await interaction.followup.send(
                    '❌ バトル時間/クールダウンは `7.5,3` のように数値をカンマ区切りで入力してください',
                    ephemeral=True)
        # 下限は必ず守る
        battle_time = max(battle_time, BATTLE_TIME_MIN)
        cooldown = max(cooldown, COOLDOWN_MIN)

        running = jobs_of(interaction.user.id)
        if len(running) >= MAX_JOBS_PER_USER:
            return await interaction.followup.send(
                f'❌ 同時実行できるのは {MAX_JOBS_PER_USER}ジョブまでです'
                '（複垢周回もこの数まで同時に投げられます）。\n'
                'DMや「状況確認」の中止ボタンで止めてから追加してください。',
                ephemeral=True)

        # --- アカウント/セーブデータ確定 ---
        if self._ctx is not None:
            ctx = self._ctx
            if time.time() - ctx.at > PENDING_TTL:
                return await interaction.followup.send(
                    '⏰ ログインから時間が経ったため、有効期限が切れました。\n'
                    'パネルの **🎮 周回開始** からやり直してください。', ephemeral=True)
            email, password = ctx.email, ctx.password
            vals = (self.save_label.component.values
                    if hasattr(self, 'save_label') else [])
            save_hint = vals[0] if vals else (str(ctx.save_uid) if ctx.save_uid else None)
            save_name = next(
                (str(a.get('playerName') or a.get('userId')) for a in ctx.accs
                 if str(a.get('userId')) == (save_hint or '')), None)
        else:
            email, save_hint = split_email_save(self.email_label.component.value)
            password = self.password_label.component.value
            save_name = save_hint

        # 自動編成の対象ステージを単独で周回するときだけ、ドロップと自動編成を聞く
        drop_asked = auto_deck = False
        if not stage_ids and stage_id == AUTO_DECK_STAGE:
            drop_asked = True
            ask = DropDeckConfirmView(interaction.user.id)
            await interaction.followup.send(
                f'🤝 ステージ `{stage_id}` を周回します。\n'
                '**ドロップと自動編成を有効にしますか？**\n'
                '有効にすると、妖怪が仲間になったらその妖怪をリーダーにします'
                '（すでにデッキにいる場合はそのまま）。\n'
                f'{AUTO_DECK_ASK_TIMEOUT}秒以内に選ばないと無効のまま開始します。',
                view=ask, ephemeral=True)
            await ask.wait()
            auto_deck = bool(ask.choice)

        # 自動編成を有効にしたときだけ、好物の使い方を聞く
        food_mode = 'off'
        if auto_deck:
            ask_food = FoodModeView(interaction.user.id)
            await interaction.followup.send(
                '🍙 **好物を使いますか？**\n'
                '好物をあげれば仲間になるバトルだけあげます（ランクは当たるうち一番安いもの）。'
                '好物は買えるランクごとに3個ずつ買い置きします（Yマネーは10,000以上残します）。\n'
                '・**まだ持っていない妖怪だけ** … すでに仲間にしている妖怪にはあげない\n'
                '・**持っていても使う** … 重ねて仲間にすると必殺技Lvが上がる\n'
                f'{AUTO_DECK_ASK_TIMEOUT}秒以内に選ばないと「使わない」で開始します。',
                view=ask_food, ephemeral=True)
            await ask_food.wait()
            food_mode = ask_food.choice or 'off'

        # 編成の妖怪のレベル上限を自動で解放するか（どの周回でも聞く）
        ask_lv = YesNoView(interaction.user.id,
                           '✅ レベル上限の自動解放を**有効**にして開始します',
                           '✖ レベル上限の自動解放は**無効**で開始します')
        await interaction.followup.send(
            '📈 **編成の妖怪のレベル上限を自動で解放しますか？**\n'
            f'Lv10・20・30・40 の上限で止まったら Yマネーで解放して、Lv{LEVEL_OPEN_MAX} まで上げます'
            f'（Lv{LEVEL_OPEN_MAX} から先は Yポイントが要るので解放しません）。\n'
            f'{AUTO_DECK_ASK_TIMEOUT}秒以内に選ばないと無効のまま開始します。',
            view=ask_lv, ephemeral=True)
        await ask_lv.wait()

        # まだ育っていない垢なら、安全な速さで回すか、育っている垢として速く回すかを聞く
        uid = save_hint if (save_hint or '').isdigit() else None
        only_special = (stage_ids or [stage_id]) == [AUTO_DECK_STAGE]
        if uid and not only_special and not is_trusted(uid):
            ask_fast = YesNoView(interaction.user.id,
                                 '⚡ **育っている垢**として、速さの制限なしで開始します',
                                 '🛡 **安全な速さ**で開始します（育つまで待ち時間を取ります）')
            ask_fast.yes.label, ask_fast.no.label = '育っている垢なので速く回す', '安全な速さで回す（おすすめ）'
            await interaction.followup.send(
                f'🛡 **この垢は、BOTで勝った回数が {trust_wins(uid)}回です。**\n'
                f'{TRUST_BATTLES}勝するまでは、弾かれない安全な速さで回します（作ったばかりの垢を速く'
                '回すと、サーバーに弾かれて（rc=4）BANされるため）。\n'
                'アプリで長く遊んできた垢など、**育っているのが確かな垢だけ**「速く回す」を選んでください。'
                '作ったばかりの垢で選ぶとBANの危険があります。\n'
                f'{AUTO_DECK_ASK_TIMEOUT}秒以内に選ばないと安全な速さで開始します。',
                view=ask_fast, ephemeral=True)
            await ask_fast.wait()
            if ask_fast.choice:
                trust_set_manual(uid)
                log_line(f'育った垢として扱う（周回開始時に選択） uid={uid} 勝ち数={trust_wins(uid)}')

        job = FarmJob(interaction.user, stage_id, count, battle_time, cooldown,
                      target_key=tkey, target_value=tvalue, target_delta=tdelta,
                      stage_ids=stage_ids, preset_name=preset_name)
        job.drop_asked, job.auto_deck = drop_asked, auto_deck
        job.food_mode = food_mode
        job.auto_levelup = bool(ask_lv.choice)
        job.resume_email = email        # 再起動後に続きから回すため（パスワードは持たない）
        add_job(job)
        self._job = job

        # DMが開いているか先に確認する（結果はDMで返すため）
        try:
            job.message = await interaction.user.send(
                embed=progress_embed(job), view=CancelView(job))
        except discord.Forbidden:
            drop_job(job)
            return await interaction.followup.send(
                '❌ DMを送れませんでした。\n'
                'サーバー設定 → プライバシー設定 →「サーバーメンバーからのDMを許可する」'
                'をONにしてから、もう一度お試しください。',
                ephemeral=True)

        avg_b = 1.0 + BATTLE_TIME_SPREAD / 2   # jitter の期待値倍率（バトル）
        avg_c = 1.0 + TIME_SPREAD / 2          # 同（クールダウン）
        # 連戦間隔の下限（MIN_BATTLE_CYCLE）を下回らないように見積もる
        per_battle = (max(battle_time * avg_b + cooldown * avg_c,
                          MIN_BATTLE_CYCLE) + REQUEST_DELAY)
        n_stages = len(stage_ids) if stage_ids else 1
        eta = None if count is None else count * n_stages * per_battle
        job.task = asyncio.create_task(
            run_job(job, email, password, save_hint))

        if tkey is None:
            if count is None:
                goal = '無限（中止で停止）'
            elif preset_name:
                goal = f'{count}周（全{count * n_stages}戦・各ステージ{count}回ずつ）'
            else:
                goal = f'{count}回'
            when = ('> 中止するまで回し続けます\n' if eta is None
                    else f'> 予想所要時間: 約{fmt_sec(eta)}\n')
        else:
            label = WALLET_LABEL.get(tkey, tkey)
            goal = (f'{label} +{tdelta:,} になるまで' if tdelta is not None
                    else f'{label} {tvalue:,} になるまで')
            goal += '（周回数上限なし）' if count is None else f'（上限 {count}回）'
            when = ('> 所要時間は1周あたりの増加量が分かってから出します\n'
                    f'> 所持数の確認は{WALLET_REFRESH_EVERY}周ごと（目標間際は毎周）\n')

        save_note = f'\n> ログインセーブ: **{save_name}**' if save_name else ''
        stage_note = (f'> プリセット **{preset_name}**（{n_stages}ステージを順番に周回） / {goal}'
                      if preset_name else f'> ステージ `{stage_id}` / {goal}')
        await interaction.followup.send(
            f'✅ 受け付けました（ジョブ **#{job.jid}**）。\n'
            f'{stage_note}\n'
            f'> バトル {battle_time:g}〜{battle_time * (1 + BATTLE_TIME_SPREAD):.1f}秒 / '
            f'クールダウン {cooldown:g}〜{cooldown * (1 + TIME_SPREAD):.1f}秒'
            f'（1戦あたり約10〜12秒）'
            + save_note + '\n'
            + when +
            f'進捗と完了通知は **DM** に送ります。',
            ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        job = getattr(self, '_job', None)
        if job is not None:
            drop_job(job)
        await report_error(interaction, error, 'FarmModal')


class StageListModal(discord.ui.Modal, title='ステージ一覧'):
    email = discord.ui.Label(
        text='メールアドレス',
        description='ぷにぷにに連携しているメールアドレス（セーブ指定: 例 a@b.c#ぷに4453）',
        component=discord.ui.TextInput(
            placeholder='example@example.com（セーブ指定: a@b.c#ぷに4453）', max_length=120),
    )
    password = discord.ui.Label(
        text='パスワード',
        description='保存・ログ出力はされません',
        component=discord.ui.TextInput(
            placeholder='ログイン用パスワード', max_length=100),
    )
    keyword = discord.ui.Label(
        text='検索キーワード（任意）',
        description='ステージIDの一部で絞り込み',
        component=discord.ui.TextInput(
            placeholder='例: 1001', required=False, max_length=12),
    )
    scope = discord.ui.Label(
        text='表示範囲',
        component=discord.ui.Select(options=[
            discord.SelectOption(label='クリア済みのみ', value='cleared',
                                 emoji='✅', default=True),
            discord.SelectOption(label='未クリアも含めて全部', value='all', emoji='📋'),
            discord.SelectOption(label='マスタ込み（未解放も・.txtで添付）',
                                 value='master', emoji='🗂',
                                 description='getMaster.nhn から全ステージを取得します'),
        ]),
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        email, save_hint = split_email_save(self.email.component.value)
        password = self.password.component.value
        kw = self.keyword.component.value.strip()
        vals = self.scope.component.values
        scope = vals[0] if vals else 'cleared'
        show_all = scope in ('all', 'master')
        include_master = scope == 'master'

        async def _send_prompt(view, accs):
            return await interaction.followup.send(
                f'🗂 このアカウントには **{len(accs)}件** のセーブデータがあります。'
                'ログインするセーブデータを選択してください'
                f'（{SAVE_SELECT_TIMEOUT // 60}分でタイムアウト）',
                view=view, ephemeral=True, wait=True)

        lease = await lease_proxy()
        try:
            c, save_uid, _ = await prepare_login(email, password,
                                                 interaction.user.id, _send_prompt,
                                                 save_hint=save_hint,
                                                 proxy=lease.url if lease is not None else None)
            await asyncio.to_thread(c.login, save_uid)
        except Exception as e:
            return await interaction.followup.send(
                f'❌ ログイン失敗: `{str(e)[:250]}`', ephemeral=True)
        finally:
            if lease is not None:
                lease.release()
            email = password = None
        try:
            name = c.raw_info().get('playerName')
        except (ValueError, AttributeError):
            name = None
        remember_session(interaction.user.id, c, name)
        await send_stage_list(interaction, c, kw, show_all, include_master)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await report_error(interaction, error, 'StageListModal')


class AccountCountModal(discord.ui.Modal, title='アカウント作成'):
    """作成数・固定パスワード・アカウント名を入力してもらうモーダル

    aged=True で「久垢作成」用になる（セーブ作成後に初回ステージまでクリアする）。
    """

    count = discord.ui.Label(
        text='作成数',
        description=f'1〜{MAX_ACCOUNTS_PER_RUN:,} の範囲で指定'
                    f'（{ACCOUNT_TXT_THRESHOLD}件以上は .txt にまとめてDMします）',
        component=discord.ui.TextInput(
            placeholder=f'1〜{MAX_ACCOUNTS_PER_RUN:,}', default='1', max_length=6),
    )
    password = discord.ui.Label(
        text='パスワード（固定・任意）',
        description='空欄なら毎回ランダム生成。入力すると全アカウントで同じパスワードになります',
        component=discord.ui.TextInput(
            placeholder='空欄でランダム生成', required=False, max_length=100),
    )
    name = discord.ui.Label(
        text='アカウント名（任意）',
        description=f'空欄なら「ぷに＋4桁」をランダム生成。{PLAYER_NAME_MAX}文字以内。'
                    '`{n}` と書くと連番に置き換わります（例: テスト{n} → テスト1, テスト2）',
        component=discord.ui.TextInput(
            placeholder='例: テスト{n}', required=False, max_length=PLAYER_NAME_MAX + 3),
    )

    def __init__(self, aged: bool = False):
        super().__init__(title='久垢作成' if aged else 'アカウント作成')
        self.aged = aged

    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(self.count.component.value.strip().replace(',', ''))
        except ValueError:
            n = 0
        if not (1 <= n <= MAX_ACCOUNTS_PER_RUN):
            return await interaction.response.send_message(
                f'❌ 作成数は 1〜{MAX_ACCOUNTS_PER_RUN:,} の整数で指定してください。',
                ephemeral=True)
        await start_account_creation(
            interaction, n, self.password.component.value,
            self.name.component.value, aged=self.aged)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await report_error(interaction, error, 'AccountCountModal')


class VipAccountModal(AccountCountModal):
    """回し放題垢作成: 作成数・パスワード・アカウント名に、まわし放題ガシャの連数を足したモーダル"""

    turns = discord.ui.Label(
        text='まわし放題ガシャの連数',
        description=f'既定 {VIP_GACHA_COUNT}。サーバーの上限（今は240）を超える指定は上限にします'
                    '（超えて送ると弾かれて垢が危ないため）',
        component=discord.ui.TextInput(
            placeholder=str(VIP_GACHA_COUNT), default=str(VIP_GACHA_COUNT), max_length=4),
    )

    def __init__(self, paid: bool = False):
        super().__init__()
        self.paid = paid
        self.title = '回し放題垢 購入' if paid else '回し放題垢作成'
        if paid:
            self.count.description = (f'1〜{VIP_PAID_MAX} の範囲で指定。'
                                      f'1垢 ¥{VIP_PRICE:,}（お支払いはPayPay）')
            self.count.component.placeholder = f'1〜{VIP_PAID_MAX}'

    async def on_submit(self, interaction: discord.Interaction):
        top = VIP_PAID_MAX if self.paid else MAX_ACCOUNTS_PER_RUN
        try:
            n = int(unicodedata.normalize('NFKC', self.count.component.value).strip().replace(',', ''))
            turns = int(unicodedata.normalize('NFKC', self.turns.component.value).strip())
        except ValueError:
            n = turns = 0
        if not (1 <= n <= top):
            return await interaction.response.send_message(
                f'❌ 作成数は 1〜{top:,} の整数で指定してください。', ephemeral=True)
        if turns < 1:
            return await interaction.response.send_message(
                '❌ 連数は 1 以上の整数で指定してください。', ephemeral=True)
        if not self.paid:
            return await start_account_creation(
                interaction, n, self.password.component.value,
                self.name.component.value, vip=turns)
        await start_vip_order(interaction, n, turns, self.password.component.value,
                              self.name.component.value)


# ============================================================================
# 有料版の支払い（PayPay 請求リンク＋取引番号照合。AutoTsum と同じ方式・同じ受取口座）
# ============================================================================

PAY_STEPS_TEXT = (
    '**お支払い方法**\n'
    '1. **💴 請求リンクを表示** を押し、表示されたリンクからPayPayで支払う\n'
    '2. PayPayアプリの **取引履歴 → 該当の取引 → 詳細** にある **取引番号** をコピー\n'
    '3. **🧾 取引番号を入力** に貼り付け → 確認できたら自動で作成を始めます')
PAY_RECOVER_TEXT = ('-# この画面が消えたり、ボタンが反応しなくなったときは、パネルの '
                    '**🧾 お支払い途中のご注文** から続きができます（二重に支払う必要はありません）。')
PAY_TIMEOUT = 3600       # 支払い画面のボタンが使える時間（秒）


def vip_sale_blocker(user_id: int, name_tpl: str | None = None) -> str | None:
    """有料版を今受け付けられない理由。受け付けられるなら None（支払いの前に必ず確かめる）"""
    if VIP_PRICE <= 0:
        return '🚧 現在、有料版は受付を停止しています（料金が未設定です）。'
    if not puni_pay.is_ready():
        return f'🚧 {puni_pay.NO_WALLET_MSG}'
    if not vip_gacha_status():
        return ('🚧 現在、まわし放題ガシャが開催されていない（または開催を確認できていない）ため、'
                '受付を停止しています。')
    return creation_blocker(user_id, name_tpl)


class PaySession:
    """1件の支払い。請求リンクを使い回し、入金を確認できたら一度だけ on_paid を呼ぶ"""

    def __init__(self, *, amount: int, kind: str, user_id: int, on_paid, link=None, on_link=None):
        self.amount = int(amount)
        self.kind = kind
        self.user_id = user_id
        self.on_paid = on_paid          # async (interaction, 取引番号)
        self.link = link                # 途中復帰のときは前に出した請求リンクを使い回す
        self.on_link = on_link          # 請求リンクを作ったときに呼ぶ（注文の記録に残す）
        self.paid = False
        self.order_id = None
        self._lock = asyncio.Lock()

    async def billing_link(self):
        async with self._lock:
            if self.link:
                return self.link, ''
            link, err = await asyncio.to_thread(puni_pay.create_billing_link, self.amount)
            if link:
                self.link = link
                if self.on_link:
                    self.on_link(link)
            return link, err

    async def confirm(self, interaction: discord.Interaction, raw_order_id: str):
        """取引番号を照合する。→ (ok, エラー文)。ok なら on_paid まで実行済み"""
        async with self._lock:
            if self.paid:
                return False, 'このお支払いは確認済みです。'
            oid = puni_pay.normalize_order_id(raw_order_id)
            if not oid:
                return False, '取引番号を入力してください。'
            try:
                prev = await asyncio.to_thread(USED_ORDERS.get_info, oid)
            except puni_pay.StoreError as e:
                print(f'[支払い] {e}')
                return False, 'お支払いの確認で問題が発生しました。管理者にご連絡ください。'
            if prev is not None:
                return False, (f'この取引番号は既に使用済みです（{prev.get("used_at_jst") or "日時不明"}）。\n'
                               '新しくお支払いいただき、その取引番号を入力してください。')
            ok, msg, detail = await asyncio.to_thread(puni_pay.verify, self.amount, oid)
            if not ok:
                return False, msg
            # 照合できた明細の正規IDで消費を確定してから作り始める（同じ入金で二重に通さない）
            canon = puni_pay.canonical_order_id(detail, oid)
            try:
                fresh = await asyncio.to_thread(
                    USED_ORDERS.try_consume, canon, user_id=interaction.user.id,
                    guild_id=interaction.guild_id, amount=self.amount, kind=self.kind)
            except puni_pay.StoreError as e:
                print(f'[支払い] {e}')
                return False, ('お支払いの記録に失敗しました。少し待って同じ取引番号でもう一度お試しいただくか、'
                               '管理者にご連絡ください。')
            if not fresh:
                return False, 'この取引は既に使用済みです。新しくお支払いいただき、その取引番号を入力してください。'
            self.paid, self.order_id = True, canon
        log_line(f'[支払い] {self.kind} user={interaction.user.id} ¥{self.amount:,} 取引番号={canon}')
        try:
            await self.on_paid(interaction, canon)
        except Exception:
            print(f'[支払い] 作成の開始でエラー\n{traceback.format_exc()}')
            try:
                await interaction.followup.send(
                    '⚠ 作成の開始中に問題が発生しました。お支払いは確認済みです。\n'
                    f'取引番号 `{canon}` を添えて管理者にご連絡ください。', ephemeral=True)
            except discord.HTTPException:
                pass
            await notify_owners(f'⚠ 有料 {self.kind} の開始に失敗: {interaction.user} '
                                f'({interaction.user.id}) 取引番号 `{canon}` ¥{self.amount:,}')
        return True, ''


class PayView(discord.ui.View):
    """請求リンクの表示と取引番号の入力（申し込んだ本人だけが押せる）"""

    def __init__(self, ps: PaySession):
        super().__init__(timeout=PAY_TIMEOUT)
        self.ps = ps

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ps.user_id:
            return True
        await interaction.response.send_message(
            'このお支払いを操作できるのは、お申し込みのご本人だけです。', ephemeral=True)
        return False

    @discord.ui.button(label='請求リンクを表示', style=discord.ButtonStyle.primary, emoji='💴')
    async def show_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.ps.paid:
            return await interaction.response.send_message('このお支払いは確認済みです。', ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        link, err = await self.ps.billing_link()
        if not link:
            return await interaction.followup.send(f'❌ {err}', ephemeral=True)
        e = discord.Embed(title=f'💴 請求リンク ¥{self.ps.amount:,}', color=COLOR_RUN,
                          description=f'{link}\n\nお支払い後、**🧾 取引番号を入力** から取引番号を送ってください。\n'
                                      '-# 取引番号は PayPayアプリの 取引履歴 → 該当の取引 → 詳細 に表示されます')
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label='PayPayで支払う', style=discord.ButtonStyle.link, url=link))
        await interaction.followup.send(embed=e, view=view, ephemeral=True)

    @discord.ui.button(label='取引番号を入力', style=discord.ButtonStyle.success, emoji='🧾')
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.ps.paid:
            return await interaction.response.send_message('このお支払いは確認済みです。', ephemeral=True)
        await interaction.response.send_modal(PayTxnModal(self.ps))


class PayTxnModal(discord.ui.Modal, title='取引番号を入力'):
    code = discord.ui.TextInput(label='取引番号', placeholder='PayPayの取引履歴 → 取引 → 詳細 → 取引番号',
                                min_length=4, max_length=64)

    def __init__(self, ps: PaySession):
        super().__init__()
        self.ps = ps

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, err = await self.ps.confirm(interaction, self.code.value)
        if not ok:
            await interaction.followup.send(f'❌ {err}', ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await report_error(interaction, error, 'PayTxnModal')


async def start_vip_order(interaction: discord.Interaction, count: int, turns: int,
                          password: str | None, player_name: str | None):
    """有料版の申し込み。受け付けられるか確かめてから、料金と支払いボタンを出す"""
    name_tpl = (player_name or '').strip() or None
    why = vip_sale_blocker(interaction.user.id, name_tpl)
    if why:
        return await interaction.response.send_message(why, ephemeral=True)
    vip = vip_gacha_status() or {}
    limit = int(vip.get('limit') or turns)
    order = {'kind': 'new', 'user_id': interaction.user.id, 'count': count,
             'turns': min(turns, limit), 'unit': VIP_PRICE, 'amount': VIP_PRICE * count,
             'password': (password or '').strip() or None, 'name': name_tpl,
             'gacha': f'{vip.get("name") or "まわし放題ガシャ"}・{str(vip.get("end") or "")[:16]} まで'}
    ps, e = vip_order_session(pending_put(order))
    await interaction.response.send_message(embed=e, view=PayView(ps), ephemeral=True)


# ============================================================================
# 支払い途中の注文（請求の画面はお客様にしか見えず、閉じたり時間がたつと消える。
# 注文を data/vip_pending.json に残し、パネルの「🧾 お支払い途中のご注文」から続きができる。
# 再起動しても残る。支払いを確かめたら paid を付け、終わったら消す）
# ============================================================================

PENDING_FILE = os.path.join(BASE_DIR, 'data', 'vip_pending.json')
PENDING_TTL = 24 * 3600          # 支払い待ちの注文を残す時間（秒）
PAY_SESSIONS: dict[int, PaySession] = {}     # ユーザーID → 今の支払い（同じ注文なら使い回す）


def pending_load() -> dict:
    try:
        with open(PENDING_FILE, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def pending_save(d: dict):
    try:
        os.makedirs(os.path.dirname(PENDING_FILE), exist_ok=True)
        tmp = PENDING_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PENDING_FILE)
    except OSError as e:
        print(f'[注文] 保存に失敗: {e}')


def pending_put(order: dict) -> dict:
    """新しい注文を残す（1人1件。前の支払い待ちは置き換える。支払い済みで作業中のものは残す）"""
    d = pending_load()
    now = time.time()
    d = {k: v for k, v in d.items() if v.get('paid') or now - float(v.get('created') or 0) < PENDING_TTL}
    order = dict(order, created=now, link=None, paid=None)
    key = str(order['user_id'])
    if d.get(key, {}).get('paid'):
        key = f'{key}:{int(now)}'       # 作業中の注文は消さない（別の名前で残す）
    order['key'] = key
    d[key] = order
    pending_save(d)
    return order


def pending_update(key: str, **kw):
    d = pending_load()
    if key in d:
        d[key].update(kw)
        pending_save(d)


def pending_drop(key: str):
    d = pending_load()
    if d.pop(key, None) is not None:
        pending_save(d)


def pending_for(user_id: int) -> dict | None:
    """このユーザーの、支払い待ち（期限内）または支払い済みで作業中の注文。新しいものを優先"""
    now = time.time()
    mine = [v for v in pending_load().values()
            if v.get('user_id') == user_id
            and (v.get('paid') or now - float(v.get('created') or 0) < PENDING_TTL)]
    return max(mine, key=lambda v: float(v.get('created') or 0)) if mine else None


def order_embed(order: dict) -> discord.Embed:
    if order['kind'] == 'new':
        head = (f'**{order["count"]}件** × ¥{order["unit"]:,} = **¥{order["amount"]:,}**\n'
                f'まわし放題ガシャ **{order["turns"]}連**（{order["gacha"]}）')
        title = '🎰 回し放題垢 ご注文内容'
    else:
        head = (f'セーブデータ **{order["pname"]}**（userId `{order["save_uid"]}`）\n'
                f'{order["gacha"]} **{order["turns"]}連** … **¥{order["amount"]:,}**\n'
                '✅ この垢で引けることを確認しました')
        title = '🎰 既存の垢に回し放題 ご注文内容'
    tail = ('\n-# お支払いから完了までゲームアプリは開かないでください' if order['kind'] == 'exist' else '')
    return discord.Embed(title=title, color=COLOR_RUN,
                         description=f'{head}\n\n{PAY_STEPS_TEXT}\n{PAY_RECOVER_TEXT}{tail}')


def vip_order_session(order: dict) -> tuple[PaySession, discord.Embed]:
    """注文の記録から支払い（PaySession）を組み立てる。最初の申し込みも途中復帰も同じもの"""
    key, uid, amount = order['key'], order['user_id'], int(order['amount'])
    tag = (key, order.get('created'))
    old = PAY_SESSIONS.get(uid)
    if old is not None and getattr(old, 'order_key', None) == tag and not old.paid:
        return old, order_embed(order)

    async def on_paid(inter: discord.Interaction, order_id: str):
        pending_update(key, paid=order_id, paid_at=time.time())
        user = inter.user
        try:
            if order['kind'] == 'new':
                await inter.followup.send(f'✅ お支払いを確認しました（¥{amount:,}）。回し放題垢の作成を始めます。',
                                          ephemeral=True)
                await notify_owners(f'💰 回し放題垢 有料注文: {user} ({user.id}) {order["count"]}件 × '
                                    f'¥{order["unit"]:,} = ¥{amount:,} ／ {order["turns"]}連 ／ 取引番号 `{order_id}`')
                await start_account_creation(inter, order['count'], order.get('password'), order.get('name'),
                                             vip=order['turns'], order=order_id, price=amount)
            else:
                await inter.followup.send(f'✅ お支払いを確認しました（¥{amount:,}）。**{order["pname"]}** で '
                                          f'{order["turns"]}連 引きます…（30秒ほど）', ephemeral=True)
                await notify_owners(f'💰 既存の垢に回し放題 有料注文: {user} ({user.id}) ¥{amount:,} ／ '
                                    f'{order["pname"]}（{order["save_uid"]}） {order["turns"]}連 ／ 取引番号 `{order_id}`')
                await run_paid_vip_exist(inter, order, order_id)
        finally:
            pending_drop(key)
            PAY_SESSIONS.pop(uid, None)

    ps = PaySession(amount=amount, kind='puni_vip' if order['kind'] == 'new' else 'puni_vip_exist',
                    user_id=uid, on_paid=on_paid, link=order.get('link'),
                    on_link=lambda link: pending_update(key, link=link))
    ps.order_key = tag
    PAY_SESSIONS[uid] = ps
    return ps, order_embed(order)


async def report_unfinished_orders():
    """起動時: 支払い済みなのに終わらなかった注文（作業中に落ちた）をオーナーに知らせる。
    期限切れの支払い待ち（パスワードや udkey を含む）はここで消す"""
    d = pending_load()
    now = time.time()
    keep = {k: v for k, v in d.items() if v.get('paid') or now - float(v.get('created') or 0) < PENDING_TTL}
    if len(keep) != len(d):
        pending_save(keep)
    left = [v for v in keep.values() if v.get('paid')]
    for v in left:
        await notify_owners(
            f'⚠ 支払い済みで未完了の有料注文があります（再起動で中断）: user {v.get("user_id")} ／ '
            f'取引番号 `{v.get("paid")}` ／ ¥{int(v.get("amount") or 0):,} ／ '
            + (f'初期垢 {v.get("count")}件 {v.get("turns")}連' if v.get('kind') == 'new'
               else f'既存の垢 {v.get("pname")}（{v.get("save_uid")}） {v.get("turns")}連')
            + '\n作れた分はお客様にDM済みです。足りない分の対応をお願いします（対応したら '
              f'`data/vip_pending.json` の `{v.get("key")}` を消してください）')
    if left:
        log_line(f'支払い済みで未完了の有料注文 {len(left)}件をオーナーに知らせた')


async def open_pending_order(interaction: discord.Interaction):
    """パネルの「🧾 お支払い途中のご注文」: 前の請求の画面をもう一度出す"""
    order = pending_for(interaction.user.id)
    if order is None:
        return await interaction.response.send_message(
            '🧾 お支払い待ちのご注文はありません（ご注文から24時間で消えます）。\n'
            'お支払い済みの場合は、同じ内容でもう一度お申し込みのうえ、その取引番号を入力してください'
            '（二重に支払う必要はありません）。', ephemeral=True)
    if order.get('paid'):
        return await interaction.response.send_message(
            f'✅ このご注文はお支払い確認済み（取引番号 `{order["paid"]}`）で、作業中です。'
            '完了するとDMでお届けします。届かないときは取引番号を添えて管理者にご連絡ください。',
            ephemeral=True)
    ps, e = vip_order_session(order)
    await interaction.response.send_message(embed=e, view=PayView(ps), ephemeral=True)


# ============================================================================
# 既存の垢に回し放題（お客様の垢にログインして まわし放題ガシャを引く。無料版・有料版）
# ============================================================================

VIP_EXIST_RUNNING: set[int] = set()     # 既存垢の回し放題を処理中のユーザーID


def vip_exist_blocker(user_id: int, paid: bool) -> str | None:
    """既存垢の回し放題を今受け付けられない理由。受け付けられるなら None"""
    if paid:
        if VIP_EXIST_PRICE <= 0:
            return '🚧 現在、有料版「既存の垢に回し放題」は受付を停止しています（料金が未設定です）。'
        if not puni_pay.is_ready():
            return f'🚧 {puni_pay.NO_WALLET_MSG}'
        if not vip_gacha_status():
            return ('🚧 現在、まわし放題ガシャが開催されていない（または開催を確認できていない）ため、'
                    '受付を停止しています。')
    mw = maint_window()
    if mw:
        return f'🛠 メンテ（{maint_text(mw)}）のため受付を止めています。管理者が再開するまでお待ちください。'
    if user_id in VIP_EXIST_RUNNING:
        return '⏳ 前のご依頼を処理中です。完了までお待ちください。'
    return None


class VipExistModal(discord.ui.Modal, title='既存の垢に回し放題'):
    """お客様の垢（メール/パスワード）と連数を入力してもらうモーダル"""

    email = discord.ui.Label(
        text='メールアドレス（LEVEL5 ID）',
        description='セーブデータが複数あるときは「メール#プレイヤー名」でも指定できます',
        component=discord.ui.TextInput(placeholder='example@gmail.com', max_length=200),
    )
    password = discord.ui.Label(
        text='パスワード',
        component=discord.ui.TextInput(max_length=100),
    )
    turns = discord.ui.Label(
        text='まわし放題ガシャの連数',
        description=f'既定 {VIP_GACHA_COUNT}。上限（今は240）を超える指定は上限にします',
        component=discord.ui.TextInput(
            placeholder=str(VIP_GACHA_COUNT), default=str(VIP_GACHA_COUNT), max_length=4),
    )

    def __init__(self, paid: bool = False):
        super().__init__(title='既存の垢に回し放題（購入）' if paid else '既存の垢に回し放題')
        self.paid = paid
        if paid:
            self.turns.description = (f'料金 ¥{VIP_EXIST_PRICE:,}（PayPay）。既定 {VIP_GACHA_COUNT}・'
                                      '上限を超える指定は上限にします')

    async def on_submit(self, interaction: discord.Interaction):
        try:
            turns = int(unicodedata.normalize('NFKC', self.turns.component.value).strip())
        except ValueError:
            turns = 0
        if turns < 1:
            return await interaction.response.send_message(
                '❌ 連数は 1 以上の整数で指定してください。', ephemeral=True)
        await start_vip_exist(interaction, self.email.component.value,
                              self.password.component.value, turns, self.paid)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await report_error(interaction, error, 'VipExistModal')


async def vip_exist_login(interaction: discord.Interaction, raw_email: str, password: str):
    """お客様の垢にログインして、まわし放題ガシャを引けるか確かめる（引かない）

    戻り値: (Client, userId, プレイヤー名, (ガチャID, 名前, 上限連数))。引けなければ VipUnavailable
    """
    email, hint = split_email_save(raw_email)

    async def _send_prompt(view, accs):
        return await interaction.followup.send(
            f'🗂 このアカウントには **{len(accs)}件** のセーブデータがあります。\n'
            'まわし放題ガシャを引くセーブデータを選択してください'
            f'（{SAVE_SELECT_TIMEOUT // 60}分でタイムアウト）',
            view=view, ephemeral=True, wait=True)

    # プロキシを1枠借りる（proxy.txt）。ガシャを引き終わるまでの間だけ占有
    # （枠は Client に結び付けて返す。start_vip_exist / run_paid_vip_exist で返却）
    lease = await lease_proxy()
    try:
        c, save_uid, accs = await prepare_login(email, password, interaction.user.id,
                                                _send_prompt, save_hint=hint,
                                                proxy=lease.url if lease is not None else None)
    except BaseException:
        if lease is not None:
            lease.release()
        raise
    if lease is not None:
        proxy_pool.bind(c, lease)
    save_uid = str(save_uid or accs[0].get('userId'))
    pname = next((str(a.get('playerName') or save_uid) for a in accs
                  if str(a.get('userId')) == save_uid), save_uid)
    # bot が周回中のセーブにログインすると、その周回が rc=32 で止まる
    busy = next((j for lst in JOBS.values() for j in lst
                 if not j.cancel.is_set() and not j.is_paused and str(j.save_uid or '') == save_uid), None)
    if busy is not None:
        raise RuntimeError(f'このセーブ（{pname}）は今 #{busy.jid} で周回中です。'
                           '先に周回を一時停止か中止してからお申し込みください')
    await asyncio.to_thread(c.login, save_uid)
    await asyncio.sleep(random.uniform(3.0, 6.0))        # ホーム → ガチャ画面
    vip = await asyncio.to_thread(c.vip_gacha_check)
    return c, save_uid, pname, vip


async def deliver_vip_exist(interaction: discord.Interaction, c: Client, save_uid: str, pname: str,
                            turns: int, amount: int = 0, order: str | None = None):
    """ログイン済みの垢で引いて、結果をお客様に届ける（失敗した有料はオーナーにも知らせる）"""
    await asyncio.sleep(random.uniform(4.0, 8.0))
    ok, text, tally, _ = await vip_pull(c, turns, f'{pname}({save_uid})')
    e = discord.Embed(title=('✅ 既存の垢に回し放題 完了' if ok else '⚠ 既存の垢に回し放題 失敗'),
                      color=COLOR_OK if ok else COLOR_WARN, timestamp=datetime.now())
    e.add_field(name='セーブデータ', value=f'**{pname}**（userId `{save_uid}`）', inline=False)
    e.add_field(name='🎰 まわし放題ガシャ', value=(text if ok else f'引けませんでした\n`{text[:200]}`')[:1024],
                inline=False)
    if order is not None:
        e.add_field(name='🧾 お支払い', value=f'¥{amount:,} ／ 取引番号 `{order}`', inline=False)
        if not ok:
            e.add_field(name='⚠ ご連絡', value='お支払いは確認済みです。この取引番号を添えて管理者にご連絡ください。',
                        inline=False)
    for send in (lambda: interaction.followup.send(embed=e, ephemeral=True),
                 lambda: interaction.user.send(embed=e)):
        try:
            await send()
        except discord.HTTPException:
            pass
    log_line(f'既存垢の回し放題 user={interaction.user.id} save={save_uid} {"OK" if ok else "失敗"}'
             + (f' 有料¥{amount} 取引番号={order}' if order else '') + ('' if ok else f' {text[:150]}'))
    if ok:
        try:
            await post_result(order is not None, interaction.user, f'既存の垢に回し放題（{turns}連）',
                              amount, tally)
        except Exception:
            print(f'[実績] 投稿でエラー\n{traceback.format_exc()}')
    elif order is not None:
        await notify_owners(f'⚠ 有料「既存の垢に回し放題」が失敗: {interaction.user} ({interaction.user.id})\n'
                            f'セーブ {pname}（{save_uid}） ／ 取引番号 `{order}` ¥{amount:,}\n{text[:300]}')


async def start_vip_exist(interaction: discord.Interaction, raw_email: str, password: str,
                          turns: int, paid: bool):
    """既存の垢に回し放題の受付。ログインして引けるのを確かめてから、無料版はそのまま引き、
    有料版は料金と支払いボタンを出す（引けない垢では支払いを求めない）"""
    uid = interaction.user.id
    why = vip_exist_blocker(uid, paid)
    if why:
        return await interaction.response.send_message(why, ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    VIP_EXIST_RUNNING.add(uid)
    try:
        try:
            c, save_uid, pname, vip = await vip_exist_login(interaction, raw_email, password)
        except VipUnavailable as e:
            return await interaction.followup.send(f'❌ {e}', ephemeral=True)
        except Exception as e:
            return await interaction.followup.send(f'❌ ログインできませんでした: `{str(e)[:250]}`',
                                                   ephemeral=True)
        finally:
            password = None     # 参照を残さない
        turns = min(turns, vip[2])
        if not paid:
            await interaction.followup.send(
                f'🎰 **{pname}** で {vip[1]} を **{turns}連** 引いています…（30秒ほど）\n'
                '-# 終わるまでゲームアプリは開かないでください', ephemeral=True)
            try:
                return await deliver_vip_exist(interaction, c, save_uid, pname, turns)
            finally:
                proxy_pool.release_client(c)
    finally:
        VIP_EXIST_RUNNING.discard(uid)      # 有料版は支払いを待つあいだ枠を空ける（払った後に取り直す）
    order = {'kind': 'exist', 'user_id': interaction.user.id, 'turns': turns,
             'amount': VIP_EXIST_PRICE, 'unit': VIP_EXIST_PRICE, 'udkey': c.udkey,
             'save_uid': save_uid, 'pname': pname, 'gacha': vip[1]}
    ps, e = vip_order_session(pending_put(order))
    proxy_pool.release_client(c)   # 支払い待ちのあいだはプロキシの枠を空ける
    await interaction.followup.send(embed=e, view=PayView(ps), ephemeral=True)


async def run_paid_vip_exist(inter: discord.Interaction, order: dict, order_id: str):
    """支払いを確かめた「既存の垢に回し放題」を引く。支払いのあいだにアプリを開かれていても、
    再起動のあとの途中復帰でも通るよう、注文に残した udkey でログインし直してから引く"""
    user, amount = inter.user, int(order['amount'])
    VIP_EXIST_RUNNING.add(user.id)
    lease = await lease_proxy()
    try:
        c = Client(order['udkey'], proxy=lease.url if lease is not None else None)
        await asyncio.to_thread(c.login, order['save_uid'])
        await deliver_vip_exist(inter, c, order['save_uid'], order['pname'], order['turns'],
                                amount=amount, order=order_id)
    except Exception as e:
        log_line(f'有料 既存垢の回し放題でエラー user={user.id} 取引番号={order_id}: {str(e)[:200]}')
        await inter.followup.send(
            f'⚠ ログインし直せませんでした: `{str(e)[:200]}`\nお支払いは確認済みです。'
            f'取引番号 `{order_id}` を添えて管理者にご連絡ください。', ephemeral=True)
        await notify_owners(f'⚠ 有料「既存の垢に回し放題」でエラー: {user} ({user.id}) '
                            f'取引番号 `{order_id}` ¥{amount:,}\n{str(e)[:300]}')
    finally:
        if lease is not None:
            lease.release()
        VIP_EXIST_RUNNING.discard(user.id)

class HitodamaBuyModal(discord.ui.Modal, title='ひとだま 購入テスト'):
    """ひとだま自動購入の中身（buyHitodama.nhn）を単体で試すモーダル"""

    target = discord.ui.Label(
        text='ひとだまの目標個数（合計）',
        description=('合計がこの個数に達するまでYマネーで買い足します。'
                     'パックは 5個=1,000 / 20個=3,000 / 50個=6,000 Yマネー'),
        component=discord.ui.TextInput(max_length=5),
    )

    def __init__(self):
        super().__init__()
        # 既定値はクラス定義時ではなく開くたびに読む（/管理パネルで変わるため）
        self.target.component.default = str(HITODAMA_BUY)
        self.target.component.placeholder = str(HITODAMA_BUY)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            target = int(self.target.component.value.strip().replace(',', ''))
            if not (1 <= target <= 99999):
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                '❌ 目標個数は 1〜99999 の整数で入力してください。', ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)

        if interaction.user.id not in SESSIONS:
            return await interaction.followup.send(
                '⚠ ログイン情報がありません。先に通常パネルの **📋 ステージ一覧** などで'
                'ログインしてください。', ephemeral=True)
        c = await acquire_client(interaction.user.id)
        if c is None:
            return await interaction.followup.send(
                '⚠ ログイン情報が期限切れでした。通常パネルから入力し直してください。',
                ephemeral=True)

        try:
            before = await asyncio.to_thread(c.points)
            before_total, before_ym = hitodama_total(before), before.get('ymoney')
            if before_total is not None and before_total >= target:
                return await interaction.followup.send(
                    f'ℹ️ ひとだまは既に目標を満たしています'
                    f'（現在 {before_total:,}個 / 目標 {target:,}個）。購入しませんでした。',
                    ephemeral=True)

            try:
                added = await asyncio.to_thread(c.buy_hitodama, target)
            except Exception as e:
                return await interaction.followup.send(
                    f'❌ 購入に失敗しました: `{str(e)[:250]}`' + chr(10) +
                    '（Yマネー不足のときは、周回でYマネーを稼いでから再試行してください）',
                    ephemeral=True)

            # buyHitodama応答でセーブは差し替わっているが、検証なのでサーバー側の
            # 最新状態をもう一度取り直して増減を突き合わせる
            try:
                await asyncio.to_thread(c.refresh)
            except Exception:
                pass
            after = await asyncio.to_thread(c.points)
            after_total, after_ym = hitodama_total(after), after.get('ymoney')

            e = discord.Embed(title='🛒 ひとだま購入テスト 結果', color=COLOR_OK,
                              timestamp=datetime.now())
            e.add_field(name='買えた個数', value=f'**+{added:,}個**', inline=True)
            if before_total is not None and after_total is not None:
                e.add_field(name='🔥 ひとだま合計',
                            value=f'{before_total:,} → **{after_total:,}**', inline=True)
            if before_ym is not None and after_ym is not None:
                e.add_field(name='💰 Yマネー',
                            value=f'{before_ym:,} → **{after_ym:,}**'
                                  f'（{before_ym - after_ym:,}消費）', inline=True)
            e.set_footer(text=f'目標 {target:,}個 / 周回中の自動購入は {HITODAMA_LOW}個以下で発動')
            remember_session(interaction.user.id, c)
            await interaction.followup.send(embed=e, ephemeral=True)
        finally:
            proxy_pool.release_client(c)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await report_error(interaction, error, 'HitodamaBuyModal')


# ============================================================================
# コインガチャ（手動）: コインを選ぶ → 枚数 → 引く
# ============================================================================

COIN_GACHA_MAX = 100        # 1回の操作で引ける上限


def gacha_result_embed(name: str, coin_name: str, want: int, results: list,
                       c: Client, left: int) -> discord.Embed:
    """引いた結果。妖怪ごとにランク付きで数える"""
    ids = [y for got in results for y in got]
    cnt = collections.Counter(ids)

    def rank(y):
        try:
            return c.youkai_rank(y) or 0
        except Exception:
            return 0
    lines = [f'`{RANK_NAMES.get(rank(y), "?"):>4}` {youkai_label(y)}' + (f' ×{n}' if n > 1 else '')
             for y, n in sorted(cnt.items(), key=lambda t: (-rank(t[0]), -t[1]))]
    e = discord.Embed(
        title=f'🎰 {coin_name} で {len(results)}回 引きました',
        description='\n'.join(lines[:25]) + (f'\n…ほか{len(lines) - 25}種' if len(lines) > 25 else ''),
        color=COLOR_OK if len(results) == want else COLOR_WARN, timestamp=datetime.now())
    if len(results) < want:
        e.add_field(name='⚠ 途中で止まりました',
                    value=f'{want}回のうち{len(results)}回で失敗しました（ログを確認してください）',
                    inline=False)
    e.set_footer(text=f'プレイヤー: {name} ／ {coin_name} 残り {left}枚')
    return e


class CoinCountModal(discord.ui.Modal, title='コインガチャ'):
    count = discord.ui.Label(text='引く回数', component=discord.ui.TextInput(max_length=4))

    def __init__(self, owner_id: int, c: Client, coin: tuple):
        super().__init__()
        self.owner_id, self.c, self.coin = owner_id, c, coin
        cid, gid, have, cname = coin
        self.count.description = f'{cname}：{have}枚持っています（1回に1枚。最大{COIN_GACHA_MAX}回）'
        self.count.component.default = str(min(have, COIN_GACHA_MAX))

    async def on_submit(self, interaction: discord.Interaction):
        cid, gid, have, cname = self.coin
        try:
            n = int(self.count.component.value.strip())
            if not 1 <= n <= min(have, COIN_GACHA_MAX):
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                f'❌ 引く回数は 1〜{min(have, COIN_GACHA_MAX)} で入力してください。', ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        log_line(f'コインガチャ（手動） user={interaction.user.id} {cname}({cid}) ×{n} gachaId={gid}')
        try:
            results = await asyncio.to_thread(self.c.pull_coin, cid, gid, n)
        except Exception as e:
            return await interaction.followup.send(f'❌ ガチャに失敗しました: `{str(e)[:250]}`',
                                                   ephemeral=True)
        name = self.c.raw_info().get('playerName') or self.c.userId
        remember_session(interaction.user.id, self.c, name)
        await interaction.followup.send(
            embed=gacha_result_embed(name, cname, n, results, self.c,
                                     self.c.items().get(cid, 0)), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await report_error(interaction, error, 'CoinCountModal')


class CoinGachaView(discord.ui.View):
    """持っているコインのプルダウン。選ぶと回数を聞く"""

    def __init__(self, owner_id: int, c: Client, coins: list):
        super().__init__(timeout=300)
        self.owner_id, self.c = owner_id, c
        self.coins = {f'{cid}:{gid}': (cid, gid, n, name) for cid, gid, n, name in coins}
        select = discord.ui.Select(
            placeholder='引くコインを選んでください',
            options=[discord.SelectOption(label=f'{name}（{n}枚）'[:100], value=key,
                                          description=f'ガチャID {gid}')
                     for key, (cid, gid, n, name) in list(self.coins.items())[:25]])
        select.callback = self._picked
        self.add_item(select)

    async def on_timeout(self):
        # このViewが握っているプロキシの枠（proxy.txt）を返す
        proxy_pool.release_client(self.c)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def _picked(self, interaction: discord.Interaction):
        coin = self.coins.get(interaction.data['values'][0])
        if coin is None:
            return await interaction.response.send_message('❌ 不明なコインです。', ephemeral=True)
        await interaction.response.send_modal(CoinCountModal(self.owner_id, self.c, coin))


async def open_coin_gacha(interaction: discord.Interaction):
    """ログイン中の垢のコインを並べる（ログインは 📋 ステージ一覧 などで済ませておく）"""
    if interaction.user.id not in SESSIONS:
        return await interaction.response.send_message(
            'ℹ️ まだログイン情報がありません。先に通常パネルの **📋 ステージ一覧** などで'
            'ガチャを引く垢にログインしてください。', ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    c = await acquire_client(interaction.user.id)
    if c is None:
        return await interaction.followup.send(
            '⚠ ログイン情報が期限切れでした。通常パネルから入力し直してください。', ephemeral=True)
    try:
        coins = await asyncio.to_thread(c.coin_list)
    except Exception as e:
        proxy_pool.release_client(c)
        return await interaction.followup.send(f'❌ ガチャ画面を開けませんでした: `{str(e)[:200]}`',
                                               ephemeral=True)
    name = c.raw_info().get('playerName') or c.userId
    if not coins:
        proxy_pool.release_client(c)
        return await interaction.followup.send(
            f'ℹ️ **{name}** はガチャに使えるコインを持っていません。', ephemeral=True)
    # Client（とプロキシの枠）はこのViewが使い終わるまで握る（on_timeoutで返す）
    try:
        await interaction.followup.send(
            f'🎰 **{name}** のコイン（{len(coins)}種・計{sum(n for _, _, n, _ in coins)}枚）。\n'
            '引くコインを選ぶと回数を聞きます。周回中の垢で使うと、周回側が一度ログインし直します。',
            view=CoinGachaView(interaction.user.id, c, coins), ephemeral=True)
    except discord.HTTPException:
        proxy_pool.release_client(c)   # Viewを出せなかったので枠をここで返す


# ============================================================================
# 周回開始: ログイン → セーブ選択 → 周回設定 の2段階フロー
# ============================================================================

class LoginModal(discord.ui.Modal, title='ぷにぷに ログイン'):
    """周回開始のSTEP1: アカウントにログインしてセーブデータを確定させる"""

    email = discord.ui.Label(
        text='メールアドレス',
        description='ぷにぷにに連携しているメールアドレス（セーブが複数なら次の画面で選択します）',
        component=discord.ui.TextInput(
            placeholder='example@example.com（#セーブ名 で事前指定も可）', max_length=120),
    )
    password = discord.ui.Label(
        text='パスワード',
        description='保存・ログ出力はされません',
        component=discord.ui.TextInput(
            placeholder='ログイン用パスワード', max_length=100),
    )

    def __init__(self, preset_name: str | None = None):
        super().__init__()
        self.preset_name = preset_name

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        email, save_hint = split_email_save(self.email.component.value)
        password = self.password.component.value

        async def _send_prompt(view, accs):
            return await interaction.followup.send(
                f'🗂 このアカウントには **{len(accs)}件** のセーブデータがあります。\n'
                'ログインするセーブデータを選択してください'
                f'（{SAVE_SELECT_TIMEOUT // 60}分でタイムアウト）',
                view=view, ephemeral=True, wait=True)

        lease = await lease_proxy()
        try:
            # ここではセーブ一覧の取得まで。クライアント自体は使い捨てで、
            # 周回本体（run_job）で改めてログインし直す
            c, save_uid, accs = await prepare_login(
                email, password, interaction.user.id, _send_prompt,
                save_hint=save_hint,
                proxy=lease.url if lease is not None else None)
        except Exception as e:
            return await interaction.followup.send(
                f'❌ ログイン失敗: `{str(e)[:250]}`', ephemeral=True)
        finally:
            if lease is not None:
                lease.release()

        # 選んだセーブ（1件のみなら先頭）の表示名
        uid = str(save_uid) if save_uid else str(accs[0].get('userId'))
        name = next((str(a.get('playerName') or f'userId: {a.get("userId")}')
                     for a in accs if str(a.get('userId')) == uid), uid)

        ctx = FarmLoginCtx()
        ctx.email, ctx.password = email, password
        ctx.accs, ctx.save_uid, ctx.save_name = accs, save_uid, name
        ctx.preset_name = self.preset_name
        ctx.at = time.time()
        PENDING_FARM[interaction.user.id] = ctx
        email = password = None   # 参照を残さない（ctxに移したぶんは使い捨て）

        await interaction.followup.send(
            f'✅ ログインを確認しました（セーブデータ **{len(accs)}件**）\n'
            f'> 周回セーブ: **{name}**\n'
            f'⚙ 下のボタンで周回の設定を開いてください'
            f'（{PENDING_TTL // 60}分で期限切れ）',
            view=SaveReadyView(interaction.user.id), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await report_error(interaction, error, 'LoginModal')


class SaveReadyView(discord.ui.View):
    """ログイン確認後に周回設定モーダルを開くボタン"""

    def __init__(self, owner_id: int):
        super().__init__(timeout=PENDING_TTL)
        self.owner_id = owner_id
        button = discord.ui.Button(label='周回設定へ進む',
                                   style=discord.ButtonStyle.primary, emoji='⚙')
        button.callback = self._open
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                'このボタンは別の人のログイン用です。', ephemeral=True)
            return False
        return True

    async def _open(self, interaction: discord.Interaction):
        ctx = PENDING_FARM.get(interaction.user.id)
        if ctx is None or time.time() - ctx.at > PENDING_TTL:
            PENDING_FARM.pop(interaction.user.id, None)
            return await interaction.response.send_message(
                '⏰ ログインの有効期限が切れました。'
                'パネルの **🎮 周回開始** からやり直してください。', ephemeral=True)
        await interaction.response.send_modal(FarmModal(ctx.preset_name, ctx))


class FarmStartView(discord.ui.View):
    """「周回開始」の周回方法を選ぶ画面（ステージID直指定 / プリセット）

    選ぶと ログイン（メール/パス）→ セーブ選択 → 周回設定 の順に進む。
    """

    def __init__(self):
        super().__init__(timeout=300)
        manual = discord.ui.Button(label='ステージIDで指定',
                                   style=discord.ButtonStyle.primary, emoji='🔢')
        manual.callback = self._manual
        self.add_item(manual)
        refresh_event_preset()
        for name, p in PRESETS.items():
            b = discord.ui.Button(label=f'{name}（{len(p["ids"])}ステージ）'[:80],
                                  style=discord.ButtonStyle.success, emoji='📜')
            b.callback = self._make_preset_callback(name)
            self.add_item(b)

    async def _manual(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LoginModal())

    def _make_preset_callback(self, name: str):
        async def callback(interaction: discord.Interaction):
            await interaction.response.send_modal(LoginModal(name))
        return callback


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='周回開始', style=discord.ButtonStyle.primary,
                       emoji='🎮', custom_id='ywp:start')
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(jobs_of(interaction.user.id)) >= MAX_JOBS_PER_USER:
            return await interaction.response.send_message(
                f'❌ 同時実行できるのは {MAX_JOBS_PER_USER}ジョブまでです。'
                'DMや「状況確認」の中止ボタンで止めてから追加してください。', ephemeral=True)
        # ステージID直指定かプリセットかを選んでから
        # ログイン → セーブ選択 → 周回設定の順に進む
        await interaction.response.send_message(
            '🎮 周回方法を選択してください'
            '（次に ログイン → セーブデータ選択 → 周回設定 と進みます。'
            'プリセットは登録済みのステージ群を順番に周回します）',
            view=FarmStartView(), ephemeral=True)

    @discord.ui.button(label='ステージ一覧', style=discord.ButtonStyle.success,
                       emoji='📋', custom_id='ywp:stages')
    async def stage_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 直近にログイン済みならパス入力を省いてそのまま表示
        if interaction.user.id in SESSIONS:
            await interaction.response.defer(ephemeral=True, thinking=True)
            c = await acquire_client(interaction.user.id)
            if c is not None:
                try:
                    return await send_stage_list(interaction, c)
                finally:
                    proxy_pool.release_client(c)
            return await interaction.followup.send(
                '⚠ 保持していたログイン情報が期限切れでした。'
                'もう一度「ステージ一覧」を押して入力してください。', ephemeral=True)
        await interaction.response.send_modal(StageListModal())

    @discord.ui.button(label='状況確認', style=discord.ButtonStyle.secondary,
                       emoji='📊', custom_id='ywp:status')
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button):
        jobs = jobs_of(interaction.user.id)
        if not jobs:
            return await interaction.response.send_message(
                '📭 実行中の周回はありません。', ephemeral=True)
        # 開いただけでは取り直さない（毎回叩くとレート制限に当たる）。
        # 最新にしたいときは出てくる「🔄 更新」を押してもらう
        if len(jobs) == 1:
            embed, view = progress_embed(jobs[0]), CancelView(jobs[0])
        else:
            embed, view = jobs_list_embed(jobs), JobsCancelView(jobs)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label='アカウント作成', style=discord.ButtonStyle.secondary,
                       emoji='🆕', custom_id='ywp:account')
    async def create_account(self, interaction: discord.Interaction,
                             button: discord.ui.Button):
        if interaction.user.id in ACCOUNT_RUNNING:
            return await interaction.response.send_message(
                '⏳ アカウント作成が実行中です。完了までお待ちください。', ephemeral=True)
        await interaction.response.send_modal(AccountCountModal())


class SettingEditModal(discord.ui.Modal, title='設定の変更'):
    """設定1項目を書き換えるモーダル（値は settings.json に保存される）"""

    value = discord.ui.Label(
        text='新しい値',
        component=discord.ui.TextInput(max_length=20),
    )

    def __init__(self, key: str):
        d = SETTING_DEFS[key]
        super().__init__(title=f'設定: {d["label"]}'[:45])
        self.key = key
        cur = setting_value(key)
        rng = []
        fmt = d.get('fmt') or (lambda x: f'{x:g}')
        if d.get('min') is not None:
            rng.append(f'{fmt(d["min"])} 以上')
        if d.get('max') is not None:
            rng.append(f'{fmt(d["max"])} 以下')
        self.value.description = (f'{d["help"]}'
                                  + (f'（{" / ".join(rng)}）' if rng else ''))[:100]
        self.value.component.default = (f'{cur:g}' if isinstance(cur, float)
                                         else RANK_NAMES.get(cur, str(cur)) if d.get('fmt')
                                         else str(cur))
        self.value.component.placeholder = f'既定値: {setting_show(key, SETTING_DEFAULTS[key])}'

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new = coerce_setting(self.key, self.value.component.value)
        except ValueError as e:
            return await interaction.response.send_message(
                f'❌ {SETTING_DEFS[self.key]["label"]}: {e}', ephemeral=True)
        old = setting_value(self.key)
        apply_setting(self.key, new)
        saved = save_settings()
        note = '' if saved else '\n⚠ settings.json に保存できませんでした（再起動で戻ります）'
        await interaction.response.send_message(
            f'✅ **{SETTING_DEFS[self.key]["label"]}** を `{setting_show(self.key, old)}` → '
            f'`{setting_show(self.key, new)}` に変更しました。'
            f'{note}', ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await report_error(interaction, error, 'SettingEditModal')


class SettingSelectView(discord.ui.View):
    """変更したい設定を選ぶプルダウン（押した本人にだけ見える一時View）"""

    def __init__(self):
        super().__init__(timeout=180)
        select = discord.ui.Select(
            placeholder='変更する設定を選んでください',
            options=[
                discord.SelectOption(
                    label=d['label'][:100], value=key,
                    description=(f'現在: {setting_show(key)}'
                                 f'（既定: {setting_show(key, SETTING_DEFAULTS[key])}）')[:100])
                for key, d in SETTING_DEFS.items()
            ][:25])
        select.callback = self._picked
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction):
        key = interaction.data['values'][0]
        if key not in SETTING_DEFS:
            return await interaction.response.send_message(
                '❌ 不明な設定です。', ephemeral=True)
        await interaction.response.send_modal(SettingEditModal(key))


def settings_embed() -> discord.Embed:
    e = discord.Embed(
        title='⚙ 管理パネル — 現在の設定',
        description='⚙ **設定変更** で1項目ずつ書き換えられます。変更は即座に反映され、'
                    '`settings.json` に保存されて再起動後も残ります。',
        color=COLOR_INFO,
        timestamp=datetime.now(),
    )
    e.add_field(name='🆕 アカウント作成',
                value=account_pause_text() + (f'（実行中 {len(ACCOUNT_RUNNING)}人）'
                                              if ACCOUNT_RUNNING else ''),
                inline=False)
    mw = maint_window()
    upcoming = [it for it in _maint_items() if it.get('start', '') > datetime.now().strftime('%Y-%m-%d %H:%M')
                and not it.get('end')]
    e.add_field(name='🛠 メンテ停止',
                value=(f'**停止中**（{maint_text(mw)}）' if mw else
                       ('予定あり: ' + ', '.join(f'{it["start"]}〜（解除するまで）' for it in upcoming)
                        if upcoming else 'なし')),
                inline=False)
    for key, d in SETTING_DEFS.items():
        cur, dflt = setting_value(key), SETTING_DEFAULTS[key]
        mark = '' if cur == dflt else ' ✏'
        e.add_field(name=f'{d["label"]}{mark}',
                    value=f'**{setting_show(key)}**'
                    + ('' if cur == dflt else f'（既定 {setting_show(key, dflt)}）'),
                    inline=True)
    e.set_footer(text='✏ = 既定値から変更済み')
    return e


class AdminPanelView(discord.ui.View):
    """管理パネル（ボタンは永続View）。サーバー管理権限が要る"""

    def __init__(self):
        super().__init__(timeout=None)

    @staticmethod
    async def _deny(interaction: discord.Interaction) -> bool:
        """オーナーでもサーバー管理者でもなければ断る。True を返したら処理を止める"""
        if is_admin(interaction.user):
            return False
        await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
        return True

    @discord.ui.button(label='設定変更', style=discord.ButtonStyle.primary,
                       emoji='⚙', custom_id='ywp:admin:edit')
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._deny(interaction):
            return
        await interaction.response.send_message(
            '⚙ 変更する設定を選んでください。', view=SettingSelectView(), ephemeral=True)

    @discord.ui.button(label='現在の設定', style=discord.ButtonStyle.secondary,
                       emoji='📋', custom_id='ywp:admin:show')
    async def show(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=settings_embed(), ephemeral=True)

    @discord.ui.button(label='全部を既定値に戻す', style=discord.ButtonStyle.danger,
                       emoji='♻', custom_id='ywp:admin:reset')
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._deny(interaction):
            return
        changed = [k for k in SETTING_DEFS if setting_value(k) != SETTING_DEFAULTS[k]]
        if not changed:
            return await interaction.response.send_message(
                'ℹ️ すべて既定値のままです。', ephemeral=True)
        for k in changed:
            apply_setting(k, SETTING_DEFAULTS[k])
        save_settings()
        await interaction.response.send_message(
            f'♻ {len(changed)}項目を既定値に戻しました:\n'
            + '\n'.join(f'・{SETTING_DEFS[k]["label"]} → `{setting_show(k, SETTING_DEFAULTS[k])}`'
                        for k in changed),
            ephemeral=True)

    @discord.ui.button(label='垢作成を停止', style=discord.ButtonStyle.danger,
                       emoji='⏸', custom_id='ywp:admin:acc_pause', row=1)
    async def acc_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._deny(interaction):
            return
        if ACCOUNT_PAUSE['paused']:
            return await interaction.response.send_message(
                f'ℹ️ アカウント作成はすでに停止中です（{ACCOUNT_PAUSE["at"]}〜）。',
                ephemeral=True)
        saved = set_account_pause(True, interaction.user)
        running = len(ACCOUNT_RUNNING)
        await interaction.response.send_message(
            '⏸ アカウント作成を**停止**しました。新しい作成は受け付けません。\n'
            + (f'実行中の作成（{running}人ぶん）は、作り途中の1件を終えたところで終了し、'
               'それまでに作れたぶんをいつもどおりDMで渡します。' if running
               else '実行中の作成はありません。')
            + ('' if saved else '\n⚠ 停止状態をファイルに保存できませんでした'
               '（Botを再起動すると受付中に戻ります）。'),
            ephemeral=True)

    @discord.ui.button(label='垢作成を再開', style=discord.ButtonStyle.success,
                       emoji='▶', custom_id='ywp:admin:acc_resume', row=1)
    async def acc_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._deny(interaction):
            return
        if not ACCOUNT_PAUSE['paused']:
            return await interaction.response.send_message(
                'ℹ️ アカウント作成は受付中です（停止していません）。', ephemeral=True)
        saved = set_account_pause(False, interaction.user)
        await interaction.response.send_message(
            '▶ アカウント作成を**再開**しました。新しい作成を受け付けます。\n'
            '停止で打ち切られた作成は自動では続かないので、残りはもう一度実行してください。'
            + ('' if saved else '\n⚠ 状態をファイルに保存できませんでした'
               '（Botを再起動すると停止中に戻ります）。'),
            ephemeral=True)

    @discord.ui.button(label='メンテ停止（今すぐ）', style=discord.ButtonStyle.danger,
                       emoji='🛠', custom_id='ywp:admin:maint_start', row=2)
    async def maint_start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._deny(interaction):
            return
        w = maint_window(0)
        if w:
            return await interaction.response.send_message(
                f'ℹ️ すでにメンテ停止中です（{maint_text(w)}）。', ephemeral=True)
        saved = maint_start_now(interaction.user)
        await interaction.response.send_message(
            '🛠 **メンテ停止**にしました。周回は戦っている最中の1戦を終えたところで止まり、'
            '垢作成も受け付けません（作成中のものは作りかけの1件で止めてDMで渡します）。\n'
            '**自動では再開しません。** 再開するときは「▶ メンテ停止を解除」を押してください。'
            + ('' if saved else '\n⚠ maintenance.json に保存できませんでした'), ephemeral=True)

    @discord.ui.button(label='メンテ停止を解除', style=discord.ButtonStyle.success,
                       emoji='▶', custom_id='ywp:admin:maint_release', row=2)
    async def maint_release_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._deny(interaction):
            return
        upcoming = [it for it in _maint_items() if not it.get('end')
                    or it['end'] > datetime.now().strftime('%Y-%m-%d %H:%M')]
        if not upcoming:
            return await interaction.response.send_message(
                'ℹ️ メンテ停止はしていません（予定もありません）。', ephemeral=True)
        saved = maint_release(interaction.user)
        await interaction.response.send_message(
            '▶ **メンテ停止を解除**しました。止まっていた周回は30秒以内に再開し、'
            '垢作成も受け付けます。（これから始まる予定だったメンテ停止も取り消しました）'
            + ('' if saved else '\n⚠ maintenance.json に保存できませんでした'), ephemeral=True)


def admin_panel_embed() -> discord.Embed:
    e = discord.Embed(
        title='⚙ ぷにぷに Bot 管理パネル',
        description=('⚙ **設定変更** … プルダウンから項目を選んで値を書き換え\n'
                     '📋 **現在の設定** … 全項目の現在値を表示\n'
                     '♻ **全部を既定値に戻す** … コードの初期値へ戻す\n'
                     '⏸ **垢作成を停止** … 新しいアカウント作成を止める。実行中のものは'
                     '作り途中の1件で打ち切り、作れたぶんをDMで渡す\n'
                     '▶ **垢作成を再開** … アカウント作成の受付を再開\n'
                     '🛠 **メンテ停止（今すぐ）** … 周回も垢作成も止める。**自動では再開しない**\n'
                     '▶ **メンテ停止を解除** … 止めていた周回を再開し、垢作成も受け付ける\n\n'
                     '変更は**すぐに反映**され、`settings.json` に保存されるので'
                     '再起動後も残ります。'),
        color=COLOR_INFO,
    )
    e.add_field(
        name='📣 完了実績の投稿先（/実績チャンネル設定）',
        value=results_text(),
        inline=False,
    )
    e.add_field(
        name='🔥 ひとだま自動購入',
        value=(f'しきい値 **{HITODAMA_LOW}個以下** → **{HITODAMA_BUY}個**まで補充\n'
               'しきい値を `0` にすると「残り0個になるまで買わない」動作になります'),
        inline=False,
    )
    e.add_field(
        name='⚠ 注意',
        value=('・操作にはサーバー管理権限（またはBotオーナー）が必要です\n'
               '・稼働中のジョブにも次の周回から反映されます\n'
               '・全体の同時稼働ジョブ数だけはコード側の固定値です'
               f'（現在 {MAX_CONCURRENT_JOBS}）'),
        inline=False,
    )
    return e


class AgedAccountView(discord.ui.View):
    """久垢作成パネル（ボタンは永続View）"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='久垢作成', style=discord.ButtonStyle.primary,
                       emoji='🕰', custom_id='ywp:aged')
    async def create(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        if interaction.user.id in ACCOUNT_RUNNING:
            return await interaction.response.send_message(
                '⏳ アカウント作成が実行中です。完了までお待ちください。', ephemeral=True)
        await interaction.response.send_modal(AccountCountModal(aged=True))


class VipAccountView(discord.ui.View):
    """回し放題垢作成パネル（ボタンは永続View）"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='初期垢を作って回し放題', style=discord.ButtonStyle.primary,
                       emoji='🆕', custom_id='ywp:vip')
    async def create(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        if interaction.user.id in ACCOUNT_RUNNING:
            return await interaction.response.send_message(
                '⏳ アカウント作成が実行中です。完了までお待ちください。', ephemeral=True)
        await interaction.response.send_modal(VipAccountModal())

    @discord.ui.button(label='既存の垢に回し放題', style=discord.ButtonStyle.success,
                       emoji='🎰', custom_id='ywp:vip_exist')
    async def exist(self, interaction: discord.Interaction, button: discord.ui.Button):
        why = vip_exist_blocker(interaction.user.id, paid=False)
        if why:
            return await interaction.response.send_message(why, ephemeral=True)
        await interaction.response.send_modal(VipExistModal())


class PaidVipView(discord.ui.View):
    """回し放題垢 有料版パネル（ボタンは永続View）"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='初期垢を作って回し放題（購入）', style=discord.ButtonStyle.primary,
                       emoji='🆕', custom_id='ywp:vip_paid')
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        why = vip_sale_blocker(interaction.user.id)
        if why:
            return await interaction.response.send_message(why, ephemeral=True)
        await interaction.response.send_modal(VipAccountModal(paid=True))

    @discord.ui.button(label='既存の垢に回し放題（購入）', style=discord.ButtonStyle.success,
                       emoji='🎰', custom_id='ywp:vip_paid_exist')
    async def buy_exist(self, interaction: discord.Interaction, button: discord.ui.Button):
        why = vip_exist_blocker(interaction.user.id, paid=True)
        if why:
            return await interaction.response.send_message(why, ephemeral=True)
        await interaction.response.send_modal(VipExistModal(paid=True))

    @discord.ui.button(label='お支払い途中のご注文', style=discord.ButtonStyle.secondary,
                       emoji='🧾', custom_id='ywp:vip_paid_resume', row=1)
    async def resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_pending_order(interaction)


PAID_PANELS_FILE = os.path.join(BASE_DIR, 'data', 'vip_paid_panels.json')


def _paid_panels() -> list[dict]:
    try:
        with open(PAID_PANELS_FILE, encoding='utf-8') as f:
            data = json.load(f)
        return [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _paid_panels_save(panels: list[dict]):
    try:
        os.makedirs(os.path.dirname(PAID_PANELS_FILE), exist_ok=True)
        with open(PAID_PANELS_FILE, 'w', encoding='utf-8') as f:
            json.dump(panels, f, ensure_ascii=False, indent=1)
    except OSError as e:
        print(f'[vip] 有料版パネルの記録に失敗: {e}')


async def refresh_paid_panels():
    """設置済みの有料版パネルを今の料金・上限で描き直す（消されたパネルは記録から外す）"""
    keep = []
    for p in _paid_panels():
        try:
            ch = bot.get_channel(p['channel']) or await bot.fetch_channel(p['channel'])
            await ch.get_partial_message(p['message']).edit(embed=paid_vip_embed(), view=PaidVipView())
            keep.append(p)
        except discord.NotFound:
            continue
        except (discord.HTTPException, KeyError, AttributeError) as e:
            print(f'[vip] 有料版パネルを描き直せませんでした {p}: {str(e)[:120]}')
            keep.append(p)
    _paid_panels_save(keep)


class HitodamaTestView(discord.ui.View):
    """ひとだま自動購入の検証用パネル（ボタンは永続View）"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='所持数確認', style=discord.ButtonStyle.secondary,
                       emoji='🔥', custom_id='ywp:hitodama:status')
    async def wallet(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        if interaction.user.id not in SESSIONS:
            return await interaction.response.send_message(
                'ℹ️ まだログイン情報がありません。' + chr(10) +
                '先に通常パネルの **📋 ステージ一覧** などからログインしてください。',
                ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        c = await acquire_client(interaction.user.id)
        if c is None:
            return await interaction.followup.send(
                '⚠ ログイン情報が期限切れでした。通常パネルから入力し直してください。',
                ephemeral=True)

        try:
            try:
                pts = await asyncio.to_thread(c.points)
            except Exception as e:
                return await interaction.followup.send(
                    f'❌ 所持数の取得に失敗しました: `{str(e)[:200]}`', ephemeral=True)
            total = hitodama_total(pts)
            if total is None:
                return await interaction.followup.send(
                    '⚠ ひとだまの所持数を読めませんでした。'
                    '通常パネルからログインし直してください。', ephemeral=True)

            labels = {'hitodama': '🔥 ひとだま（本数）', 'freeHitodama': '🔥 無料ひとだま',
                      'ymoney': '💰 Yマネー', 'ypoint': '🔷 Yポイント'}
            e = discord.Embed(title='🔥 所持数の確認', color=COLOR_INFO,
                              timestamp=datetime.now())
            for key, label in labels.items():
                if key in pts:
                    e.add_field(name=label, value=f'**{pts[key]:,}**', inline=True)
            e.add_field(name='🔥 ひとだま合計', value=f'**{total:,}**', inline=True)
            if total <= HITODAMA_LOW:
                e.add_field(
                    name='⚠ 自動購入が発動する状態です',
                    value=(f'合計が{HITODAMA_LOW}個以下なので、周回でこの状態になると'
                           f'Yマネーで{HITODAMA_BUY}個まで自動購入します。' + chr(10) +
                           'このまま **🛒 購入テスト** を押すと挙動を確認できます。'),
                    inline=False)
            else:
                e.add_field(
                    name='ℹ 自動購入の発動条件',
                    value=f'合計が **{HITODAMA_LOW}個以下** になると、周回中に'
                          f'Yマネーで **{HITODAMA_BUY}個まで** 自動購入します。',
                    inline=False)
            try:
                name = c.raw_info().get('playerName')
            except (ValueError, AttributeError):
                name = None
            if name:
                e.set_footer(text=f'プレイヤー: {name}')
            await interaction.followup.send(embed=e, ephemeral=True)
        finally:
            proxy_pool.release_client(c)

    @discord.ui.button(label='購入テスト', style=discord.ButtonStyle.primary,
                       emoji='🛒', custom_id='ywp:hitodama:buy')
    async def buy(self, interaction: discord.Interaction,
                  button: discord.ui.Button):
        if interaction.user.id not in SESSIONS:
            return await interaction.response.send_message(
                'ℹ️ まだログイン情報がありません。' + chr(10) +
                '先に通常パネルの **📋 ステージ一覧** などからログインしてください。',
                ephemeral=True)
        await interaction.response.send_modal(HitodamaBuyModal())


def panel_embed() -> discord.Embed:
    e = discord.Embed(
        title='🎮 ぷにぷに 自動周回',
        description=(
            '🎮 **周回開始** … 周回方法（🔢ステージID / 📜プリセット）を選び、'
            'ログインして `セーブデータ` `周回数/目標` を設定して周回\n'
            '📋 **ステージ一覧** … クリア済みステージIDを分類別に表示\n'
            '📊 **状況確認** … 実行中の周回をまとめて表示\n'
            '　　　　　　　　 **⏸ 一時停止** / **▶ 再開** / **⏹ 中止** をジョブ単位で操作\n'
            '　　　　　　　　 所持数は **🔄 更新** を押したときだけ取り直します\n'
            '🆕 **アカウント作成** … 捨てメールでLEVEL5 ID＋周回用セーブデータを自動作成\n'
            '　　　　　　　　 `作成数` `パスワード固定` `アカウント名` を指定できます。'
            f'**{ACCOUNT_TXT_THRESHOLD}件以上**は `メアド:パスワード` の **.txt** をDMに添付\n'
            f'　　　　　　　　 ステージ`{AGED_STAGE_ID}`クリア済みで欲しいときは '
            '**🕰 久垢作成パネル**（`/久垢作成設置`）へ\n\n'
            'ステージIDが分からないときは先に **📋 ステージ一覧** を押してください。\n'
            '未解放を含めた全ステージが要るときは '
            '`/ステージ一覧 include_master:True`（.txtで届きます）。\n'
            '進捗と完了通知は **DM** に届きます。'
        ),
        color=COLOR_INFO,
    )
    e.add_field(
        name='🎯 周回数/目標の書き方',
        value=('`10` … 10周する\n'
               '`無限` … 中止するまで回し続ける\n'
               '`P12000` … Yポイントが12000になるまで\n'
               '`P+3000` … Yポイントが3000増えるまで\n'
               '`M+50000` … Yマネーが50000増えるまで\n'
               '※周回数の上限はありません。目標指定は達成まで回ります'),
        inline=False,
    )
    e.add_field(
        name='📜 ステージプリセット',
        value=('「🎮 周回開始」→プリセットボタンで選択（ステージIDの代わりに順番に周回）\n'
               + preset_help()
               + '\n周回数は「プリセット全ステージを何周するか」です'
                 '（`1`=各ステージ1回ずつ / `無限`=一覧を繰り返し回し続ける）'),
        inline=False,
    )
    e.add_field(
        name='⏱ 動作',
        value=(f'バトル時間 **{DEFAULT_BATTLE_TIME:g}〜'
               f'{DEFAULT_BATTLE_TIME * (1 + BATTLE_TIME_SPREAD):g}秒**（ランダム） / '
               f'連戦 **約10〜12秒/戦**\n'
               f'周回数の上限なし（`無限` 指定可） / 1人 **{MAX_JOBS_PER_USER}ジョブ**まで同時投下'
               f'（稼働は全体で{MAX_CONCURRENT_JOBS}ジョブまで）\n'
               f'所持数の確認は **{WALLET_REFRESH_EVERY}周ごと**（目標間際は毎周）\n'
               f'エラー時は**{ERROR_RETRY}回まで再試行** / tokenズレ（rc=32）は**再ログインで自動復帰**\n'
               f'ひとだまが**{HITODAMA_LOW}個以下**になるとYマネーで'
               f'**{HITODAMA_BUY}個まで自動補充**します（買えないときは強制停止）'),
        inline=False,
    )
    e.add_field(
        name='⚠ 注意',
        value=('・DMを受け取れる設定にしてください\n'
               '・入力したパスワードは保存・ログ出力されません\n'
               '・周回開始は ログイン → セーブデータ選択 → 周回設定 の順に進みます'
               '（セーブは名前の一覧から選択。`メール#セーブ名` での事前指定も可）\n'
               f'・一度ログインすると{SESSION_TTL // 3600}時間はパス入力を省略できます'
               '（`/ログアウト`で消去）\n'
               '・実行中は「状況確認」やDMから **⏸ 一時停止 / ▶ 再開 / ⏹ 中止** ができます\n'
               '　（一時停止は進行中のバトルを閉じてから止まり、停止中の時間は経過から除外されます）\n'
               '・動作設定は `/管理パネル` から変更できます（サーバー管理権限またはBotオーナー）\n'
               '・`無限` 指定はエラーやひとだま不足で自動停止しますが、'
               '長時間の放置はアカウント停止（BAN）のリスクが高まるのでご注意ください'),
        inline=False,
    )
    return e


def aged_account_embed() -> discord.Embed:
    e = discord.Embed(
        title='🕰 ぷにぷに 久垢作成',
        description=(
            '🕰 **久垢作成** を押して `作成数` `パスワード（固定・任意）` '
            '`アカウント名（任意）` を入力してください。\n\n'
            '**1件あたりの流れ**\n'
            '① 捨てメールで LEVEL5 ID を新規作成\n'
            '② セーブデータ（ゲーム内ユーザー）を作成して連携\n'
            f'③ ログインしてステージ `{AGED_STAGE_ID}` をクリア\n'
            '④ メアド/パスワードを **DM** で通知'
        ),
        color=COLOR_INFO,
    )
    e.add_field(
        name='📨 受け取り方',
        value=(f'・**{ACCOUNT_TXT_THRESHOLD}件未満** … 1件ずつ埋め込みでDM\n'
               f'・**{ACCOUNT_TXT_THRESHOLD}件以上** … `メアド:パスワード` を並べた '
               '**.txt** をDMに添付\n'
               '　（.txtの1行目に「〇月〇日〇時〇分から〇月〇日〇時〇分までに作られた垢」を記載）\n'
               '・どちらの場合も **作成日時** を必ず記載します'),
        inline=False,
    )
    e.add_field(
        name='✏ アカウント名',
        value=(f'・空欄 … 「ぷに＋4桁」をランダム生成\n'
               f'・入力 … そのままゲーム内の名前になります（{PLAYER_NAME_MAX}文字以内）\n'
               '・`{n}` を含めると連番に置換（例: `テスト{n}` → テスト1, テスト2 …）'),
        inline=False,
    )
    e.add_field(
        name='⚠ 注意',
        value=('・DMを受け取れる設定にしてください（.txtはDMに届きます）\n'
               f'・ステージクリアは最大{AGED_CLEAR_RETRY}回まで再試行します。'
               '失敗してもメアド/パスワードは通知します（クリア失敗と明記）\n'
               '・件数が多いと時間がかかります（登録メール待ちが1件あたり最大'
               f'{int(MAIL_WAIT_TIMEOUT)}秒）\n'
               f'・同時に作成できるのは{MAX_CONCURRENT_ACCOUNTS}件までです'),
        inline=False,
    )
    return e


def vip_account_embed() -> discord.Embed:
    stages = vip_stages()
    e = discord.Embed(
        title='🎰 ぷにぷに 回し放題代行（無料）',
        description=(
            '**🆕 初期垢を作って回し放題**\n'
            '① 捨てメールで LEVEL5 ID とセーブデータを新しく作成\n'
            f'② 第1章 {len(stages)}面（`{stages[0]}`〜`{stages[-1]}`）を新しい垢の安全な速さでクリア\n'
            f'③ **まわし放題ガシャ** を指定の連数（既定{VIP_GACHA_COUNT}）ぶん引く\n'
            '④ メアド/パスワードと、引いたキャラをDMでお届け（1垢 5分ほど）\n\n'
            '**🎰 既存の垢に回し放題**\n'
            '① お持ちの垢の `メールアドレス` `パスワード` と連数を入力\n'
            '② ログインして、まわし放題ガシャを引けるか確認（引き済みの垢は引きません）\n'
            '③ 指定の連数ぶん引いて、引いたキャラをDMでお届け（30秒ほど）'
        ),
        color=COLOR_INFO,
    )
    e.add_field(
        name='🔢 連数',
        value=('・既定は **240**（今のガシャの上限）\n'
               '・上限を超える指定は上限に合わせます。超えて送るとサーバーに弾かれ（rc=4）、'
               '垢が危なくなるためです\n'
               '・まわし放題ガシャは **1垢1回** です'),
        inline=False,
    )
    e.add_field(
        name='📨 受け取り方',
        value=(f'・**{ACCOUNT_TXT_THRESHOLD}件未満** … 1件ずつ埋め込みでDM\n'
               f'・**{ACCOUNT_TXT_THRESHOLD}件以上** … `メアド:パスワード` を並べた '
               '**.txt** をDMに添付（引いたキャラも下に記載）'),
        inline=False,
    )
    e.add_field(
        name='⚠ 注意',
        value=('・DMを受け取れる設定にしてください\n'
               '・既存の垢は、終わるまでゲームアプリを開かないでください\n'
               '・管理者の作成停止・メンテは面ごとに確認して、その場で打ち切ります\n'
               f'・同時に作成できるのは{MAX_CONCURRENT_ACCOUNTS}件までです'),
        inline=False,
    )
    return e


def paid_vip_embed() -> discord.Embed:
    stages = vip_stages()
    vip = vip_gacha_status()

    def yen(v):
        return f'**¥{v:,}**' if v > 0 else '**受付停止中**（料金未設定）'

    e = discord.Embed(
        title='🎰 ぷにぷに 回し放題代行',
        description=(
            f'**🆕 初期垢を作って回し放題** … 1垢 {yen(VIP_PRICE)}（1回のご注文で {VIP_PAID_MAX}垢まで）\n'
            f'・第1章 {len(stages)}面クリア済み'
            + ('（イベントステージ解放済み）' if stages[-1] >= 1001015 else '') + 'の新しい垢に、'
            f'まわし放題ガシャ（既定 {VIP_GACHA_COUNT}連）を引いてお届け\n\n'
            f'**🎰 既存の垢に回し放題** … 1垢 {yen(VIP_EXIST_PRICE)}\n'
            '・お持ちの垢にログインして、まわし放題ガシャ（既定 '
            f'{VIP_GACHA_COUNT}連）を引きます。**引けることを確認してからお支払い**いただきます\n\n'
            'お支払いは **PayPay**。メアド/パスワードと引いたキャラ（ランク別）を **DM** でお届けします'
        ),
        color=COLOR_OK if (VIP_PRICE > 0 or VIP_EXIST_PRICE > 0) and vip else COLOR_WARN,
    )
    e.add_field(
        name='🛒 ご購入の流れ',
        value=('① ボタンを押して必要事項を入力\n'
               '② 表示された金額を **請求リンク** からPayPayで支払う\n'
               '③ PayPayの **取引番号** を入力 → 確認できたら自動で開始\n'
               '④ 完了したらDMでお届け（初期垢は1垢5分ほど・既存の垢は30秒ほど）\n'
               '・支払いの画面が消えたら **🧾 お支払い途中のご注文** から続きができます'),
        inline=False,
    )
    e.add_field(
        name='🎰 まわし放題ガシャ',
        value=(f'{vip["name"]}（{str(vip["end"])[:16]} まで・上限 {vip["limit"]}連・1垢1回）' if vip
               else '現在は開催していないため、受付を停止しています'),
        inline=False,
    )
    e.add_field(
        name='⚠ ご注意',
        value=('・DMを受け取れる設定にしてください\n'
               '・既存の垢は、お支払いから完了までゲームアプリを開かないでください\n'
               '・初期垢で作成に失敗した垢は自動で作り直します。それでも足りないときは'
               '取引番号を添えてご連絡ください\n'
               '・メンテ中・停止中は受け付けません'),
        inline=False,
    )
    return e


def hitodama_test_embed() -> discord.Embed:
    e = discord.Embed(
        title='🧪 ひとだま自動購入 検証パネル',
        description=(
            '🔥 **所持数確認** … ひとだま／Yマネー／Yポイントを表示し、'
            '自動購入の発動条件を判定します' + chr(10) +
            '🛒 **購入テスト** … 実際に buyHitodama.nhn を叩いてYマネーで買い足します' + chr(10) + chr(10) +
            '使用前に一度、通常パネル（📋 ステージ一覧 など）でログインしておくと'
            'パス入力なしで使えます。'
        ),
        color=COLOR_WARN,
    )
    e.add_field(
        name='⚙ 周回での自動購入の条件',
        value=(f'ひとだま合計が **{HITODAMA_LOW}個以下** のとき、'
               f'**{HITODAMA_BUY}個** までYマネーで自動補充します'
               '（50個パック=6,000Yマネーを繰り返し購入）。' + chr(10) +
               'ひとだま不足でバトルを開始できないとき、または買えない'
               '（Yマネー不足など）ときは周回を強制停止します。'),
        inline=False,
    )
    e.add_field(
        name='⚠ 注意',
        value=('・**Yマネーを実際に消費します**（5個=1,000 / 20個=3,000 / 50個=6,000）' + chr(10) +
               '・目標個数を「現在の合計+5」などにすると1パックだけの購入で済みます' + chr(10) +
               '・周回実行中にこのパネルのボタンを押すと周回側のtokenが一時的にズレて'
               'エラー（rc=32）になりますが、Botが再ログインして自動で続行します'),
        inline=False,
    )
    return e


# ============================================================================
# Bot
# ============================================================================

# --- Discord-Cheat ハブから差し込むフック(単体起動のときは空のまま) ---
SETUP_HOOKS: list = []      # async def hook(bot): 他ゲームのCog登録やAPI起動。tree.sync より前に呼ぶ
READY_HOOKS: list = []
SKIP_DEFAULT_SYNC = False   # True のときはハブ側が tree.sync を行う


class YWPBot(commands.Bot):
    def __init__(self):
        # メッセージ/メンバーのキャッシュは使っていないので持たない(省メモリ。少RAMのホストで効く)
        super().__init__(command_prefix='!ywp ', intents=discord.Intents.default(),
                         help_command=None, max_messages=None,
                         member_cache_flags=discord.MemberCacheFlags.none())

    async def setup_hook(self):
        # 再起動後もパネル/中止ボタンが動くように永続Viewを登録
        self.add_view(PanelView())
        self.add_view(CancelView())
        self.add_view(HitodamaTestView())
        self.add_view(AgedAccountView())
        self.add_view(VipAccountView())
        self.add_view(PaidVipView())
        self.add_view(AdminPanelView())
        for hook in SETUP_HOOKS:
            await hook(self)
        if not SKIP_DEFAULT_SYNC:
            await self.tree.sync()

    async def on_ready(self):
        print(f'[bot] ログイン: {self.user} ({self.user.id})')
        print(f'[bot] サーバー数: {len(self.guilds)}')
        print(f'[bot] プロキシ: {proxy_pool.status_text()}')
        await self.change_presence(
            activity=discord.Game(name='ぷにぷに 自動周回'))
        # 再起動前に動いていた周回を続きから再開する（2回目以降の on_ready では何もしない）
        for hook in READY_HOOKS:
            await hook(self)
        await restore_jobs()
        if not getattr(self, '_orders_checked', False):
            self._orders_checked = True
            await report_unfinished_orders()


bot = YWPBot()


@bot.tree.command(name='パネル設置', description='自動周回パネルをこのチャンネルに設置します')
async def setup_panel(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
    channel = interaction.channel
    if channel is None or not isinstance(channel, discord.abc.Messageable):
        return await interaction.response.send_message(
            '❌ このチャンネルには設置できません。', ephemeral=True)
    try:
        await channel.send(embed=panel_embed(), view=PanelView())
    except discord.Forbidden:
        return await interaction.response.send_message(
            '❌ このチャンネルに送信する権限がありません。', ephemeral=True)
    await interaction.response.send_message('✅ パネルを設置しました。', ephemeral=True)


@bot.tree.command(name='ひとだま検証設置',
                  description='ひとだま自動購入の検証パネルをこのチャンネルに設置します')
async def setup_hitodama_test_panel(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
    channel = interaction.channel
    if channel is None or not isinstance(channel, discord.abc.Messageable):
        return await interaction.response.send_message(
            '❌ このチャンネルには設置できません。', ephemeral=True)
    try:
        await channel.send(embed=hitodama_test_embed(), view=HitodamaTestView())
    except discord.Forbidden:
        return await interaction.response.send_message(
            '❌ このチャンネルに送信する権限がありません。', ephemeral=True)
    await interaction.response.send_message('✅ 検証パネルを設置しました。', ephemeral=True)


@bot.tree.command(name='コインガチャ', description='ログイン中の垢で、持っているコインを選んでガチャを引きます')
async def coin_gacha_command(interaction: discord.Interaction):
    await open_coin_gacha(interaction)


@bot.tree.command(name='周回状況', description='自分の周回の進捗を表示します')
async def farm_status(interaction: discord.Interaction):
    jobs = jobs_of(interaction.user.id)
    if not jobs:
        return await interaction.response.send_message(
            '📭 実行中の周回はありません。', ephemeral=True)
    # 開いただけでは取り直さない。最新にしたいときは「🔄 更新」を押す
    if len(jobs) == 1:
        embed, view = progress_embed(jobs[0]), CancelView(jobs[0])
    else:
        embed, view = jobs_list_embed(jobs), JobsCancelView(jobs)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name='ステージ一覧', description='クリア済みステージIDを分類別に表示します')
@app_commands.describe(keyword='ステージIDの一部で絞り込み（任意）',
                       show_all='未クリアのステージも含めて表示する',
                       include_master='マスタから未解放ステージも取得する（.txtで添付）')
async def stage_list(interaction: discord.Interaction, keyword: str = '',
                     show_all: bool = False, include_master: bool = False):
    if interaction.user.id not in SESSIONS:
        return await interaction.response.send_message(
            'ℹ️ まだログイン情報がありません。\n'
            'パネルの **📋 ステージ一覧** ボタンからメール/パスワードを入力してください。'
            '（一度ログインすればこのコマンドが使えるようになります）',
            ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    c = await acquire_client(interaction.user.id)
    if c is None:
        return await interaction.followup.send(
            '⚠ ログイン情報が期限切れでした。パネルの **📋 ステージ一覧** から入力し直してください。',
            ephemeral=True)
    try:
        await send_stage_list(interaction, c, keyword, show_all, include_master)
    finally:
        proxy_pool.release_client(c)


@bot.tree.command(name='ログアウト', description='Botが保持しているログイン情報を消去します')
async def logout(interaction: discord.Interaction):
    existed = SESSIONS.pop(interaction.user.id, None) is not None
    await interaction.response.send_message(
        '✅ ログイン情報を消去しました。' if existed else 'ℹ️ 保持しているログイン情報はありません。',
        ephemeral=True)


@bot.tree.command(name='管理パネル',
                  description='Botの動作設定を編集する管理パネルを設置します')
async def setup_admin_panel(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
    channel = interaction.channel
    if channel is None or not isinstance(channel, discord.abc.Messageable):
        return await interaction.response.send_message(
            '❌ このチャンネルには設置できません。', ephemeral=True)
    try:
        await channel.send(embed=admin_panel_embed(), view=AdminPanelView())
    except discord.Forbidden:
        return await interaction.response.send_message(
            '❌ このチャンネルに送信する権限がありません。', ephemeral=True)
    await interaction.response.send_message('✅ 管理パネルを設置しました。', ephemeral=True)


@bot.tree.command(name='久垢作成設置',
                  description='久垢作成パネルをこのチャンネルに設置します')
async def setup_aged_panel(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
    channel = interaction.channel
    if channel is None or not isinstance(channel, discord.abc.Messageable):
        return await interaction.response.send_message(
            '❌ このチャンネルには設置できません。', ephemeral=True)
    try:
        await channel.send(embed=aged_account_embed(), view=AgedAccountView())
    except discord.Forbidden:
        return await interaction.response.send_message(
            '❌ このチャンネルに送信する権限がありません。', ephemeral=True)
    await interaction.response.send_message('✅ 久垢作成パネルを設置しました。', ephemeral=True)


@bot.tree.command(name='回し放題垢作成設置',
                  description='回し放題代行（無料）のパネルを設置します。初期垢作成／既存の垢の2メニュー')
async def setup_vip_panel(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
    channel = interaction.channel
    if channel is None or not isinstance(channel, discord.abc.Messageable):
        return await interaction.response.send_message(
            '❌ このチャンネルには設置できません。', ephemeral=True)
    try:
        await channel.send(embed=vip_account_embed(), view=VipAccountView())
    except discord.Forbidden:
        return await interaction.response.send_message(
            '❌ このチャンネルに送信する権限がありません。', ephemeral=True)
    await interaction.response.send_message('✅ 回し放題垢作成パネルを設置しました。', ephemeral=True)


@bot.tree.command(name='実績チャンネル設定',
                  description='完了実績を投稿するチャンネルを、無料メニューと有料メニューで別々に設定します')
@app_commands.describe(kind='どちらの実績の投稿先か', channel='投稿先のチャンネル（省略すると投稿をやめる）')
@app_commands.choices(kind=[app_commands.Choice(name='無料メニュー', value='free'),
                            app_commands.Choice(name='有料メニュー', value='paid')])
async def set_result_channel(interaction: discord.Interaction, kind: app_commands.Choice[str],
                             channel: discord.TextChannel | None = None):
    if not is_admin(interaction.user):
        return await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
    try:
        import hub.achievements  # noqa: F401
        return await interaction.response.send_message(
            'ℹ 実績の投稿先は管理サイトの「代行管理」タブで設定します（3ゲーム共通）。', ephemeral=True)
    except ImportError:
        pass
    if channel is not None:
        me = channel.guild.me
        perms = channel.permissions_for(me) if me else None
        if perms is None or not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                f'❌ {channel.mention} に投稿する権限がBotにありません'
                '（チャンネルを見る・メッセージを送信・埋め込みリンクが必要）。', ephemeral=True)
    d = results_conf()
    if channel is None:
        d.pop(kind.value, None)
    else:
        d[kind.value] = channel.id
    results_save(d)
    log_line(f'実績チャンネル設定 {kind.value}={channel.id if channel else "なし"} by {interaction.user.id}')
    await interaction.response.send_message(
        f'✅ **{kind.name}** の完了実績の投稿先を '
        + (f'{channel.mention} にしました。' if channel else '解除しました（投稿しません）。')
        + f'\n\n**今の設定**\n{results_text()}', ephemeral=True)


@bot.tree.command(name='回し放題垢有料版設置',
                  description='回し放題代行の有料版（PayPay払い）パネルを設置します。料金は /管理パネル で設定')
async def setup_paid_vip_panel(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
    channel = interaction.channel
    if channel is None or not isinstance(channel, discord.abc.Messageable):
        return await interaction.response.send_message(
            '❌ このチャンネルには設置できません。', ephemeral=True)
    try:
        msg = await channel.send(embed=paid_vip_embed(), view=PaidVipView())
    except discord.Forbidden:
        return await interaction.response.send_message(
            '❌ このチャンネルに送信する権限がありません。', ephemeral=True)
    _paid_panels_save(_paid_panels() + [{'channel': channel.id, 'message': msg.id}])
    status, _ = puni_pay.wallet_status()
    notes = []
    for v, label in ((VIP_PRICE, '回し放題垢（有料版）の1垢の料金'),
                     (VIP_EXIST_PRICE, '既存の垢に240連（有料版）の料金')):
        if v <= 0:
            notes.append(f'⚠ 「{label}」が未設定（0円）なので、そのメニューはまだ受け付けません。'
                         '`/管理パネル` の設定で決めてください。')
    if not results_conf().get('paid'):
        notes.append('ℹ 有料メニューの完了実績の投稿先が未設定です（`/実績チャンネル設定`）。')
    if not puni_pay.is_ready():
        notes.append('⚠ PayPayの受取アカウントがありません。'
                     + ('PAY管理BOTでログインしてください。' if puni_pay.USING_PAYMASTER
                        else '`/paypayログイン1` でログインしてください。'))
    await interaction.response.send_message(
        '✅ 回し放題垢 有料版パネルを設置しました。\n' + '\n'.join(notes)
        + f'\n-# 受取: {status.splitlines()[0]}', ephemeral=True)


@bot.tree.command(name='アカウント作成', description='捨てメールでLEVEL5 IDを自動作成します')
@app_commands.describe(
    count=f'作成するアカウントの数（1〜{MAX_ACCOUNTS_PER_RUN:,}・省略時は1）',
    password='固定パスワード（省略時はランダム生成）',
    name='アカウント名（省略時はランダム。{n} は連番に置換）')
async def account_create(interaction: discord.Interaction,
                         count: app_commands.Range[int, 1, MAX_ACCOUNTS_PER_RUN] = 1,
                         password: str | None = None,
                         name: str | None = None):
    await start_account_creation(interaction, int(count), password, name)


@bot.tree.command(name='久垢作成',
                  description=f'アカウントを作り、ステージ{AGED_STAGE_ID}をクリアしてからDMします')
@app_commands.describe(
    count=f'作成するアカウントの数（1〜{MAX_ACCOUNTS_PER_RUN:,}・省略時は1）',
    password='固定パスワード（省略時はランダム生成）',
    name='アカウント名（省略時はランダム。{n} は連番に置換）')
async def aged_account_create(interaction: discord.Interaction,
                              count: app_commands.Range[int, 1, MAX_ACCOUNTS_PER_RUN] = 1,
                              password: str | None = None,
                              name: str | None = None):
    await start_account_creation(interaction, int(count), password, name, aged=True)


# --- PayPayログイン（有料版の受取口座。PAY管理BOT が無い環境用） ---
@bot.tree.command(name='paypayログイン1',
                  description='有料版の受取用PayPayにログインします（電話番号とパスワード → SMS送信）')
@app_commands.describe(phone='電話番号（例: 09012345678）', password='PayPayのパスワード')
async def paypay_login1(interaction: discord.Interaction, phone: str, password: str):
    if not is_admin(interaction.user):
        return await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    status, msg = await asyncio.to_thread(puni_pay.login_start, interaction.user.id, phone, password)
    password = None   # 参照を残さない
    if status == 'otp_required':
        await interaction.followup.send(
            f'📩 {msg}\n届いたURL（またはコード）を `/paypayログイン2` に貼ってください。\n'
            '-# PayPay側の受付は3分ほどで切れます。SMSが届いたらすぐに。', ephemeral=True)
    elif status == 'ok':
        wallet, _ = await asyncio.to_thread(puni_pay.wallet_status)
        await interaction.followup.send(f'✅ {msg}\n{wallet}', ephemeral=True)
    else:
        await interaction.followup.send(f'❌ {msg}', ephemeral=True)


@bot.tree.command(name='paypayログイン2',
                  description='SMSに届いた認証URL（またはコード）を入れてPayPayログインを完了します')
@app_commands.describe(otp='SMSに届いた認証URL または 4桁コード')
async def paypay_login2(interaction: discord.Interaction, otp: str):
    if not is_admin(interaction.user):
        return await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    ok, msg = await asyncio.to_thread(puni_pay.login_complete, interaction.user.id, otp)
    if not ok:
        return await interaction.followup.send(
            f'❌ {msg}\n-# 時間切れのときは `/paypayログイン1` からやり直してください。', ephemeral=True)
    wallet, _ = await asyncio.to_thread(puni_pay.wallet_status)
    await interaction.followup.send(f'✅ {msg}\n{wallet}', ephemeral=True)


@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await report_error(interaction, error, 'tree')


def main():
    # print の出力もAPIの往復も同じファイルに時系列で残す
    path = setup_log(tee=True)
    if path:
        print(f'[log] {path} に記録します（止めるには YWP_LOG=0）')
    token = resolve_token()
    if not token:
        raise SystemExit(
            'DISCORD_TOKEN が設定されていません。\n'
            '  環境変数 DISCORD_TOKEN に設定するか、\n'
            f'  {CONFIG_FILE} に {{"token": "<Botトークン>"}} を置いてください。')
    bot.run(token)


if __name__ == '__main__':
    main()
