#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ぷにぷに 対話型自動周回スクリプト
メール/パスワードでログイン → UDkey自動取得 → ステージ自動周回
"""

import base64
import contextvars
import datetime
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
import zlib
from urllib.parse import urljoin, urlparse, parse_qs
import requests

# pycryptodome は Crypto、pycryptodomex は Cryptodome に入る。どちらでも動くように
try:
    from Crypto.Cipher import AES
except ImportError:
    try:
        from Cryptodome.Cipher import AES
    except ImportError:
        raise ImportError(
            'AES実装が見つかりません。次のどちらかを入れてください:\n'
            '  pip install pycryptodome   (Crypto)\n'
            '  pip install pycryptodomex  (Cryptodome)')

# ============================================================================
# 定数設定
# ============================================================================

AESK = bytes.fromhex('a865d7e5e2458f8ce1b5ecd087e54594')
K = b'0bk2kvtFE2'
APKEY = 'a-zrhgm09pgcgjc1iv9cxvpk3xm9b0ynyo4u00sny6bjq10nrx3up2yrhjnq2lhg'
SIGNATURE = ('s4X9CoyxGma3kGuAp5woThgvBX3dCi77Slh5RcOo6ybmMTt0J4CGiZwyiCsil7P3'
             'MVgjiVt+kGE1MqvttCXLB+hlOpyTkJp5a78TXthBNVw=')
GS = 'https://gameserver.yw-p.com'
L5 = 'https://api.level5-id.com'

MODEL = 'GA00747-UK'
OSVER = '9'
APPVER = '4.175.0'    # 2026-09-30 のメンテで 4.174.0 は rc=20（更新してください）になった

# タイミング下限（これより短くはしない）
BATTLE_TIME_MIN = 7.5       # gameStart→gameEnd の最短間隔（秒）
BATTLE_TIME_SPREAD = 0.2    # バトル時間の上振れ幅（+0〜20% → 7.5〜9.0秒に散らす）
COOLDOWN_MIN = 2.0          # バトル終了→次バトルの最短間隔（秒）
TIME_SPREAD = 0.45          # クールダウン等その他の待機の上振れ幅（+0〜45%）
# 成功したgameEnd→次のgameEndの最短間隔（秒）。10秒を切ると gameEnd が rc=3（パラメータ
# 不正）で弾かれ、rc=3/rc=4 を出した垢は BAN された（2026-09-28 に使い捨て垢で確認）。
# 余裕をみて11秒。battle_end が送る直前に必ず守る
MIN_BATTLE_CYCLE = 11.0
# 通常の面（ステージ種別1）では、サーバーが「そのデッキの攻撃力でその速さは可能か」を
# 見ていて、ありえない速さのクリアは gameEnd が rc=4（エラーが発生しました。タイトルに
# 戻ります）で弾かれ、その垢は BAN される（2026-09-28。rc=4 を出した6垢すべて）。
#   通った: 1秒あたりダメージ÷デッキ攻撃力 ≦0.35 / 弾かれた: 0.37〜0.44（弱い新規デッキ）
#   強いデッキ（攻撃力3〜4倍）は同じ面を8秒で全部通った。実機の手動プレイは最大0.22
# 29704006 など種別20の面はこの判定がないらしく、HP1,270万を8秒で申告しても通る
DPS_RATIO_MAX = 0.30        # これを超えない時間をかけてからクリアを申告する
MAX_CLEAR_TIME = 30.0       # これより長くかかる面はデッキに対して硬すぎる（周回側で手前の面に下がる）
# この回数だけ勝った垢は「育った垢」として速さの制限（DPS_RATIO_MAX）を外す（min_clear_time 参照）。
# 30まんは安全な速さで約130勝したあと、速くしても全部通った。100 はユーザーの指定
# （2026-09-30。100〜130勝のあいだは未確認）。管理パネルで変えられる
TRUST_BATTLES = 100
# ひとだま自動購入で大きいパックを買う所持Yマネーの下限。
# これを下回るあいだは一番安いパック(1,000)だけにして財布を空にしない。
# /管理パネル から変更できる（bot.py の HITODAMA_RICH と連動）
HITODAMA_RICH = 6000    # 50個パック（6,000・1個120）を買える額。以前は 10000 で、作りたての垢は
                        # 1個200の5個パックばかり買ってYマネーが尽きていた（2026-09-30）
# 余裕がないときに1回で買い足す上限（個）。
# 安いパックで target(既定500)まで埋めようとすると財布を使い切るので、
# 数戦ぶんだけ買ってつなぎ、残りは周回で貯める
HITODAMA_POOR_BUY = 20
# 2026-09-24 実測で旧説を撤回: gameEnd間隔10.0秒の連戦6/6が全部成功した。
# サーバーは連戦間隔を弾いていない（旧説「35秒未満はrc=3で拒否」は誤りで、
# 実機の最短gameEnd間隔34.9秒は単に実機バトルの所要時間でしかなかった。
# 40秒制限を入れたせいで効率が5000pt/h→約1000pt/hに落ちていた）。
# 自然な周期はバトル7.5〜9秒＋クールダウンで約10〜12秒なので、この下限は
# 普段は発動しない保険として残す（異常設定で極端に詰めるのを防ぐだけ）。
def battery_info():
    """login.nhn に載せる端末のバッテリー状態

    定数にすると「常に残量100%・26.1℃・4300mV」を報告し続けることになる。
    実機ならありえない並びなので、残量・電圧・温度を毎回それらしく作る。
    Android の BatteryManager に合わせて state は 2=充電中 3=放電中 5=満充電。
    """
    level = random.randint(32, 100)
    if level >= 98:
        state = random.choice([5, 2])              # 満充電 or 充電中
    else:
        state = 2 if random.random() < 0.25 else 3
    # Li-poly は残量に電圧が連動する（空:約3.5V 満:約4.35V）。充電中は少し高め
    volt = 3500 + level / 100.0 * 850 + (45 if state == 2 else 0)
    return {
        'level': level,
        'state': state,
        'technology': 'Li-poly',
        # 1/10℃単位。プレイ中の端末は常温より高く、充電中はさらに上がる
        'temperature': random.randint(255, 340) + (random.randint(10, 45) if state == 2 else 0),
        'voltage': int(volt + random.randint(-25, 25)),
    }

UA = 'Dalvik/2.1.0 (Linux; U; Android %s; %s Build/PI) com.Level5.YWP/%s' % (OSVER, MODEL, APPVER)
HDR = {'Accept-Encoding': 'identity', 'User-Agent': UA, 'Accept': 'application/json',
       'Content-Type': 'application/json', 'Connection': 'Keep-Alive'}

# ============================================================================
# プロキシ
#   通信を全部ここ経由にする。優先順位は
#       set_proxy() での明示指定 > 環境変数 YWP_PROXY > config.json の "proxy"
#   受け付ける書き方:
#       103.4.76.237:1111              → http://103.4.76.237:1111 として扱う
#       http://host:port
#       http://user:pass@host:port     → 認証つき
#       socks5://host:port             → 要 PySocks（pip install requests[socks]）
#   HTTPS も CONNECT トンネルで同じプロキシを通るので http/https 両方に入れる。
# ============================================================================

PROXIES: dict = {}
_ENV_SET = False        # HTTP_PROXY等を自分で書いたかどうか


def normalize_proxy(v):
    """プロキシ指定を requests の proxies dict にする。空なら {}"""
    s = str(v or '').strip()
    if not s:
        return {}
    if '://' not in s:
        s = 'http://' + s
    return {'http': s, 'https': s}


def set_proxy(v):
    """プロキシを設定する。None/空 で無効化。設定後の dict を返す

    requests には proxies= で明示的に渡すが、bot.py の aiohttp（LEVEL5 ID の
    登録フローと捨てメール）は ClientSession(trust_env=True) で環境変数を見る。
    どちらも同じ出口を通すため、ここで環境変数にも反映しておく。
    """
    global PROXIES, _ENV_SET
    PROXIES = normalize_proxy(v)
    names = ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')
    if PROXIES:
        for n in names:
            os.environ[n] = PROXIES['http']
        _ENV_SET = True
        log_line('[net] プロキシ経由: %s' % mask_proxy(PROXIES['http']))
    elif _ENV_SET:
        # 自分で入れたぶんだけ消す。もともとOS側に設定されていた
        # HTTP_PROXY を勝手に消さないため
        for n in names:
            os.environ.pop(n, None)
        _ENV_SET = False
    return PROXIES


def mask_proxy(url):
    """ログに出すとき認証情報を隠す"""
    if '@' in url:
        head, _, tail = url.rpartition('@')
        scheme = head.split('://', 1)[0] if '://' in head else ''
        return '%s://***@%s' % (scheme, tail) if scheme else '***@' + tail
    return url


def _proxy_from_config():
    """config.json の "proxy" を読む（存在しなければ空）"""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
        with open(path, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        return cfg.get('proxy') if isinstance(cfg, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


_PROXY_READY = False


def init_proxy():
    """環境変数 → config.json の順に見て自動設定する"""
    global _PROXY_READY
    _PROXY_READY = True
    if PROXIES:
        return PROXIES
    return set_proxy(os.environ.get('YWP_PROXY') or _proxy_from_config())


def _proxies():
    """通信直前に呼ぶ。初回だけ環境変数/config.jsonを読む

    log_line がこの下で定義されているので import 時ではなく遅延で初期化する。
    """
    if not _PROXY_READY:
        init_proxy()
    return PROXIES


def check_proxy(timeout=15):
    """プロキシが生きているか・外から見えるIPが何かを確かめる

    ゲームサーバに投げる前にこれで切り分けられる。
    """
    px = _proxies()
    out = {'proxy': mask_proxy(px.get('http', '')) or '(直接接続)'}
    try:
        r = requests.get('https://api.ipify.org', proxies=px,
                         timeout=timeout, headers={'User-Agent': UA})
        out['ip'] = r.text.strip()
    except Exception as e:
        out['ip'] = None
        out['error'] = '%s: %s' % (type(e).__name__, str(e)[:160])
    try:
        r = requests.get('%s/api/v1/active/' % L5, params={'apkey': APKEY},
                         proxies=px, timeout=timeout,
                         headers={'User-Agent': UA})
        out['level5_id_status'] = r.status_code
    except Exception as e:
        out['level5_id_status'] = None
        out['level5_id_error'] = '%s: %s' % (type(e).__name__, str(e)[:160])
    return out

RC = {0: 'OK', -1: 'IPブロック/復号不可',
      3: 'パラメータ不正', 5: 'ステージ拒否(ひとだま不足でも出る)',
      20: 'appVerかマスタ版が古い', 30: '署名の不整合', 32: 'tokenズレ',
      37: 'チュートリアル未完了', 100: 'stageIdを受理できない(マスタ不一致)',
      101: 'マスタ版が現行と違う',
      202: 'BAN', 206: '購入失敗(Yマネー不足・販売期間外など)',
      # 2026-09-24 実測: gameStart で約0.7%（10,380周中74回）発生。意味は未特定だが
      # 次の試行で必ず復帰しており、連続もしないので一過性のものとして再試行に任せる。
      1701: 'gameStart一時拒否(原因未特定・再試行で復帰)'}

GAME_CONST = [
    {"constType": 5, "mstKey": "blockComboAdjustNumA", "mstValue": "0.051800"},
    {"constType": 5, "mstKey": "blockComboAdjustNumB", "mstValue": "0.995500"},
    {"constType": 5, "mstKey": "blockSizeAdjustA", "mstValue": "0.000800"},
    {"constType": 5, "mstKey": "blockSizeAdjustB", "mstValue": "0.077000"},
    {"constType": 5, "mstKey": "blockSizeAdjustC", "mstValue": "-0.058500"},
    {"constType": 5, "mstKey": "blockSizeAdjustSkillA", "mstValue": "5.780200"},
    {"constType": 5, "mstKey": "blockSizeAdjustSkillB", "mstValue": "-2.201000"},
    {"constType": 5, "mstKey": "blockSizeAdjustSkillC", "mstValue": "1.000000"},
    {"constType": 5, "mstKey": "blockSizeRate1", "mstValue": "0.019300"},
    {"constType": 5, "mstKey": "blockSizeRate2", "mstValue": "0.058600"},
    {"constType": 5, "mstKey": "blockSizeRate3", "mstValue": "0.137900"},
    {"constType": 5, "mstKey": "comboEnableSec", "mstValue": "3.000000"},
    {"constType": 5, "mstKey": "comboEnableSize", "mstValue": "2"},
    {"constType": 5, "mstKey": "damageSwitchSize", "mstValue": "3"},
    {"constType": 5, "mstKey": "feverDamageAdjustNum", "mstValue": "0.100000"},
    {"constType": 5, "mstKey": "feverScoreAdjustNum", "mstValue": "0.100000"},
    {"constType": 5, "mstKey": "saBlockSizeAdjustSubA", "mstValue": "0.006200"},
    {"constType": 5, "mstKey": "saBlockSizeAdjustSubB", "mstValue": "-0.024500"},
    {"constType": 5, "mstKey": "saBlockSizeAdjustSubC", "mstValue": "0.416900"},
    {"constType": 5, "mstKey": "scoreAdjustNumA", "mstValue": "11.223000"},
    {"constType": 5, "mstKey": "scoreAdjustNumB", "mstValue": "0.047700"},
    {"constType": 5, "mstKey": "skillGaugeIncrementSize1", "mstValue": "0.100000"},
    {"constType": 5, "mstKey": "skillGaugeIncrementSize2", "mstValue": "1.200000"},
    {"constType": 5, "mstKey": "skillGaugeIncrementSize3", "mstValue": "2.400000"},
]

# ============================================================================
# 暗号化・復号化関数
# ============================================================================

def salt20(body):
    return hashlib.sha1(K + hashlib.sha1(K + b' ' + body).digest()).digest()

def enc(body):
    pt = salt20(body) + body
    p = 16 - len(pt) % 16
    pt += bytes([p]) * p
    return base64.urlsafe_b64encode(AES.new(AESK, AES.MODE_ECB).encrypt(pt)).decode().rstrip('=')

def dec(s):
    s = s.strip()
    ct = base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))
    pt = AES.new(AESK, AES.MODE_ECB).decrypt(ct)
    rest = pt[20:]
    g = rest.find(b'\x1f\x8b')
    if g >= 0:
        try:
            return zlib.decompress(rest[g:], 47)
        except Exception:
            pass
    try:
        rest = rest[:-pt[-1]]
    except Exception:
        pass
    e = max(rest.rfind(b'}'), rest.rfind(b']'))
    return rest[:e + 1] if e >= 0 else rest

def jbody(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')

def jitter(base, minimum, spread=None):
    """下限を保証したうえで上方向にだけランダム変動させる

    下限に張り付いて同じ値が並ぶと不自然なので、[base, base*(1+spread)] で散らす。
    バトル時間は 7.5秒×(1+0〜0.2) → 7.5〜9.0秒（報告クリア秒は8/9の2通り）。
    旧来の7〜10.2秒より中央に寄せて上限を詰めている。もっと散らしたければ
    BATTLE_TIME_SPREAD を大きくする（小さいほど速いが値は偏る）。
    """
    spread = TIME_SPREAD if spread is None else spread
    base = max(float(base), float(minimum))
    return random.uniform(base, base * (1.0 + spread))

def _share(total, weights):
    """total を weights の比で整数分配する（端数は最大枠に寄せ、合計は必ず total）"""
    if not weights:
        return []
    tw = sum(weights)
    if tw <= 0:
        return [total] + [0] * (len(weights) - 1)
    out = [int(total * w / tw) for w in weights]
    out[weights.index(max(weights))] += total - sum(out)
    return out

def _peak(total, parts, lo, hi, min_each=0):
    """total を parts 回に分けたときの「最大の1回」らしい値

    平均×[lo, hi] で決めるが、平均未満／合計超過にはならないよう丸める。
    合計値と突き合わせても矛盾しない最大値を返す。
    """
    if total <= 0 or parts <= 0:
        return 0
    if parts == 1:
        return total
    ceil_mean = -(-total // parts)
    cap = max(total - min_each * (parts - 1), ceil_mean)
    return min(max(int(round(total / parts * random.uniform(lo, hi))), ceil_mean), cap)

# gameStartの抽選情報 lotItemInfoList / lotYoukaiInfoList の読み方
#   'off'    … 後半を無視してドロップなしで申告する（安全側）
#   'flag'   … 後半が 0/1 の当落フラグ
#   'itemid' … 後半がドロップしたID（0/00000=なし）
# 2026-09-25 判明（108戦の観測）:
#   lotItemInfoList   = '00000|0' / '00000|1'  → 後半は0/1の当落フラグ（flag方式）
#   lotYoukaiInfoList = '00000|00000'          → 後半は5桁。当選時は妖怪IDの見込み（itemid方式）
# このため build_game_end はフィールドごとに別方式を指定する。
# このスイッチを 'off' に戻すと両方ともドロップなし申告に戻る（総合安全弁）。
LOT_DROP_MODE = 'bits'
LOT_DROP_RATE = 0.85    # 先頭桁が1のときに当たりとして申告する割合（実機実測 11/13）

_lot_seen = {}

def parse_lot_info(raw, mode=None):
    """抽選情報を (checkKey, ドロップしたID) に分解する

    ドロップしていない／判断できない場合は ID に None を返す。
    """
    mode = LOT_DROP_MODE if mode is None else mode
    s = raw if isinstance(raw, str) else ''
    parts = s.split('|')
    key = parts[0] if parts and parts[0] else '00000'
    if mode == 'off' or len(parts) < 2:
        return key, None
    tail = parts[1].strip()
    if not tail.lstrip('-').isdigit():
        return key, None
    n = int(tail)
    if n <= 0:
        return key, None            # 0 はどの解釈でも「なし」
    if mode == 'bits':
        # 実機キャプチャ（敵28体）で確定した読み方。後半は 5桁のビット列で、
        # 同じ '11111' から dropYoukaiFlg=0 と 1 の両方が送られている
        # ＝当落はクライアントが決めており、サーバーはどちらも受理する。
        # 当たりが出たのは先頭桁が 1 のときだけ（13件中11件＝約85%）。
        # 先頭が 0 のものは28体すべて外れだったので絶対に申告しない。
        if tail[0] != '1':
            return key, None
        return (key, 0) if random.random() < LOT_DROP_RATE else (key, None)
    if mode == 'flag':
        return key, (0 if n == 1 else None)   # 当選。IDは別途決まる
    if mode == 'itemid':
        return key, n
    return key, None

_BEFRIEND_LOT = re.compile(r'(\w+)\|([01]{5})')
BEFRIEND_KEY = '00000'

# 好物のランク（ywp_mst_item の5列目。1=ちょびっと 2=それなり 3=けっこう 4=ものすごく）と、
# gameEnd の敵ごとの欄。2026-09-28 実機: ダブルバーガー（ランク3）をあげた敵が
# useItemLarge=1・itemId=10503 で送られた。
# lotYoukaiInfoList の5桁は [餌なし, ランク1, 2, 3, 4] の当落で、キャプチャした11組は
# すべて 0…01…1 の形（高いランクほど当たりやすい）。'00001' の敵にランク3をあげた回は
# クライアントも友だち申告をしなかった（2桁目から数えて4桁目=ランク3 が 0）。
FOOD_FIELDS = {1: 'useItemSmall', 2: 'useItemMiddle', 3: 'useItemLarge', 4: 'useItemLLarge'}

# 妖怪のランク（ywp_mst_youkai の4列目）。ゲーム内の14ランクに、最大レベル
# （1〜8=Lv50, 9=60, 10=70, 11=80, 12=90, 13=100, 14=110）と背景画像（9=ss系,
# 10〜11=zz系, 12〜14=uz系）がそろって対応する（2026-09-28 マスタで確認）
# --- 垢の育ち具合（勝った回数） -------------------------------------------------
# data/account_trust.json: {"wins": {userId: 勝った回数}, "manual": [育った垢として扱う userId]}
# gameEnd が rc=0 になるたびに数える（battle_end）。ファイルが無いときは logs/ の
# 「gameEnd.nhn rc=0 … uid=」の行から数え直して作る。
TRUST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'account_trust.json')
_trust: dict = {}
_trust_lock = threading.Lock()


def _trust_load():
    if _trust:
        return
    try:
        with open(TRUST_FILE, encoding='utf-8') as f:
            d = json.load(f)
        _trust['wins'] = {str(k): int(v) for k, v in (d.get('wins') or {}).items()}
        _trust['manual'] = {str(u) for u in d.get('manual') or []}
        return
    except (OSError, ValueError):
        pass
    wins: dict = {}
    logs = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    pat = re.compile(r'gameEnd\.nhn\s+rc=0 .*?uid=(\d+)')
    try:
        names = sorted(n for n in os.listdir(logs) if n.startswith('ywp-') and n.endswith('.log'))
    except OSError:
        names = []
    for n in names:
        try:
            with open(os.path.join(logs, n), encoding='utf-8', errors='ignore') as f:
                for line in f:
                    m = pat.search(line)
                    if m:
                        wins[m.group(1)] = wins.get(m.group(1), 0) + 1
        except OSError:
            pass
    _trust['wins'], _trust['manual'] = wins, set()
    _trust_save()


def _trust_save():
    try:
        os.makedirs(os.path.dirname(TRUST_FILE), exist_ok=True)
        tmp = TRUST_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'wins': _trust.get('wins', {}), 'manual': sorted(_trust.get('manual', set()))}, f)
        os.replace(tmp, TRUST_FILE)
    except OSError:
        pass


def trust_wins(uid) -> int:
    """その垢が勝った回数（gameEnd rc=0）"""
    with _trust_lock:
        _trust_load()
        return _trust['wins'].get(str(uid), 0)


def is_trusted(uid) -> bool:
    """育った垢か（TRUST_BATTLES 勝以上、または手動で育った垢にした）"""
    if not uid:
        return False
    with _trust_lock:
        _trust_load()
        return str(uid) in _trust['manual'] or _trust['wins'].get(str(uid), 0) >= TRUST_BATTLES


def trust_add(uid, n=1):
    if not uid:
        return
    with _trust_lock:
        _trust_load()
        _trust['wins'][str(uid)] = _trust['wins'].get(str(uid), 0) + n
        _trust_save()


def trust_set_manual(uid, on=True):
    """周回開始時に「育っている垢なので速く回す」を選んだ垢（取り消すときは on=False）"""
    with _trust_lock:
        _trust_load()
        (_trust['manual'].add if on else _trust['manual'].discard)(str(uid))
        _trust_save()


RANK_NAMES = {1: 'E', 2: 'D', 3: 'C', 4: 'B', 5: 'A', 6: 'S', 7: 'SS', 8: 'SSS',
              9: 'Z', 10: 'ZZ', 11: 'ZZZ', 12: 'UZ', 13: 'UZ+', 14: 'UZ++'}
FOOD_BUY_CNT = 3        # 好物を切らしたときにまとめて買う個数


def befriend_lot(raw):
    """lotYoukaiInfoList から申告に使う (キー, 5桁) を取る。読めなければ None

    ふつうは 'キー|5桁' の1組。リーダーによっては 'キー|5桁*キー|5桁*…' の複数組になる
    （2026-09-27: 自動編成で 2175000 をリーダーにした直後から
    '20000|…*30000|…*10000|…*00000|…' の4組になった）。
    複数組のときはキー 00000（基本の組）を使う。この組・このキーで申告して rc=0・
    仲間になる・メニューが開くことを確認済み。他のキーの組は未検証なので使わない。
    """
    parts = [p for p in str(raw or '').strip().split('*') if p]
    found = []
    for p in parts:
        m = _BEFRIEND_LOT.fullmatch(p)
        if not m:
            return None
        found.append((m.group(1), m.group(2)))
    if len(found) == 1:
        return found[0]
    return next((f for f in found if f[0] == BEFRIEND_KEY), None)


def befriend_pick(start_enemies, results, fed=None):
    """gameEnd で「友だちになった」と申告する敵を1体選び、(結果, キー) を返す（無ければ None）

    実機（decrypted_session.json 1〜15面・2026-09-27 キャプチャ）と照合した規則:
      餌を使っていないなら、gameStart の lotYoukaiInfoList の5桁の先頭が 1 の敵のうち、
      最初に倒した（deadEndOrder が最小の）1体だけ（組の選び方は befriend_lot）。
    複数の敵がいる面5件すべてがこの規則どおりで、新規垢で多数申告して全部 rc=0。
    2026-09-25 までに rc=4 で弾かれた26件は、26件とも先頭が 0（外れ）を申告していた。

    メニュー（1=4面 / 2=6面 / 4=8面 / 3=ショップ=12面）はその面で友だちになった
    ときにだけ開く。申告しないと何周してもショップが開かない。
    好物をあげた敵（fed = {敵ID: (アイテムID, ランク)}）は、5桁のうちランクの桁
    （1=ちょびっと … 4=ものすごく）で見る（FOOD_FIELDS 参照）。
    """
    fed = fed or {}
    lots = {e.get('enemyId'): befriend_lot(e.get('lotYoukaiInfoList'))
            for e in start_enemies}

    def digit(eid):
        return lots[eid][1][fed[eid][1] if eid in fed else 0]

    cands = [(r, lots[r.get('enemyId')][0]) for r in results
             if lots.get(r.get('enemyId')) and digit(r.get('enemyId')) == '1']
    return min(cands, key=lambda t: t[0]['deadEndOrder']) if cands else None


def _warn_lot(field, raw):
    """抽選情報の実物を「形」ごとに1回だけ出す

    checkKeyは毎バトル変わるので、文字列そのままで重複を見ると延々と出続ける。
    比較するのは | の後ろの形だけにして、0 / 1 / それ以外の数値 を1例ずつ見せる。
    """
    if not isinstance(raw, str) or not raw or len(_lot_seen) >= 6:
        return
    tail = raw.split('|', 1)[1] if '|' in raw else '<区切りなし>'
    shape = tail if tail in ('0', '1') else ('<0,1以外の数値>' if tail.isdigit()
                                             else '<数値でない>')
    if (field, shape) in _lot_seen:
        return
    _lot_seen[(field, shape)] = raw
    hint = ('' if LOT_DROP_MODE != 'off' else
            '  ← | の後ろが 0/1 だけなら LOT_DROP_MODE="flag"、'
            'アイテムIDらしい数値なら "itemid" に')
    print(f'[ywp] {field} = {raw!r} （後半の形: {shape}）{hint}')

_SECRETISH = ('token', 'sig', 'key', 'pass', 'udkey', 'gdkey',
              'webcode', 'userid', 'mail', 'address', 'characterid')

# ============================================================================
# ログ
# ============================================================================
#
# 不具合を後から追えるように、APIの往復と画面出力を1本の時系列にまとめて
# logs/ywp-YYYYMMDD.log に残す。環境変数で切り替える:
#   YWP_LOG=0       ログを取らない
#   YWP_LOG_FULL=1  成功したリクエストの中身も全部残す（既定は失敗時のみ）

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
LOG_ENABLED = os.environ.get('YWP_LOG', '1') != '0'
LOG_FULL = os.environ.get('YWP_LOG_FULL') == '1'
LOG_VALUE_MAX = 160          # 1つの値をこの長さで打ち切る（セーブ本体が巨大なため）
LOG_MAX_BYTES = 20 * 1024 * 1024   # これを超えたら .part2 .part3 … に移る

# どの処理の最中かを示す札。asyncio.to_thread はコンテキストを引き継ぐので
# 別スレッドで走るAPI呼び出しにも伝わる。
LOG_TAG = contextvars.ContextVar('ywp_log_tag', default='')

_log_lock = threading.Lock()
_log_path = [None]


class log_tag:
    """この中で起きたログ行に札を付ける（例: 'account#1' / 'job#3'）

    `with` でも `async with` でも使える。周回側は同期のwith、アカウント作成側は
    セマフォと並べて `async with A, log_tag(...)` と書くため両対応にしてある。
    """

    def __init__(self, tag):
        self.tag = tag
        self._token = None

    def __enter__(self):
        self._token = LOG_TAG.set(self.tag)
        return self

    def __exit__(self, *exc):
        if self._token is not None:
            LOG_TAG.reset(self._token)
            self._token = None
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *exc):
        return self.__exit__(*exc)


def _log_now():
    return datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]


def _roll_if_big(path):
    """肥大化したら連番で新しいファイルに移る（放置で容量を食い潰さないように）"""
    try:
        if os.path.getsize(path) < LOG_MAX_BYTES:
            return path
    except OSError:
        return path
    base, ext = os.path.splitext(path)
    base = base.split('.part')[0]
    for n in range(2, 1000):
        cand = f'{base}.part{n}{ext}'
        if not os.path.exists(cand) or os.path.getsize(cand) < LOG_MAX_BYTES:
            _log_path[0] = cand
            return cand
    return path


def log_line(text):
    """ログファイルに1行書く（画面には出さない）"""
    if not LOG_ENABLED:
        return
    path = _log_path[0]
    if path is None:
        path = setup_log()
        if path is None:
            return
    tag = LOG_TAG.get()
    head = f'{_log_now()} ' + (f'[{tag}] ' if tag else '')
    try:
        with _log_lock:
            path = _roll_if_big(path)
            with open(path, 'a', encoding='utf-8') as f:
                for i, line in enumerate(str(text).rstrip('\n').split('\n')):
                    f.write((head if i == 0 else ' ' * len(head)) + line + '\n')
    except OSError:
        pass        # ログが書けないだけで周回は止めない


def attach_stdlib_logging(names=('discord',)):
    """discord.py などが logging に出す内容も同じファイルに流す

    レート制限の警告はここにしか出ないので、拾えないと原因が追えない。
    """
    import logging

    class _Handler(logging.Handler):
        def emit(self, record):
            try:
                if _goes_to_tee(record.name):
                    return      # 画面に出る分を tee が拾うので二重になる
                log_line(f'{record.levelname} {record.name}: {record.getMessage()}')
            except Exception:
                pass

    def _goes_to_tee(name):
        """そのロガーが tee 済みのストリームに出しているか

        discord.py は run() の中で自前のStreamHandlerを付けるので、
        付く前か後かで状況が変わる。書き出す時点で判定する。
        """
        lg = logging.getLogger(name)
        while lg:
            for h in lg.handlers:
                if isinstance(getattr(h, 'stream', None), _Tee):
                    return True
            if not lg.propagate:
                break
            lg = lg.parent
        return False

    handler = _Handler()
    for name in names:
        logger = logging.getLogger(name)
        if not any(isinstance(h, _Handler) for h in logger.handlers):
            logger.addHandler(handler)
        if logger.level == logging.NOTSET:
            logger.setLevel(logging.INFO)


def setup_log(tee=False):
    """ログファイルを用意する。tee=True なら print も同じファイルに流す"""
    if not LOG_ENABLED:
        return None
    if _log_path[0] is None:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
        except OSError:
            return None
        stamp = datetime.datetime.now().strftime('%Y%m%d')
        _log_path[0] = os.path.join(LOG_DIR, f'ywp-{stamp}.log')
        log_line(f'=== ログ開始 pid={os.getpid()} full={LOG_FULL} ===')
    if tee and not isinstance(sys.stdout, _Tee):
        sys.stdout = _Tee(sys.stdout)
        sys.stderr = _Tee(sys.stderr)
        attach_stdlib_logging()
    return _log_path[0]


class _Tee:
    """print の出力を画面とログの両方に流す

    既存の print をログ用に書き換えて回らなくて済むように、出口で分岐させる。
    """

    def __init__(self, stream):
        self._stream = stream
        self._buf = ''

    def write(self, s):
        try:
            self._stream.write(s)
        except Exception:
            pass
        self._buf += s
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            if line.strip():
                log_line(line)
        return len(s)

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def isatty(self):
        return getattr(self._stream, 'isatty', lambda: False)()

    def __getattr__(self, name):
        return getattr(self._stream, name)


# 中身が巨大で読んでも意味のないキーは件数だけ残す
_BULKY = ('ywp_mst_game_const', 'userYoukaiResultList', 'enemyYoukaiResultList',
          'userYoukaiList', 'enemyYoukaiList', 'themeResultList')


def _brief(obj, depth=0):
    """ログ用に short く安全な表現にする（秘密は伏せ、長い値は打ち切る）"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(s in str(k).lower() for s in _SECRETISH):
                out[k] = '<伏せ字>'
            elif k in _BULKY:
                out[k] = f'<{len(v)}件>' if isinstance(v, (list, dict)) else '<略>'
            elif depth >= 2:
                out[k] = '<略>'
            else:
                out[k] = _brief(v, depth + 1)
        return out
    if isinstance(obj, list):
        return f'<{len(obj)}件>' if len(obj) > 3 else [_brief(v, depth + 1) for v in obj]
    if isinstance(obj, str) and len(obj) > LOG_VALUE_MAX:
        return obj[:LOG_VALUE_MAX] + f'…(全{len(obj)}字)'
    return obj


def _dump(label, obj):
    try:
        return f'    {label} ' + json.dumps(_brief(obj), ensure_ascii=False, default=str)
    except Exception:
        return f'    {label} <表現できず>'


def api_log(name, body, resp, http, ms, attempt=1, err=None):
    """APIの往復を1件記録する

    既定では1行の要約だけ。失敗したときだけ本文も残す（成功時まで残すと
    セーブ本体でログが膨れるため）。YWP_LOG_FULL=1 で常に本文も残す。
    """
    if not LOG_ENABLED:
        return
    code = resp.get('resultCode') if isinstance(resp, dict) else None
    uid = body.get('userId') if isinstance(body, dict) else None
    parts = [f'{name:<20}', f'rc={code}', f'http={http}', f'{ms:.0f}ms']
    if uid and uid != '0':
        parts.append(f'uid={uid}')
    if attempt > 1:
        parts.append(f'再送{attempt}回目')
    if err:
        parts.append(f'例外={err}')
    lines = [' '.join(parts)]
    if err or LOG_FULL or code not in (0, None):
        lines.append(_dump('req ', body))
        lines.append(_dump('resp', resp))
    log_line('\n'.join(lines))


_dumped = []

def _mask(k, v):
    return '<伏せ字>' if any(s in k.lower() for s in _SECRETISH) else v

def dump_start_payload(js):
    """gameStartの生レスポンスを1回だけ表示する（環境変数 YWP_DEBUG_START=1 で有効）

    dropItemFlg に何を入れるべきか・HPのキー名が何かは、実際のレスポンスを
    見ないと決められない。周回のついでに1件だけ覗くための仕掛け。
    """
    if os.environ.get('YWP_DEBUG_START') != '1' or _dumped:
        return
    _dumped.append(True)
    en = js.get('enemyYoukaiList') or []
    yk = js.get('userYoukaiList') or []
    print('\n' + '=' * 70)
    print('  gameStart 生データ（YWP_DEBUG_START=1）')
    print('=' * 70)
    print('■ トップレベルのキー:')
    print('   ', ', '.join(sorted(k for k in js if not k.startswith('_'))))
    print('■ 敵の抽選情報（ここの | の後ろがドロップ結果かを見たい）:')
    for i, e in enumerate(en):
        print(f'    [{i}] lotItemInfoList   = {e.get("lotItemInfoList")!r}')
        print(f'        lotYoukaiInfoList = {e.get("lotYoukaiInfoList")!r}')
    print('■ 敵1体ぶんの全フィールド（HPのキー名の確認用）:')
    for k, v in sorted((en[0] if en else {}).items()):
        print(f'    {k} = {_mask(k, v)!r}')
    print('■ 自分の妖怪1体ぶんの全フィールド（resultYoukaiHP の根拠を探す）:')
    for k, v in sorted((yk[0] if yk else {}).items()):
        print(f'    {k} = {_mask(k, v)!r}')
    print('=' * 70 + '\n', flush=True)

def server_elapsed(start, at=None):
    """サーバー側が計る gameStart→gameEnd の間隔の推定値（秒）

    サーバーが gameStart を処理したのは往復のどこかなので中点を起点とみなし、
    こちらが gameEnd を送ってから届くまでの片道分を足す。
    レスポンス受信時刻を起点にして送信時刻で測ると、往復1回ぶん短く申告する
    ことになり、サーバーが計った間隔と食い違う。
    起点が分からない場合は None（呼び出し側で実測にフォールバック）。
    """
    anchor = start.get('_clockAnchor') if isinstance(start, dict) else None
    if not anchor:
        return None
    half_trip = (start.get('_rtt') or 0.0) / 2.0
    return max((time.time() if at is None else at) + half_trip - anchor, 0.0)

# ============================================================================
# API通信関数
# ============================================================================

def post_nhn(name, obj, timeout=30, retry=3, refresh=None, timing=None, proxies=None):
    """API呼び出し（リトライ機能付き）

    refresh: 再送の直前に呼ばれ、送信内容を作り直す関数。時間依存の値
             （クリア秒など）が再送までの待ちで古くならないようにするため。
    timing:  渡すと実際に送受信した時刻を 'sent'/'recv' に書き戻す。
             呼び出しごとに別のdictを渡すこと（スレッドから同時に呼ばれる）。
    proxies: この呼び出しだけ使うプロキシ（requests の proxies dict）。
             None なら set_proxy()/config.json の全体設定に従う。

    APIの往復はすべてここを通るので、ログの記録もここでまとめて行う。
    """
    t0 = time.time()
    tm = {} if timing is None else timing
    state = {'attempt': 1, 'body': obj, 'err': None}
    http, js = _post_nhn(name, obj, timeout, retry, refresh, tm, state, proxies)
    ms = ((tm['recv'] - tm['sent']) * 1000 if tm.get('sent')
          else (time.time() - t0) * 1000)
    api_log(name, state['body'], js, http, ms, state['attempt'], state['err'])
    if (isinstance(js, dict) and js.get('resultCode') == 20 and isinstance(obj, dict)
            and obj.get('appVer') and 'バージョン' in (js.get('dialogMsg') or '')):
        # アプリの更新で古いバージョンになった。ストアの最新に切り替えて1回だけ送り直す
        new = update_appver(obj.get('deviceId'))
        if new and new != obj.get('appVer'):
            return post_nhn(name, dict(obj, appVer=new), timeout, retry, refresh,
                            timing, proxies)
    return http, js


# --- アプリのバージョン（appVer）の自動更新 -------------------------------------
# メンテでアプリが更新されると、古い appVer の通信は全部 rc=20「最新バージョンへ更新を」になる
# （2026-09-30: 4.174.0 → 4.175.0）。そのときは App Store の公開API と Google Play のページから
# 今のバージョンを取り、init.nhn で通ることを確かめてから切り替える（data/appver.json に残す）。
APPVER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'appver.json')
IOS_APP_ID = 1032346221          # App Store の「妖怪ウォッチ ぷにぷに」（bundleId com.Level5.YWP）
_appver_lock = threading.RLock()
_appver_checked = [0.0]


def _ver_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split('.'))
    except ValueError:
        return ()


def store_versions():
    """ストアに出ている今のバージョン（新しい順）。取れなければ空"""
    found = set()
    try:
        r = requests.get('https://itunes.apple.com/lookup', params={'id': IOS_APP_ID, 'country': 'jp'},
                         timeout=15)
        for a in r.json().get('results') or []:
            if a.get('version'):
                found.add(a['version'])
    except Exception:
        pass
    try:
        h = requests.get('https://play.google.com/store/apps/details',
                         params={'id': 'com.Level5.YWP', 'hl': 'ja', 'gl': 'JP'},
                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=15).text
        found.update(re.findall(r'\[\[\["(\d+\.\d+\.\d+)"\]\]', h)[:1])   # 「現在のバージョン」の欄
    except Exception:
        pass
    return sorted((v for v in found if _ver_tuple(v)), key=_ver_tuple, reverse=True)


def update_appver(device_id=None):
    """ストアの最新バージョンに切り替える（init.nhn が rc=20 にならないものだけ）。今の appVer を返す

    5分に1回まで（周回が同時に rc=20 を受けても、ストアに何度も聞かない）。
    """
    global APPVER
    with _appver_lock:
        if time.time() - _appver_checked[0] < 300:
            return APPVER
        _appver_checked[0] = time.time()
        cands = [v for v in store_versions() if _ver_tuple(v) > _ver_tuple(APPVER)]
        dev = device_id or new_udkey()
        for v in cands:
            _h, j = post_nhn('init.nhn', {
                'appGuardDeviceId': hashlib.sha256(dev.encode()).hexdigest(),
                'appVer': v, 'deviceId': dev, 'level5UserId': '0', 'mstVersionVer': 0,
                'osType': 2, 'signature': SIGNATURE, 'userId': '0', 'ywpToken': '0'})
            if isinstance(j, dict) and j.get('resultCode') not in (20, None):
                log_line(f'アプリのバージョンを自動で更新: {APPVER} → {v}（ストアの最新・init.nhn rc={j.get("resultCode")}）')
                APPVER = v
                try:
                    os.makedirs(os.path.dirname(APPVER_FILE), exist_ok=True)
                    with open(APPVER_FILE, 'w', encoding='utf-8') as f:
                        json.dump({'appVer': v, 'at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}, f)
                except OSError:
                    pass
                return APPVER
        log_line(f'⚠ アプリのバージョンを更新できなかった（今 {APPVER} / ストア {cands or "取れず"}）')
        return APPVER


# --- 今のイベント（ログインの応答の ywp_mst_event から） ----------------------
# generalIntParam2 が最初のマップ（例: 1309 百物語 → 29701、2026-10-01 からの 1317 ホロライブ
# 第8弾 → 29801）。周回のプリセットと最深部の面を決めるのに使う
EVENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'current_event.json')


def note_current_event(save, client=None):
    ev = save.get('ywp_mst_event') if isinstance(save, dict) else None
    # 最初のマップ（5桁の番号）を持っているイベントがマップのあるイベント。種別は回によって違う
    # （1309 は 10、1317 は 5）ので見ない。複数あれば一番新しいもの
    cands = [e for e in ev if isinstance(e, dict) and isinstance(e.get('generalIntParam2'), int)
             and 10000 <= e['generalIntParam2'] <= 99999] if isinstance(ev, list) else []
    main = max(cands, key=lambda e: e.get('eventId') or 0) if cands else None
    if not main:
        return
    cur = {'eventId': main.get('eventId'), 'name': main.get('description'),
           'firstMap': int(main['generalIntParam2']), 'openStage': main.get('generalIntParam3')}
    try:
        with open(EVENT_FILE, encoding='utf-8') as f:
            old = json.load(f)
        if all(old.get(k) == v for k, v in cur.items()):
            return
    except (OSError, ValueError):
        pass
    try:
        os.makedirs(os.path.dirname(EVENT_FILE), exist_ok=True)
        with open(EVENT_FILE, 'w', encoding='utf-8') as f:
            json.dump(dict(cur, at=datetime.datetime.now().strftime('%Y-%m-%d %H:%M')), f, ensure_ascii=False)
        log_line(f'今のイベント: {cur["eventId"]} {cur["name"]}（最初のマップ {cur["firstMap"]}）')
    except OSError:
        pass
    if client is not None:
        # イベントが変わった。ステージのマスタ（1日1回しか取り直さない）に新しい面が
        # まだ無いかもしれないので、すぐ取り直す
        try:
            client.master_table('ywp_mst_stage', max_age=0)
        except Exception as e:
            log_line(f'ステージのマスタを取り直せなかった: {str(e)[:120]}')


class VipUnavailable(RuntimeError):
    """この垢では今 まわし放題ガシャを引けない（closed=True は開催期間外）"""

    def __init__(self, msg, closed=False):
        super().__init__(msg)
        self.closed = closed


VIP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'vip_gacha.json')


def _vip_note(vip):
    """initGacha で見た まわし放題ガシャ（無ければ None）を data/vip_gacha.json に残す。
    ログインせずに開催中かを知るため（有料版の受付前の確認に使う）"""
    data = ({'gachaId': vip[0], 'name': vip[1], 'limit': vip[2], 'end': vip[3]} if vip
            else {'gachaId': None})
    data['checked'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        os.makedirs(os.path.dirname(VIP_FILE), exist_ok=True)
        with open(VIP_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except OSError as e:
        log_line(f'まわし放題ガシャの記録に失敗: {e}')


def vip_gacha_status():
    """最後に見た まわし放題ガシャが今も開催中なら {gachaId, name, limit, end, checked}、
    終わっている・見たことが無いなら None"""
    try:
        with open(VIP_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if not isinstance(data, dict) or not data.get('gachaId') or not now < str(data.get('end') or ''):
        return None
    return data


def current_event_stages():
    """今のイベントの面（隠しステージを除く・マップ順）と、最深部（種別 20）の面、イベント名

    data/current_event.json（ログインのたびに更新）の最初のマップから、同じ番号帯（例: 298xx）で
    それ以降のマップを data/ywp_mst_stage.txt から集める。読めなければ ([], None, None)。
    """
    try:
        with open(EVENT_FILE, encoding='utf-8') as f:
            ev = json.load(f)
        first = int(ev['firstMap'])
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'ywp_mst_stage.txt'),
                  encoding='utf-8') as f:
            table = rows(f.read())
    except (OSError, ValueError, KeyError, TypeError):
        return [], None, None
    mine = [r for r in table if len(r) > 3 and r[1].isdigit() and r[0].isdigit()
            and int(r[1]) // 100 == first // 100 and int(r[1]) >= first]
    ids = sorted(int(r[0]) for r in mine if r[3] in ('1', '20'))
    deepest = sorted(int(r[0]) for r in mine if r[3] == '20')
    return ids, (deepest[-1] if deepest else None), ev.get('name')


def _load_appver():
    """前に自動で切り替えたバージョンが、コードの値より新しければそれを使う"""
    global APPVER
    try:
        with open(APPVER_FILE, encoding='utf-8') as f:
            v = json.load(f).get('appVer')
        if _ver_tuple(v) > _ver_tuple(APPVER):
            APPVER = v
    except (OSError, ValueError, AttributeError):
        pass


_load_appver()


def _post_nhn(name, obj, timeout, retry, refresh, timing, state, proxies=None):
    """post_nhn の本体（記録は呼び出し側がまとめて行う）"""
    for attempt in range(1, retry + 1):
        state['attempt'] = attempt
        if refresh is not None and attempt > 1:
            obj = refresh(obj)
        state['body'] = obj
        try:
            sent = time.time()
            r = requests.post('%s/%s' % (GS, name), data=enc(jbody(obj)),
                              headers={**HDR, 'Host': 'gameserver.yw-p.com'},
                              proxies=_proxies() if proxies is None else proxies,
                              timeout=timeout)
            if timing is not None:
                timing['sent'], timing['recv'] = sent, time.time()

            # HTTPステータスコードチェック
            if r.status_code != 200:
                if attempt < retry:
                    time.sleep(2 ** attempt)
                    continue
                return r.status_code, {'resultCode': -1, '_error': f'HTTP{r.status_code}'}
            
            try:
                out = dec(r.text)
            except Exception as e:
                if attempt < retry:
                    time.sleep(2 ** attempt)  # 2秒, 4秒, 8秒で指数バックオフ
                    continue
                return r.status_code, {'resultCode': -1, '_raw': (r.text or '')[:200]}
            try:
                return r.status_code, json.loads(out)
            except Exception:
                if attempt < retry:
                    time.sleep(2 ** attempt)
                    continue
                return r.status_code, {'_raw': out[:300].decode('utf-8', 'replace')}
        except requests.exceptions.Timeout:
            state['err'] = 'Timeout'
            if attempt < retry:
                time.sleep(3 * attempt)
                continue
            return None, {'resultCode': -1, '_error': 'Timeout'}
        except requests.exceptions.ConnectionError:
            state['err'] = 'ConnectionError'
            if attempt < retry:
                time.sleep(3 * attempt)
                continue
            return None, {'resultCode': -1, '_error': 'ConnectionError'}

    return None, {'resultCode': -1, '_error': 'Max retries exceeded'}

def active(udkey=None, timeout=30, proxies=None):
    p = {'apkey': APKEY, 'device_cd': '%s_%s' % (MODEL, OSVER), 'device_type_cd': 'Android',
         'sign': 'true', 'version': APPVER}
    if udkey:
        p['udkey'] = udkey
    return requests.get('%s/api/v1/active/' % L5, params=p,
                        headers={'User-Agent': UA},
                        proxies=_proxies() if proxies is None else proxies,
                        timeout=timeout).json()

def new_udkey(proxy=None):
    return active(proxies=normalize_proxy(proxy) or None)['udkey']['value']

def create_gdkey(udkey, timeout=30, proxies=None):
    r = requests.get('%s/api/v1/create_gdkey' % L5,
                     params={'apkey': APKEY, 'udkey': udkey,
                             'device_cd': '%s_%s' % (MODEL, OSVER), 'device_type_cd': 'Android',
                             'version': APPVER, 'sign': 'true'},
                     headers={'User-Agent': UA},
                     proxies=_proxies() if proxies is None else proxies,
                     timeout=timeout).json()
    if not r.get('result'):
        raise RuntimeError('create_gdkey失敗: %s' % r)
    return r['gdkey']['value']

def _parse_forms(html):
    out = []
    for fm in re.finditer(r'<form\b[^>]*>(.*?)</form>', html, re.S | re.I):
        block = fm.group(0)
        am = re.search(r'action="([^"]*)"', block, re.I)
        inputs = {}
        for im in re.finditer(r'<input\b[^>]*>', block, re.I):
            nm = re.search(r'name="([^"]*)"', im.group(0), re.I)
            vm = re.search(r'value="([^"]*)"', im.group(0), re.I)
            if nm:
                inputs[nm.group(1)] = vm.group(1) if vm else ''
        out.append({'action': am.group(1) if am else '', 'inputs': inputs})
    return out

def link_email(udkey, email, pw, timeout=25, proxy=None):
    """udkeyにメール/パスを連携(L5 OAuth)。proxy 指定時はその出口だけを通す"""
    s = requests.Session()
    s.proxies.update(normalize_proxy(proxy) or _proxies())   # OAuthのWebフローも同じ出口を通す
    s.headers.update({'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
                      'Accept-Language': 'ja'})
    r = s.get('%s/api/v1/link_account' % L5, params={'apkey': APKEY, 'udkey': udkey},
              allow_redirects=True, timeout=timeout)
    lf = [x for x in _parse_forms(r.text) if any('email' in k.lower() for k in x['inputs'])]
    if not lf:
        raise RuntimeError('ログイン画面が出ない(既に連携済み or メール不正)')
    inp = dict(lf[0]['inputs'])
    inp['form[email]'] = email
    inp['form[password]'] = pw
    r = s.post(urljoin(r.url, lf[0]['action']), data=inp, allow_redirects=True, timeout=timeout)
    ap = [x for x in _parse_forms(r.text) if 'client_id' in x['inputs'] and x['inputs'].get('_method', '') != 'delete']
    if not ap:
        raise RuntimeError('consent画面が出ない(メール/パスが違う?)')
    inp = dict(ap[0]['inputs'])
    inp.setdefault('commit', 'Authorize')
    r2 = s.post(urljoin(r.url, ap[0]['action']), data=inp, allow_redirects=False, timeout=timeout)
    code = (parse_qs(urlparse(r2.headers.get('Location', '')).query).get('code') or [None])[0]
    if not code:
        raise RuntimeError('認可コードが取れない')
    fin = s.get('%s/api/v1/link_account' % L5,
                params={'code': code, 'apkey': APKEY, 'udkey': udkey,
                        'device_cd': '%s_%s' % (MODEL, OSVER), 'device_type_cd': 'Android'},
                timeout=timeout).json()
    if not fin.get('result'):
        raise RuntimeError('連携finalize失敗: %s' % fin)
    return fin

def rows(s):
    if isinstance(s, str):
        s = json.loads(s) if s.startswith('{') else s
    # セーブは行が'*'、列が'|'
    return [r.split('|') for r in (s or '').split('*') if r] if isinstance(s, str) else (s.get('rows') if isinstance(s, dict) else (s or []))

# ============================================================================
# Clientクラス
# ============================================================================

class Client:
    def __init__(self, udkey, proxy=None):
        self.udkey = udkey
        # このClientの通信だけが通るプロキシ（回し放題系統で使用）。
        # 未指定（{}）のときは post_nhn/active が全体設定（set_proxy/config.json）を使う
        self._px = normalize_proxy(proxy)
        self.gdkey = None
        self.userId = None
        self.token = '0'
        self.mst = 16897
        self.save = {}
        self._last_ok = 0.0    # 最後にgameEndが成功した時刻（連戦間隔の下限保証用）
        # 友だち申告が rc=4 で弾かれたステージ。以後そのステージでは申告しない（保険）
        self.befriend_off = set()
        # gameEnd が rc=4 で弾かれた回数（ステージごと）。続いたら周回側で止める
        self.rc4_count: dict = {}
        # 好物を使う・買い足す面か（stageId → None / 'new' / 'all'）。bot が周回ごとに決める
        # 'new' = まだ持っていない妖怪の敵だけ、'all' = 持っていても使う（必殺技Lv上げ）
        self.food_policy = lambda stageId: None
        # 好物の出来事（bot が gameEnd ごとに読んで数え、空にする）
        # ('fed', 敵ID, アイテムID, ランク) / ('bought', アイテムID, 個数, Yマネー)
        self.food_events: list = []

    def _active_with_gdkeys(self, retries=6):
        """gdkeyが返ってくるまでactive()を繰り返す"""
        a = active(self.udkey, proxies=self._px or None)
        for _ in range(retries):
            if a.get('gdkeys'):
                break
            time.sleep(1.2)
            a = active(self.udkey, proxies=self._px or None)
        if not a.get('gdkeys'):
            raise RuntimeError('gdkeyが無い(このudkeyに紐づくゲームデータが無い)')
        return a

    def _enum(self, a):
        """gdkeyからユーザー情報を列挙"""
        gds = a['gdkeys']
        pl = []
        for _ in range(5):
            rc, j = post_nhn('getGdkeyAccounts.nhn', {
                'appVer': APPVER, 'deviceId': self.udkey,
                'gdkeys': [{'gdkey': g['value']} for g in gds],
                'level5UserId': '0', 'mstVersionVer': self.mst, 'osType': 2,
                'userId': '0', 'ywpToken': '0'}, proxies=self._px or None)
            pl = j.get('udkeyPlayerList') or []
            if len(pl) >= len(gds):
                break
            time.sleep(1.2)
        # udkeyPlayerListは送った順で返らない。gdkey欄で突き合わせる
        by_g = {str(p.get('gdkey')): p for p in pl if p.get('gdkey')}
        out = []
        for i, g in enumerate(gds):
            p = by_g.get(g['value'], {})
            out.append({'idx': i, 'userId': p.get('userId'), 'playerName': p.get('playerName'),
                        'gdkey': g['value'], 'gdsig': g['signature']})
        return out

    def init_nhn(self):
        """init.nhnを呼び出してmstVersionを更新"""
        rc, j = post_nhn('init.nhn', {
            'appGuardDeviceId': hashlib.sha256(self.udkey.encode()).hexdigest(),
            'appVer': APPVER, 'deviceId': self.udkey, 'level5UserId': '0',
            'mstVersionVer': self.mst, 'osType': 2, 'signature': SIGNATURE,
            'userId': '0', 'ywpToken': '0'}, proxies=self._px or None)
        v = j.get('mstVersionMaster')
        if isinstance(v, int) and v > 0:
            self.mst = v
        return j

    def login(self, userId=None):
        """ゲームサーバーへログイン（正しい実装）"""
        # init.nhn → active → login という順序が重要
        self.init_nhn()
        a = self._active_with_gdkeys()   # 署名一式は同じactiveから揃える(混ぜるとrc=30)
        accs = self._enum(a)
        sel = None
        if userId is not None:
            sel = next((x for x in accs if str(x['userId']) == str(userId)), None)
            if sel is None:
                raise RuntimeError('userId %s が見つからない' % userId)
        if sel is None:
            # 未指定時は userId が採番済みのセーブを優先（未作成の空gdkeyを避ける）
            sel = next((x for x in accs if x.get('userId')), accs[0])
        rc, j = post_nhn('login.nhn', {
            'appVer': APPVER, 'batteryInfo': battery_info(), 'deviceId': self.udkey,
            'deviceName': MODEL, 'gdkeySignature': sel['gdsig'], 'gdkeyValue': sel['gdkey'],
            'isL5IDLinked': 1,
            'level5UserId': sel['gdkey'],     # gdkeyを入れる
            'modelName': MODEL, 'mstVersionVer': self.mst, 'osType': 2, 'osVersion': OSVER,
            'signNonce': a['sign_nonce'], 'signTimestamp': str(a['sign_timestamp']),
            'signature': SIGNATURE, 'udkeySignature': a['udkey']['signature'],
            'udkeyValue': self.udkey, 'userId': sel['userId'], 'ywpToken': '0'},
            proxies=self._px or None)
        if j.get('resultCode') != 0:
            code = j.get('resultCode')
            error_desc = RC.get(code, '不明なエラー')
            error_detail = j.get('_raw') or j.get('_error') or j.get('dialogMsg') or ''
            raise RuntimeError('login失敗: rc=%s (%s) %s' % (code, error_desc, error_detail[:100]))
        self.gdkey, self.userId, self.token = sel['gdkey'], sel['userId'], j.get('token')
        self.save = j
        note_current_event(j, self)
        return j

    def accounts(self):
        """複数アカウント情報"""
        self.init_nhn()
        return [{k: v for k, v in a.items() if k != 'gdsig'}
                for a in self._enum(self._active_with_gdkeys())]

    def call(self, name, extra=None, refresh=None, timing=None):
        """API呼び出し"""
        if not self.token or self.token == '0':
            raise RuntimeError('先に login() してください')
        body = {'activeDeckId': 1, 'appVer': APPVER, 'deviceId': self.udkey,
                'level5UserId': self.gdkey, 'mstVersionVer': self.mst, 'osType': 2,
                'token': self.token, 'userId': str(self.userId), 'ywpToken': '0'}
        if extra:
            body.update(extra)
        rc, j = post_nhn(name, body, refresh=refresh, timing=timing,
                         proxies=self._px or None)
        t = j.get('token')
        if t and t != 'null':
            self.token = t     # 持ち越さないと次が rc=32
        return rc, j

    def master(self, key):
        """マスターデータ取得。key は 'ywp_mst_xxx'（'|'区切りで複数可）

        引数名は tableNames（実機キャプチャで確認済み。旧来の key だと rc=3）。
        """
        rc, j = self.call('getMaster.nhn', {'tableNames': key})
        if j.get('resultCode') != 0:
            return {}
        return j

    # ひとだまショップのパック (goodsId, 価格Yマネー, 入手個数=本体+ボーナス)。
    # 2026-09 の ywp_mst_shop_hitodama_list 実測値。単価が最も良いのは50個パック。
    # (goodsId, 価格Yマネー, ひとだま個数) 単価の良い順
    HITODAMA_PACKS = ((1003, 6000, 50), (1002, 3000, 20), (1001, 1000, 5))

    def buy_hitodama(self, target=500, current=None):
        """ひとだまをYマネーで買い足し、合計が target 個に達するまで繰り返す

        buyHitodama.nhn はパック指定（リクエストは共通項 + goodsId のみ、実測済み）。

        パックの選び方は所持Yマネーで変える:
          HITODAMA_RICH 以上 … 単価の良い大きいパック（従来どおり）
          それ未満         … 一番安いパックだけ

        大きいパックは単価が良いが、6,000払うと序盤の垢は財布が空になり、
        次の周回ぶんも買えなくなる。余裕がないうちは1,000のパックで
        つなぎ、周回で貯まって HITODAMA_RICH を超えたら大きいパックに戻す。

        Yマネーが途中で足りなくなったら買えたぶんだけで戻る（部分補充）。
        1つも買えなかった場合だけ例外。
        戻り値: 買い足したひとだまの個数
        """
        if current is None:
            pts = self.points()
            current = (pts.get('hitodama') or 0) + (pts.get('freeHitodama') or 0)

        def _ymoney():
            try:
                return int(self.info().get('ymoney') or 0)
            except (TypeError, ValueError):
                return 0

        added = 0
        while True:
            # 失敗リクエストを増やさないよう、財布から買えるパックを先に選ぶ
            ym = _ymoney()
            if ym >= HITODAMA_RICH:
                packs, goal = self.HITODAMA_PACKS, target
            else:
                # 余裕なし。安いパックだけで、数戦ぶんだけ買ってつなぐ。
                # 残りは周回で貯めて HITODAMA_RICH を超えてから大きいのを買う
                packs = self.HITODAMA_PACKS[-1:]
                goal = min(target, current + HITODAMA_POOR_BUY)
            if current + added >= goal:
                break
            choice = next((p for p in packs if p[1] <= ym), None)
            if choice is None:
                if added:
                    break       # 部分的に買えた。周回は続け、次の節目でまた試す
                raise RuntimeError('buyHitodama失敗: Yマネー不足 (ymoney=%d)' % ym)
            goods, _price, pack = choice
            rc, j = self.call('buyHitodama.nhn', {'goodsId': goods})
            code = j.get('resultCode')
            if code != 0:
                if added:
                    break
                raise RuntimeError('buyHitodama失敗: rc=%s %s' % (
                    code, (j.get('dialogMsg') or j.get('_error') or '')[:120]))
            added += pack
            # 応答に最新のユーザーデータが付いていれば財布を差し替える
            if j.get('ywp_user_data') is not None:
                self.save['ywp_user_data'] = j['ywp_user_data']
            time.sleep(random.uniform(1.0, 2.0))    # 連続購入も人間っぽく間隔を空ける
        return added

    @staticmethod
    def _lead_hp(yk):
        """残HP報告値のフォールバック。本来は gameStart 応答の youkaiHP
        （デッキ合計HP）を使い、実機はそこから recvDamageTotal を引いて報告する。
        youkaiHP が読めないときだけ先頭妖怪のHPで代用する"""
        for key in ('hp', 'maxHp', 'hpMax', 'hitPoint'):
            v = (yk[0] if yk else {}).get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                return int(v)
        return 755

    def build_game_end(self, stageId, battleType, start, clearTime=None):
        """gameEnd構築

        clearTime: 実際に経過した秒数。報告値を実経過とズラさないために渡す。

        戦績（消去数・サイズ・コンボ・ダメージ配分）は毎回同じ値だとリクエストの
        指紋になるので、合計と内訳の整合を保ったままランダムに散らす。
        フラグ類とサーバーが決める値（ドロップ・イベントPt）は固定のまま。
        """
        reqId = start.get('requestId')
        yk = start.get('userYoukaiList') or []
        en = start.get('enemyYoukaiList') or []
        fed = start.get('_fed') or {}           # use_food で好物をあげた敵
        ehp = sum(e.get('hp', 0) for e in en)
        # 撃破に必要な敵HP合計の直上を少し上回るだけにする
        # （実機キャプチャ: 敵HP610に対し damageTotal=640）
        dmg = (ehp + random.randint(5, max(ehp // 12, 60))) if ehp else random.randint(48000, 63000)
        # 残HPの報告値。実機は gameStart 応答の youkaiHP（デッキ合計HP）から
        # recvDamageTotal を引いた値を gameEnd で送る（実機キャプチャで
        # 652-114=538 等が全件成立）。youkaiHP が読めないときだけ旧来の推定に落ちる
        # 実機キャプチャ18戦で youkaiHP は全件 0 だった（この応答には載らない）。
        # 正解は userYoukaiList の hp 合計で、18/18 で
        # resultYoukaiHP == 合計 - recvDamageTotal が成立する。
        deck_hp = sum(int(y.get('hp') or 0) for y in yk)
        if deck_hp <= 0:
            v = start.get('youkaiHP')
            deck_hp = int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) \
                and v > 0 else self._lead_hp(yk)
        # 被ダメージ。実機18戦のうち10戦が0、残りは編成HPの2〜18%を受けて
        # resultRecvAtkNum は1〜4だった。毎回「無傷・敵の攻撃0回」で通すと
        # 残HPが常に満タンになり、ここが固定パターンになる。
        # 実機の被ダメは編成HPの大小によらず 44〜116 の絶対値に収まっていた
        # （HP652で114、HP3882でも88）。割合で決めると大型編成で過大になる
        if deck_hp > 0 and random.random() < 0.45:
            recv_dmg = min(random.randint(40, 120), max(1, int(deck_hp * 0.2)))
            recv_atk = random.randint(1, 4)
        else:
            recv_dmg, recv_atk = 0, 0

        # clearTime不明時に29秒を名乗ると実間隔と食い違うので、下限側に寄せる
        clear_sec = (max(int(round(clearTime)), int(BATTLE_TIME_MIN))
                     if clearTime is not None else int(BATTLE_TIME_MIN))

        # 消去回数は戦闘時間に比例させる（実機の手動プレイは約1.6〜3.0回/秒）
        erase_total = max(4, int(round(clear_sec * random.uniform(1.6, 3.0))))
        # 1消去のサイズはぷにの「個数」。実機の eraseSizeAve は 1.0〜3.2 なので
        # 合計も消去数の2倍程度に収める（旧: 52〜78倍は実機にありえない値だった）
        erase_size_total = int(round(erase_total * random.uniform(1.6, 3.2)))
        # リンク（一度に繋げるぷに数）の最大は実機で 2〜12
        link_max = random.randint(2, 12)

        # 戦績は編成全体に配分する。1体だけが全部稼ぐ形は毎回続くと不自然なので
        # 先頭を主軸にしつつ、残りにも軽く割り振る。
        weights = ([random.uniform(0.6, 1.0)] + [random.uniform(0.05, 0.45) for _ in yk[1:]]
                   if yk else [])
        erase_nums = _share(erase_total, weights)
        erase_sizes = _share(erase_size_total,
                             [k * random.uniform(0.85, 1.15) for k in erase_nums])
        damages = _share(dmg, [float(s) for s in erase_sizes])

        users = []
        for i, y in enumerate(yk):
            k, s, d = erase_nums[i], erase_sizes[i], damages[i]
            users.append({
                # 実機の damageMax はその妖怪の damageTotal の半分近くまで届く
                # （193中89、179中155）。旧の倍率だと最大1回が小さすぎた
                'damageMax': _peak(d, max(k, 1), 4.0, 8.0),
                'damageTotal': d,
                'eraseNum': k,
                'eraseSize': s,
                # 1消去の最大サイズ（ぷに個数）。実機キャプチャは 2〜19 に収まる
                'eraseSizeMax': min(_peak(s, k, 2.5, 6.0, min_each=1), 19),
                'linkSizeMax': random.randint(1, link_max) if k else 0,
                'recoveryActual': 0,
                'recoveryMax': 0,
                'sSkillUseNum': 0,
                # 実機キャプチャ18戦・全妖怪で 0 だった（スキル発動は別枠らしい）
                'skillUseNum': 0,
                'youkaiId': y.get('youkaiId'),
            })
        if users:
            # 最大リンクの持ち主が1体もいないと全体値と食い違うので主軸に持たせる
            users[erase_nums.index(max(erase_nums))]['linkSizeMax'] = link_max

        # 撃破順もリスト順固定だと並びが毎回同じになるため入れ替える
        order = list(range(1, len(en) + 1))
        random.shuffle(order)
        enemies = []
        for i, e in enumerate(en):
            # サーバーが返した抽選結果をそのまま申告する。読めない形式なら
            # 従来どおりドロップなしで通す（外れを当たりと言うと弾かれるため）
            raw_item, raw_yk = e.get('lotItemInfoList'), e.get('lotYoukaiInfoList')
            _warn_lot('lotItemInfoList', raw_item)
            _warn_lot('lotYoukaiInfoList', raw_yk)
            if e.get('lotTreasureFlg') or e.get('lotTreasureInfoList'):
                # 宝箱の抽選は読んでいない（いつも「宝箱なし」で送る）。出たら記録して調べる
                log_line(f'宝箱の抽選あり stage={stageId} enemyId={e.get("enemyId")} '
                         f'lotTreasureFlg={e.get("lotTreasureFlg")!r} '
                         f'lotTreasure={e.get("lotTreasureInfoList")!r}')
            # lotItemInfoList は '|1'=当選のフラグ方式（2026-09-24 観測）。
            # 当選時の dropItemId には、敵データの dropItemId（サーバーが
            # gameStart時点で決めた落下品）をそのまま申告する
            m_item = 'off' if LOT_DROP_MODE == 'off' else 'flag'
            item_key, won_item = parse_lot_info(raw_item, m_item)
            # 妖怪が友だちになったかは、下でまとめて1体だけ決める（befriend_pick）
            yk_key, _ = parse_lot_info(raw_yk, 'off')
            yk_flg = 0
            item_flg = 1 if won_item is not None else 0
            item_id = (e.get('dropItemId') or 0) if item_flg else 0
            if item_flg:
                print(f'[ywp] アイテムドロップ当選を申告: itemId={item_id} '
                      f'(lotItem={raw_item!r})', flush=True)
            enemies.append({
                'deadEndOrder': order[i],
                'deadEndType': 0,
                'dropItemCheckKey': item_key,
                'dropItemFlg': item_flg,
                'dropItemId': item_id,
                'dropTreasureFlg': 0,
                'dropTreasureId': 0,
                'dropYoukaiCheckKey': yk_key,
                # dropYoukaiId は元の本文に無いフィールドなので足さない
                # （知らないキーを増やすと rc=3 で弾かれうる）
                'dropYoukaiFlg': yk_flg,
                'enemyId': e.get('enemyId'),
                'itemId': 0,
                'useItemLLarge': 0,
                'useItemLarge': 0,
                'useItemMiddle': 0,
                'useItemSmall': 0,
            })
            if e.get('enemyId') in fed:
                # gameUseItem で好物をあげた敵。あげた物とランクの欄を立てる
                enemies[-1]['itemId'] = fed[e['enemyId']][0]
                enemies[-1][FOOD_FIELDS[fed[e['enemyId']][1]]] = 1
        picked = (befriend_pick(en, enemies, fed) if stageId not in self.befriend_off
                  else None)
        if picked is not None:
            r, key = picked
            r['dropYoukaiFlg'] = 1
            r['dropYoukaiCheckKey'] = key
            print(f'[ywp] 妖怪が友だちになったと申告: enemyId={r["enemyId"]} '
                  f'key={key} stage={stageId}', flush=True)
        return {
            'stageId': stageId,
            'battleType': battleType,
            'requestId': str(reqId),
            'damageTotal': dmg,
            # 実機のスコアは damageTotal の約12.7〜16.2倍（640→9083 等）
            'score': min(int(dmg * random.uniform(12.5, 16.5)), 2000000000),
            'userYoukaiResultList': users,
            'enemyYoukaiResultList': enemies,
            'bonusBlockNum': 0,
            'cheatFlg': 0,
            'clearTimeLongSec': 0,
            'clearTimeSec': clear_sec,
            # コンボは実機の手動プレイで3〜14程度（フィーバー連鎖時を除く）
            'comboMax': max(1, min(erase_total, random.randint(3, 9))),
            'eraseNumTotal': erase_total,
            'eraseSizeAve': '%.2f' % (erase_size_total / erase_total),
            'eraseSizeMax': max([u['eraseSizeMax'] for u in users]
                                or [min(_peak(erase_size_total, erase_total, 2.5, 6.0,
                                             min_each=1), 19)]),
            'eventPoint': 0,
            'eventSubPoint': 0,
            'eventTeamPoint': 0,
            # 実機キャプチャ18戦では 0 か 1 しか出ていない
            'feverTimeNum': 1 if random.random() < 0.28 else 0,
            'linkSizeMax': link_max,
            'pauseAtkNum': 0,
            'recvDamageTotal': recv_dmg,
            'resultRecvAtkNum': recv_atk,
            'resultYoukaiHP': int(deck_hp) - recv_dmg,
            'scoreLog': '',
            'spMissionIntValue1': 0,
            'suspendFlg': 0,
            'themeResultList': [],
            'ywp_mst_game_const': GAME_CONST,
        }

    def stage_ranking(self, stageId):
        """userStageRanking.nhn — ステージ選択画面の通信

        実機は gameStart の直前に必ずこれを送る（実機キャプチャで18/18）。
        リクエストは共通項 + stageId のみ。実機と同じ画面遷移を再現するため
        battle_start から毎回呼ぶ。
        """
        return self.call('userStageRanking.nhn', {'stageId': stageId})

    def battle_type_of(self, stageId):
        """ステージIDからbattleTypeを推定"""
        return 6 if str(stageId).startswith(('28805', '28904', '29008', '29304')) else 1

    def battle_start(self, stageId, battleType=None):
        """gameStartだけ実行。戻り値 (rc, js, battleType)

        後で server_elapsed() がクリア秒を測れるよう、送受信時刻を js に残す。
        実機と同じ画面遷移を再現するため、gameStart の直前には必ずステージ
        選択画面の userStageRanking を送る（詳細は stage_ranking のdocstring）。
        """
        if battleType is None:
            battleType = self.battle_type_of(stageId)
        # gameEnd間隔の下限保証（MIN_BATTLE_CYCLE のコメント参照）。
        # 2026-09-24 の実測でサーバーは短い連戦を弾かないことが分かったので、
        # 普段はこの待ちが発動することはない
        gap = MIN_BATTLE_CYCLE - (time.time() - self._last_ok) - 10.0
        if gap > 0:
            time.sleep(gap + random.uniform(0.5, 8.0))
        try:
            self.stage_ranking(stageId)
        except Exception:
            # ここだけで落として周回を止めない。本体の成否は gameStart で判断
            pass
        t = {}
        rc, js = self.call('gameStart.nhn', {'stageId': stageId, 'battleType': battleType,
                                             'battleCode': '', 'retryFlg': 0}, timing=t)
        if isinstance(js, dict):
            # 送受信時刻が取れなければ受信時刻を起点にする（往復ぶん短めになるが、
            # 起点なしにすると clearTime 不明扱いになって実間隔と大きくズレる）
            if t.get('sent'):
                js['_clockAnchor'] = (t['sent'] + t['recv']) / 2.0
                js['_rtt'] = t['recv'] - t['sent']
            else:
                js['_clockAnchor'], js['_rtt'] = time.time(), 0.0
            dump_start_payload(js)
        return rc, js, battleType

    def battle_end(self, stageId, battleType, start, clearTime=None):
        """gameEndだけ実行

        clearTime 省略時は送信の直前に gameStart からの経過を測り直す。
        呼び出し側で先に計算して渡すと、スレッド切り替えや本文の組み立てに
        かかった時間のぶんだけ短く申告することになるため、ここで測る。
        好物を使う面（food_policy）では、あげれば仲間になる敵にだけ先に好物をあげる。
        """
        if self.maybe_feed(stageId, start):
            time.sleep(random.uniform(2.0, 4.0))    # あげてから倒すまで
        # 前回の gameEnd から MIN_BATTLE_CYCLE 秒たつまで送らない（rc=3 → BAN を防ぐ）
        gap = MIN_BATTLE_CYCLE - (time.time() - self._last_ok)
        if self._last_ok and gap > 0:
            time.sleep(gap + random.uniform(0.2, 1.0))
            clearTime = None        # 待ったぶん、申告するクリア秒も測り直す
        if clearTime is None:
            clearTime = server_elapsed(start)
        ge = self.build_game_end(stageId, battleType, start, clearTime=clearTime)

        def refresh(body):
            # 再送までに空いた時間ぶん、申告クリア秒が実間隔とズレるので測り直す
            sec = server_elapsed(start)
            if sec:
                body['clearTimeSec'] = max(int(round(sec)), int(BATTLE_TIME_MIN))
            return body

        rc, j = self.call('gameEnd.nhn', ge, refresh=refresh)
        code = j.get('resultCode') if isinstance(j, dict) else None
        declared = any(e.get('dropYoukaiFlg') for e in ge['enemyYoukaiResultList'])
        if code == 0:
            self._last_ok = time.time()
            trust_add(self.userId)          # 育ち具合（TRUST_BATTLES）に数える
            # 進行フラグは gameEnd の応答に全体が載ってくる。手元も最新にしておく
            for key in ('ywp_user_tutorial_list', 'ywp_user_menufunc'):
                if j.get(key) is not None:
                    self.save[key] = j[key]
            self.merge_youkai_diff(j)
            if j.get('ywp_user_item') is not None:
                self.save['ywp_user_item'] = j['ywp_user_item']
            try:
                self.advance_opening()
            except Exception as e:
                log_line(f'オープニングの進行に失敗（周回は続ける）: {str(e)[:120]}')
            try:
                mode = self.food_policy(stageId)
                if mode and stageId not in self.befriend_off:
                    self.stock_food(start, owned_ok=mode == 'all')
            except Exception as e:
                log_line(f'好物の買い足しに失敗（周回は続ける）: {str(e)[:150]}')
        elif code == 4:
            # 弾かれた。これまでの例は全部「まだ育っていない垢で、デッキの攻撃力に対して速すぎる
            # クリア」（min_clear_time 参照）。1回でも出すと BAN されるので、周回側ですぐ止める
            n = self.rc4_count[stageId] = self.rc4_count.get(stageId, 0) + 1
            log_line(f'⚠ gameEnd rc=4 stage={stageId}（{n}回目・申告 {ge.get("clearTimeSec")}秒・'
                     f'友だち申告{"あり" if declared else "なし"}）')
            # 原因を後から調べられるよう、その1戦の敵の抽選（仲間・アイテム・宝箱）を全部残す
            for e in start.get('enemyYoukaiList') or []:
                log_line(f'  rc=4 の敵 {e.get("enemyId")} hp={e.get("hp")} '
                         f'lotYoukai={e.get("lotYoukaiInfoList")!r} lotItem={e.get("lotItemInfoList")!r} '
                         f'lotTreasureFlg={e.get("lotTreasureFlg")!r} '
                         f'lotTreasure={e.get("lotTreasureInfoList")!r} dropItemId={e.get("dropItemId")}')
            if declared and n >= 2:
                self.befriend_off.add(stageId)
        return rc, j

    def stage_type(self, stageId):
        """ステージ種別（ywp_mst_stage の4列目）。1=通常、20=29704006 などの特別な面"""
        index = Client._MASTER_MEM.get('_stage_type')
        table = self.master_table('ywp_mst_stage')
        if not index or index[0] is not table:
            index = (table, {r[0]: r[3] for r in table if len(r) > 3})
            Client._MASTER_MEM['_stage_type'] = index
        return index[1].get(str(stageId))

    def min_clear_time(self, stageId, start):
        """このバトルで申告してよい最短のクリア秒

        確かめたこと（2026-09-27〜30）だけで決める:
          ・rc=4 は、まだ育っていない垢で、1秒あたりのダメージがデッキの攻撃力の約0.35倍を
            超えたときに出る（作りたての垢9つ。1回でも出すと BAN）。0.3倍以下で回した周回は
            一度も弾かれていない（DPS_RATIO_MAX）
          ・安全な速さで勝ってきた垢は、あとで速くしても通る（30まん 約130勝後・ぷに5749）。
            何勝で解禁されるかは未確認なので、多めの TRUST_BATTLES 勝で「育った垢」とする
          ・種別 20 の面（29704006）は速さを見られていない
        育った垢と種別 20 の面は BATTLE_TIME_MIN。それ以外は
        敵のHP合計（≒申告するダメージ）÷（デッキ攻撃力 × DPS_RATIO_MAX）を下回らない。
        面の種別が読めないときは、安全側（通常の面）として扱う。
        """
        try:
            if self.stage_type(stageId) not in (None, '1'):
                return BATTLE_TIME_MIN
        except Exception:
            pass
        if is_trusted(self.userId):
            return BATTLE_TIME_MIN
        atk = sum(int(y.get('atkPower') or 0) for y in (start or {}).get('userYoukaiList') or [])
        hp = sum(int(e.get('hp') or 0) for e in (start or {}).get('enemyYoukaiList') or [])
        if atk <= 0 or hp <= 0:
            return BATTLE_TIME_MIN
        return max(BATTLE_TIME_MIN, hp * 1.05 / (atk * DPS_RATIO_MAX))

    def current_deck(self, start=None):
        """今のデッキ（妖怪IDの並び。先頭がリーダー）と、各妖怪のHP

        gameStart の userYoukaiList はデッキと同じ並び（2026-09-27 実機で確認）で、
        アプリ側で編成を変えていてもサーバーの実際の並びが取れるのでそちらを優先する。
        """
        yk = (start or {}).get('userYoukaiList') or []
        if yk:
            return ([str(y.get('youkaiId')) for y in yk],
                    {str(y.get('youkaiId')): int(y.get('hp') or 0) for y in yk})
        deck = next((r for r in rows(self.save.get('ywp_user_youkai_deck'))
                     if r and r[0] == '1'), None)
        return [y for y in (deck or [])[1:6] if y not in ('', '0')], {}

    def deck_edit(self, ids):
        """デッキ1を ids の並びにする（deckEdit.nhn）。戻り値は resultCode"""
        rc, j = self.call('deckEdit.nhn',
                          {'youkaiIdList': [{'youkaiId': int(y)} for y in ids]})
        for key in ('ywp_user_youkai_deck', 'ywp_user_tutorial_list'):
            if j.get(key) is not None:
                self.save[key] = j[key]
        if j.get('resultCode') == 0 and j.get('ywp_user_youkai_deck') is None:
            # 応答にデッキが無くても手元は新しい並びにしておく（行: デッキ番号|5体|残りの列）
            decks = rows(self.save.get('ywp_user_youkai_deck'))
            for r in decks:
                if r and r[0] == '1':
                    r[1:6] = [str(y) for y in ids] + ['0'] * (5 - len(ids))
            if decks:
                self.save['ywp_user_youkai_deck'] = '*'.join('|'.join(r) for r in decks)
        return j.get('resultCode')

    def make_leader(self, youkai_id, start=None):
        """仲間になった妖怪をリーダーにする（自動編成）

        すでにデッキにいれば何もしない（並びも変えない）。
        いなければ先頭（リーダー）に入れる。元のリーダーは残し、5体を超えるぶんは
        リーダー以外で HP が一番低い1体を外す。
        戻り値: ('already', None) / (resultCode, 外した妖怪ID or None)
        """
        yid = str(youkai_id)
        deck, hp = self.current_deck(start)
        if yid in deck:
            return 'already', None
        new, out = [yid] + deck, None
        if len(new) > 5:
            rest = new[1:]
            out = min(rest, key=lambda y: hp.get(y, 0))
            rest.remove(out)
            new = [yid] + rest
        time.sleep(random.uniform(2.0, 5.0))    # 実機も結果画面→編成画面の間がある
        return self.deck_edit(new), out

    def merge_youkai_diff(self, result):
        """gameEnd 等の応答の ywp_user_youkai_diff（変わった妖怪の行）を手元のセーブに重ねる

        セーブはログイン時に一度来るきりなので、重ねないとレベル・攻撃力が古いままになる。
        行の並び: 妖怪ID|レベル|経験値|HP|攻撃力|…（2026-09-28 実機の gameStart と照合）
        """
        diff = result.get('ywp_user_youkai_diff') if isinstance(result, dict) else None
        if not diff or not isinstance(self.save.get('ywp_user_youkai'), str):
            return
        by_id = {r[0]: r for r in rows(self.save['ywp_user_youkai']) if r}
        for r in rows(diff):
            if r:
                by_id[r[0]] = r
        self.save['ywp_user_youkai'] = '*'.join('|'.join(r) for r in by_id.values())

    def youkai_attack(self):
        """持っている妖怪の {妖怪ID: 攻撃力}（ywp_user_youkai の5列目）"""
        out = {}
        for r in rows(self.save.get('ywp_user_youkai')):
            if len(r) > 4:
                try:
                    out[r[0]] = int(r[4])
                except ValueError:
                    pass
        return out

    def strongest_deck(self, leader=None):
        """攻撃力の高い順に5体。leader を渡すとその妖怪をリーダーに固定して残り4体を強い順"""
        atk = self.youkai_attack()
        order = sorted(atk, key=lambda y: -atk[y])
        if leader:
            return [str(leader)] + [y for y in order if y != str(leader)][:4]
        return order[:5]

    def sort_deck(self, leader=None):
        """デッキを強い順（strongest_deck）に組み直す

        今の並びと同じなら通信しない。rc=4 はデッキの攻撃力に対して速すぎるクリアで
        出るので（DPS_RATIO_MAX）、強いデッキほど同じ面を安全に速く回せる。
        戻り値: ('same', 今の並び, 今の並び) / (resultCode, 前の並び, 新しい並び)
        """
        old, _ = self.current_deck()
        new = self.strongest_deck(leader)
        if not new or new == old:
            return 'same', old, old
        time.sleep(random.uniform(2.0, 5.0))    # 実機も編成画面を開いてから並べ替える
        return self.deck_edit(new), old, new

    # --- 好物 -------------------------------------------------------------------

    def food_ranks(self):
        """食べ物の {アイテムID: ランク}（ywp_mst_item の2列目が 2 の行。5列目がランク1〜4）"""
        hit = Client._MASTER_MEM.get('_food_rank')
        table = self.master_table('ywp_mst_item')
        if not hit or hit[0] is not table:
            ranks = {}
            for r in table:
                if len(r) > 4 and r[1] == '2' and r[4] in ('1', '2', '3', '4'):
                    ranks[r[0]] = int(r[4])
            hit = (table, ranks)
            Client._MASTER_MEM['_food_rank'] = hit
        return hit[1]

    def shop_items(self, max_age=24 * 3600):
        """ショップの品ぞろえ {アイテムID: 行}。行は goodsId / price(Yマネー) / lockConditionFlg など

        ywp_mst_shop_item_list は tableData でなく {'data': [...]} で返る（2026-09-28 実測）。
        lockConditionFlg=1 の品は ywp_user_shop_item_unlock に載ってはじめて買える。
        """
        hit = Client._MASTER_MEM.get('_shop_items')
        if hit and time.time() - hit[0] < max_age:
            return hit[1]
        path = os.path.join(self.MASTER_DIR, 'ywp_mst_shop_item_list.json')
        data = None
        try:
            if time.time() - os.path.getmtime(path) < max_age:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
        except (OSError, ValueError):
            data = None
        if data is None:
            rc, j = self.call('getMaster.nhn', {'tableNames': 'ywp_mst_shop_item_list'})
            v = j.get('ywp_mst_shop_item_list')
            data = v.get('data') if isinstance(v, dict) else None
            if not isinstance(data, list):
                raise RuntimeError(f'ショップの品ぞろえを取れませんでした（rc={j.get("resultCode")}）')
            try:
                os.makedirs(self.MASTER_DIR, exist_ok=True)
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False)
            except OSError:
                pass
        out = {str(d.get('itemId')): d for d in data if isinstance(d, dict)}
        Client._MASTER_MEM['_shop_items'] = (time.time(), out)
        return out

    def buyable_foods(self):
        """今ショップで買える食べ物 {アイテムID: (goodsId, 値段)}"""
        ranks, shop = self.food_ranks(), self.shop_items()
        unlocked = {r[0] for r in rows(self.save.get('ywp_user_shop_item_unlock')) if r}
        return {iid: (int(d.get('goodsId') or iid), int(d.get('price') or 0))
                for iid, d in shop.items()
                if iid in ranks and (not d.get('lockConditionFlg') or iid in unlocked)}

    def enemy_youkai(self, enemy):
        """敵が仲間になったときの妖怪ID（ywp_mst_youkai_enemy_param の 敵ID→妖怪ID）

        2026-09-28: 敵 100101301 に好物をあげて仲間になった妖怪 2277000 がこの表と一致。
        29704006 のボス 29704061 は 9002931（ＨＯＧ 白雪姫）。
        """
        if enemy.get('replaceYoukaiId'):
            return str(enemy['replaceYoukaiId'])
        table = self.master_table('ywp_mst_youkai_enemy_param')
        index = Client._MASTER_MEM.get('_enemy_youkai')
        if not index or index[0] is not table:
            index = (table, {r[0]: r[1] for r in table if len(r) > 1})
            Client._MASTER_MEM['_enemy_youkai'] = index
        return index[1].get(str(enemy.get('enemyId')))

    def wants_friend(self, enemy):
        """まだ持っていない妖怪の敵か。持っていれば好物は要らない。分からなければ True"""
        try:
            yid = self.enemy_youkai(enemy)
        except Exception:
            return True
        return not yid or yid not in {r[0] for r in rows(self.save.get('ywp_user_youkai')) if r}

    def food_plan(self, start, owned_ok=False):
        """好物をあげれば仲間になる敵と、あげる物を選ぶ。(敵, アイテムID, ランク) か None

        すでに持っている妖怪の敵には、あげない（wants_friend）。owned_ok なら持っていてもあげる
        （重ねて仲間にすると必殺技Lvが上がる。9002931 が 3→4 になっていた。2026-09-28）。
        餌なしで仲間になる敵がいれば、あげない（1戦で仲間になるのは1体）。
        あげるのは持っている好物だけ（gameStart の enableFoodInfoList で itemFlg=1 のもの）。
        当たりになるランクのうち一番低い（安い）ものを使う。当たらないなら何もあげない。
        """
        en = start.get('enemyYoukaiList') or []
        lots = {e.get('enemyId'): befriend_lot(e.get('lotYoukaiInfoList')) for e in en}
        if any(v and v[1][0] == '1' for v in lots.values()):
            return None
        ranks, owned = self.food_ranks(), self.items()
        best = None
        for e in en:
            lot = lots.get(e.get('enemyId'))
            if not lot or e.get('invalidFoodFlg') or not (owned_ok or self.wants_friend(e)):
                continue
            for fi in e.get('enableFoodInfoList') or []:
                iid = str(fi.get('itemId'))
                rank = ranks.get(iid)
                if (not rank or fi.get('itemFlg') != 1 or owned.get(int(iid), 0) <= 0
                        or lot[1][rank] != '1'):
                    continue
                if best is None or rank < best[2]:
                    best = (e, int(iid), rank)
        return best

    def maybe_feed(self, stageId, start):
        """好物を使う面（food_policy）で、あげれば仲間になる敵がいれば好物をあげる

        1戦につき1回だけ判定する。周回側はバトルの待ち時間の中で先に呼んでおく
        （battle_end でまとめてやると、あげる回だけ1戦が長くなる）。あげたら True。
        """
        if start.get('_food_checked') or stageId in self.befriend_off:
            return False
        start['_food_checked'] = True
        try:
            mode = self.food_policy(stageId)
            if mode:
                plan = self.food_plan(start, owned_ok=mode == 'all')
                return bool(plan) and self.use_food(start, plan)
        except Exception as e:
            log_line(f'好物をあげられなかった（バトルは続ける）: {str(e)[:150]}')
        return False

    def use_food(self, start, plan):
        """バトル中に好物をあげる（gameUseItem.nhn）。あげられたら True

        リクエストは敵ID・アイテムID・gameStart の requestId（2026-09-28 実機）。
        あげた記録は start['_fed'] に残し、gameEnd の組み立て（build_game_end）で使う。
        """
        e, iid, rank = plan
        time.sleep(random.uniform(2.0, 5.0))    # 実機もバトルが始まってから好物を選ぶ
        rc, j = self.call('gameUseItem.nhn', {'enemyId': e.get('enemyId'), 'itemId': iid,
                                              'requestId': str(start.get('requestId'))})
        if j.get('resultCode') != 0:
            log_line(f'好物をあげられなかった enemyId={e.get("enemyId")} itemId={iid} '
                     f'rc={j.get("resultCode")}')
            return False
        if j.get('ywp_user_item') is not None:
            self.save['ywp_user_item'] = j['ywp_user_item']
        start['_fed'] = {e.get('enemyId'): (iid, rank)}
        self.food_events.append(('fed', e.get('enemyId'), iid, rank))
        log_line(f'好物をあげた enemyId={e.get("enemyId")} itemId={iid}（ランク{rank}） '
                 f'lot={e.get("lotYoukaiInfoList")}')
        return True

    def stock_food(self, start, cnt=FOOD_BUY_CNT, owned_ok=False):
        """この面の敵の好物を、ショップで買えるランクごとに切らさないようにする

        当たりの桁はランクごとにばらばら（2026-09-28 ゲスト垢: '01000' はランク1だけ当たり）
        なので、一番高いランクだけでなく買えるランクを全部そろえる。高いランクから順に、
        持っていない品を cnt 個買う（buyItem.nhn: goodsId・cnt。2026-09-28 実機）。
        ひとだま代を残すため、買ったあとに HITODAMA_RICH を割るなら買わない。
        戻り値: 買った [(アイテムID, 個数, Yマネー)]
        """
        ranks, shop, owned = self.food_ranks(), self.buyable_foods(), self.items()
        bought = []
        want = []
        for e in start.get('enemyYoukaiList') or []:
            if e.get('invalidFoodFlg') or not (owned_ok or self.wants_friend(e)):
                continue        # 持っている妖怪の好物は買い置きしない（owned_ok のときは買う）
            for fi in e.get('enableFoodInfoList') or []:
                iid = str(fi.get('itemId'))
                if iid in shop and iid in ranks and iid not in [w[1] for w in want]:
                    want.append((ranks[iid], iid))
        for rank, iid in sorted(want, reverse=True):
            if owned.get(int(iid), 0) > 0:
                continue
            goods, price = shop[iid]
            try:
                ymoney = int(self.info().get('ymoney') or 0)
            except (TypeError, ValueError):
                ymoney = 0
            if ymoney - price * cnt < HITODAMA_RICH:
                continue
            time.sleep(random.uniform(1.5, 3.5))
            rc, j = self.call('buyItem.nhn', {'cnt': cnt, 'goodsId': goods})
            if j.get('resultCode') != 0:
                log_line(f'好物を買えなかった itemId={iid} rc={j.get("resultCode")} '
                         f'{(j.get("dialogMsg") or "")[:80]}')
                continue
            # 応答に持ち物全部と財布が載る（2026-09-28 実機）
            for key in ('ywp_user_item', 'ywp_user_data'):
                if j.get(key) is not None:
                    self.save[key] = j[key]
            owned = self.items()
            bought.append((int(iid), cnt, price * cnt))
            self.food_events.append(('bought', int(iid), cnt, price * cnt))
            log_line(f'好物を買った itemId={iid}（ランク{rank}）×{cnt} Yマネー{price * cnt}')
        return bought

    # --- ガチャ -----------------------------------------------------------------

    def coin_gachas(self, table, now=None):
        """コインで引くガチャ {コインのアイテムID: ガチャID}（ywp_mst_gacha の行から）

        行: ガチャID|開始|終了|種別|支払うアイテムID|個数|…|12列目=1回で引く数。
        種別 2 がアイテム（コイン）払い。例: 7001 は 1つ星コイン(25010) 1枚で1回。
        """
        now = now or datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # 払い物にはチケット類も混じるので、名前に「コイン」が付くアイテムだけにする
        coins = {r[0] for r in self.master_table('ywp_mst_item') if len(r) > 2 and 'コイン' in r[2]}
        out = {}
        for r in rows(table):
            if (len(r) > 11 and r[3] == '2' and r[4] in coins and r[5] == '1'
                    and r[11] == '1' and r[1] <= now < r[2]):
                out.setdefault(r[4], r[0])
        return out

    def pull_coin_gachas(self, max_pulls=30):
        """持っているコインを全部使ってガチャを引く（initGacha → executeGacha を1回ずつ）

        リクエストは 2026-09-28 実機と同じ: gachaId・gachaCnt=1・候補なし。
        新しい妖怪は ywp_user_youkai_diff で返るので手元のセーブに重ねる（sort_deck 用）。
        戻り値: [(コインID, ガチャID, [手に入れた妖怪ID…])]
        """
        got = []
        for coin, gid, n, _ in self.coin_list():
            for ids in self.pull_coin(coin, gid, min(n, max_pulls - len(got))):
                got.append((coin, gid, ids))
            if len(got) >= max_pulls:
                break
        return got

    def coin_list(self):
        """ガチャ画面を開いて（initGacha）、持っているコインを返す

        戻り値: [(コインID, ガチャID, 枚数, コインの名前)]
        """
        rc, j = self.call('initGacha.nhn')
        table = j.get('ywp_mst_gacha')
        if j.get('resultCode') != 0 or not table:
            raise RuntimeError(f'ガチャ画面を開けませんでした（rc={j.get("resultCode")}）')
        owned = self.items()
        names = {r[0]: r[2] for r in self.master_table('ywp_mst_item') if len(r) > 2}
        return [(int(coin), int(gid), owned[int(coin)], names.get(coin, coin))
                for coin, gid in self.coin_gachas(table).items() if owned.get(int(coin), 0) > 0]

    def pull_coin(self, coin, gacha_id, count):
        """coin で gacha_id を count 回、1回ずつ引く（executeGacha）。戻り値: 1回ごとの [妖怪ID…]

        途中で失敗したら、そこまでの分を返す。新しい妖怪は手元のセーブに重ねる（sort_deck 用）。
        """
        got = []
        for _ in range(max(0, int(count))):
            time.sleep(random.uniform(4.0, 8.0))    # 実機はガチャ演出を見てから次を引く
            rc, j = self.call('executeGacha.nhn', {'candidateYoukaiIdList': [], 'gachaCnt': 1,
                                                   'gachaId': int(gacha_id), 'requestYoukaiId': 0})
            if j.get('resultCode') != 0:
                log_line(f'ガチャを引けなかった gachaId={gacha_id} coin={coin} '
                         f'rc={j.get("resultCode")} {(j.get("dialogMsg") or "")[:80]}')
                break
            for key in ('ywp_user_item', 'ywp_user_data'):
                if j.get(key) is not None:
                    self.save[key] = j[key]
            self.merge_youkai_diff(j)
            ids = [p['youkai'].get('youkaiId') for p in j.get('gachaPrizeList') or []
                   if isinstance(p, dict) and isinstance(p.get('youkai'), dict)]
            got.append(ids)
            log_line(f'ガチャ gachaId={gacha_id}（コイン{coin}）→ 妖怪 {ids}')
        return got

    # まわし放題ガシャ（2026-10-01 実機: 50014「まわし放題ＶＩＰガシャ」10/1 01:00〜10/16 23:59）
    VIP_GACHA_WAIT = (20.0, 25.0)   # スピン開始→確定。実機は約23秒（10秒まわして連数を見てから はい）

    @staticmethod
    def vip_gachas(table, now=None):
        """今まわせる まわし放題ガシャ [(ガチャID, 名前, 上限連数, 終了日時)]（ywp_mst_gacha の行から）

        行: ガチャID|開始|終了|種別=5|…|12列目=上限連数(240)|…|15列目=名前。
        """
        now = now or datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return [(int(r[0]), r[14], int(r[11]), r[2]) for r in rows(table)
                if len(r) > 14 and r[3] == '5' and r[11].isdigit() and r[1] <= now < r[2]]

    def vip_gacha_check(self):
        """ガチャ画面を開いて（initGacha）、この垢で今 まわし放題ガシャを引けるか確かめる

        引ける間は gachaLotRuleList に {gachaId: 50014, remainCnt: 0} が載り、引いた後は消える。
        消えた垢に executeVipGacha を送ると rc=4（2026-10-01 引き済みの捨て垢で確認）なので、
        載っていなければ送らない。戻り値: (ガチャID, 名前, 上限連数)。引けなければ VipUnavailable
        """
        rc, j = self.call('initGacha.nhn')
        if j.get('resultCode') != 0:
            raise RuntimeError(f'ガチャ画面を開けませんでした（rc={j.get("resultCode")}）')
        vips = self.vip_gachas(j.get('ywp_mst_gacha'))
        _vip_note(vips[0] if vips else None)
        if not vips:
            raise VipUnavailable('今まわせる まわし放題ガシャがありません（開催期間外）', closed=True)
        gid, name, limit, _ = vips[0]
        listed = {r.get('gachaId') for r in j.get('gachaLotRuleList') or [] if isinstance(r, dict)}
        if gid not in listed:
            raise VipUnavailable(f'この垢は {name} を引き済みです（1垢1回まで）')
        return gid, name, limit

    def pull_vip_gacha(self, count=None):
        """まわし放題ガシャを count 連ぶん受け取る（initGacha → executeVipGacha → recvVipGacha）

        executeVipGacha（{gachaId}）がスピン開始で、応答に上限 limitTurnCnt が載る。連数は
        アプリが recvVipGacha の gachaCnt で送る値で、上限を超えると rc=4 で弾かれる
        （2026-10-01 捨て垢: 300 → rc=4・景品0。続けて 240 → rc=0・240体。弾かれた回は
        権利を消費しなかった）。なので count は上限で切る。None なら上限まで。
        戻り値: (ガチャID, 名前, 送った連数, [妖怪ID…])。引けない垢・期間外は VipUnavailable
        """
        gid, name, limit = self.vip_gacha_check()
        time.sleep(random.uniform(4.0, 8.0))
        rc, j = self.call('executeVipGacha.nhn', {'gachaId': gid})
        if j.get('resultCode') != 0:
            raise RuntimeError(f'{name} を始められませんでした（rc={j.get("resultCode")} '
                               f'{(j.get("dialogMsg") or "")[:60]}）')
        limit = int((j.get('specialEventInfo') or {}).get('limitTurnCnt') or limit)
        cnt = limit if count is None else max(1, min(int(count), limit))
        time.sleep(random.uniform(*self.VIP_GACHA_WAIT))
        rc, j = self.call('recvVipGacha.nhn', {'gachaCnt': cnt, 'gachaId': gid})
        if j.get('resultCode') != 0:
            raise RuntimeError(f'{name} {cnt}連を受け取れませんでした（rc={j.get("resultCode")} '
                               f'{(j.get("dialogMsg") or "")[:60]}）')
        for key in ('ywp_user_item', 'ywp_user_data'):
            if j.get(key) is not None:
                self.save[key] = j[key]
        self.merge_youkai_diff(j)
        ids = [p['youkai'].get('youkaiId') for p in j.get('gachaPrizeList') or []
               if isinstance(p, dict) and isinstance(p.get('youkai'), dict)]
        log_line(f'{name}（gachaId={gid}）{cnt}連 → 妖怪{len(ids)}体')
        return gid, name, cnt, ids

    def _tutorial(self, key):
        return {'|'.join(r[:-1]): r[-1]
                for r in rows(self.save.get('ywp_user_tutorial_list')) if len(r) >= 2}.get(key)

    # --- マスタの保存（1日ごとに取り直す） -------------------------------------

    MASTER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

    _MASTER_MEM: dict = {}          # name -> (読んだ時刻, 行)。3MB級のマスタを毎回読まない

    def master_table(self, name, max_age=24 * 3600):
        """getMaster.nhn で ywp_mst_<name> を取って data/ に保存し、行（'|'区切りのリスト）で返す"""
        hit = Client._MASTER_MEM.get(name)
        if hit and time.time() - hit[0] < max_age:
            return hit[1]
        path = os.path.join(self.MASTER_DIR, f'{name}.txt')
        try:
            if time.time() - os.path.getmtime(path) < max_age:
                with open(path, 'r', encoding='utf-8') as f:
                    got = rows(f.read())
                Client._MASTER_MEM[name] = (os.path.getmtime(path), got)
                return got
        except OSError:
            pass
        rc, j = self.call('getMaster.nhn', {'tableNames': name})
        v = j.get(name)
        data = v.get('tableData') if isinstance(v, dict) else v
        if not isinstance(data, str) or not data:
            raise RuntimeError(f'{name} を取れませんでした（rc={j.get("resultCode")}）')
        try:
            os.makedirs(self.MASTER_DIR, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(data)
        except OSError:
            pass
        got = rows(data)
        Client._MASTER_MEM[name] = (time.time(), got)
        return got

    # --- ミッション報酬 -----------------------------------------------------

    def receive_missions(self, max_rounds=8):
        """ミッション報酬を全部受け取る。受け取ったもの（recieveList を足したもの）を返す

        実機（2026-09-28 キャプチャ）: ミッション画面を開くと getMission.nhn（引数なし）、
        「まとめて受け取る」で missionRewardAll.nhn（引数なし）。1回で受け取りきれない
        ことがあり、実機は何度か押していた。受け取るものが無くなるまで繰り返す。
        """
        got = []
        self.call('getMission.nhn')
        for _ in range(max_rounds):
            time.sleep(random.uniform(2.5, 5.0))
            rc, j = self.call('missionRewardAll.nhn')
            if j.get('ywp_user_data') is not None:
                self.save['ywp_user_data'] = j['ywp_user_data']
            items = j.get('recieveList') or []
            if j.get('resultCode') != 0 or not items:
                break
            got.extend(items)
        return got

    # --- レベル上限の解放 ---------------------------------------------------

    def receive_presents(self):
        """郵便をまとめて受け取る（アプリの「全部受け取る」と同じ）。受け取った郵便の一覧を返す

        2026-09-30 実機: getPresentBox → receivePresentBox {"seqs": "番号|番号|…"}。アプリは
        一覧の番号を全部（CM の 0 も）送り、サーバーはまとめて受け取れるもの（canReceiveAll=1）
        だけを受け取って receiveSeqs で返す。まとめて受け取れるものが無ければ送らない。
        """
        rc, j = self.call('getPresentBox.nhn')
        lst = [p for p in j.get('ywp_user_present_box_list') or [] if isinstance(p, dict)]
        if not any(p.get('canReceiveAll') for p in lst):
            return []
        time.sleep(random.uniform(2.0, 5.0))       # 実機も一覧を開いてからボタンを押す
        rc, j = self.call('receivePresentBox.nhn', {'seqs': '|'.join(str(p.get('seq')) for p in lst)})
        if j.get('resultCode') != 0:
            log_line(f'郵便を受け取れなかった rc={j.get("resultCode")} {(j.get("dialogMsg") or "")[:80]}')
            return []
        for key in ('ywp_user_item', 'ywp_user_data'):
            if j.get(key) is not None:
                self.save[key] = j[key]
        got = {s for s in str(j.get('receiveSeqs') or '').split('|') if s}
        return [p for p in lst if str(p.get('seq')) in got]

    def youkai_type(self, youkai_id):
        """妖怪の「種類」（ywp_mst_youkai の4列目）。レベル上限を解放する値段を引くのに使う"""
        table = self.master_table('ywp_mst_youkai')
        index = Client._MASTER_MEM.get('_youkai_type')
        if not index or index[0] is not table:
            index = (table, {r[0]: r[3] for r in table if len(r) > 3})
            Client._MASTER_MEM['_youkai_type'] = index
        return index[1].get(str(youkai_id))

    def youkai_rank(self, youkai_id):
        """妖怪のランク番号（ywp_mst_youkai の4列目。RANK_NAMES 参照）。分からなければ None"""
        v = self.youkai_type(youkai_id)
        return int(v) if v and v.isdigit() else None

    def level_open_cost(self, youkai_id, level):
        """Lv{level} の上限を解放する値段 (Yマネー, Yポイント)。分からなければ None

        ywp_mst_youkai_level_open = 上限レベル|種類|Yマネー|Yポイント|…
        （2026-09-28 実機: Lv50・種類11 は Yマネー0/Yポイント500 で dispCost=500、
          試験垢: Lv10・種類1/2 は Yマネー10 で dispCost=10 → どちらも rc=0）
        """
        ty = self.youkai_type(youkai_id)
        if ty is None:
            return None
        for r in self.master_table('ywp_mst_youkai_level_open'):
            if len(r) >= 4 and r[0] == str(level) and r[1] == ty:
                return int(r[2]), int(r[3])
        return None

    def open_level(self, youkai_id, level, max_level=50, keep=0):
        """上限で止まっている妖怪を Yマネーで解放する（levelLockOff.nhn）

        戻り値: (結果, 払ったYマネー)。結果は 'ok' / 'skip:理由' / 'rc=N'
        Yマネーで払えるものだけ解放する（Yポイントが要る段は解放しない）。
        keep: 払ったあとに残すYマネー（ひとだま代）。割るなら 'skip:ひとだま優先'。
        """
        if level >= max_level:
            return 'skip:上限', 0
        cost = self.level_open_cost(youkai_id, level)
        if cost is None:
            return 'skip:値段不明', 0
        ymoney_cost, ypoint_cost = cost
        if ymoney_cost <= 0:
            return 'skip:Yマネーで払えない段', 0
        try:
            have = int(self.info().get('ymoney') or 0)
        except (TypeError, ValueError):
            have = 0
        if have < ymoney_cost:
            return 'skip:Yマネー不足', 0
        if have - ymoney_cost < keep:
            return 'skip:ひとだま優先', 0
        time.sleep(random.uniform(2.0, 5.0))       # 実機も結果画面→解放の確認を挟む
        rc, j = self.call('levelLockOff.nhn', {'dispCost': ymoney_cost,
                                               'youkaiId': int(youkai_id)})
        for key in ('ywp_user_data', 'ywp_user_youkai'):
            if j.get(key) is not None:
                self.save[key] = j[key]
        code = j.get('resultCode')
        return ('ok', ymoney_cost) if code == 0 else (f'rc={code}', 0)

    def advance_opening(self):
        """オープニング（tutorial 1|1000）を実機と同じ手順で進める

        実機: gameEnd で 3→4→5→6 と進み、6 で編成チュートリアル（deckEdit.nhn）→7、
        次の gameEnd で 8、そこでクライアントが (1,1000,1000) を送って完了する。
        bot は編成をしないので 6 で止まり、その垢をアプリで開くと編成チュートリアルの
        次（5面へ誘導）で操作が効かなくなる（2026-09-27 LDPlayer で確認）。
        途中の段階で完了値を送るとサーバーが進行を整合し直してフラグが落ちるので、
        必ず 8 のときだけ送る。
        """
        step = self._tutorial('1|1000')
        if step == '6':
            ids, _ = self.current_deck()
            if not ids:
                return
            time.sleep(random.uniform(3.0, 7.0))      # 実機はマップに戻ってから編成する
            code = self.deck_edit(ids)
            log_line(f'オープニング: 編成チュートリアル deckEdit rc={code} '
                     f'→ 1|1000={self._tutorial("1|1000")}')
        elif step == '8':
            time.sleep(random.uniform(3.0, 7.0))
            code = self.update_tutorial(1, 1000, 1000)
            log_line(f'オープニング: 完了 (1,1000,1000) rc={code} '
                     f'→ 1|1000={self._tutorial("1|1000")}')

    def battle(self, stageId, battleType=None, wait=None):
        """バトル実行

        wait: gameStart→gameEnd の間隔(秒)。未指定なら BATTLE_TIME_MIN 以上でランダム。
              指定しても BATTLE_TIME_MIN より短くはならない。
        """
        wait = (jitter(BATTLE_TIME_MIN, BATTLE_TIME_MIN, spread=BATTLE_TIME_SPREAD)
                if wait is None else max(float(wait), BATTLE_TIME_MIN))
        rc, js, battleType = self.battle_start(stageId, battleType)
        if js.get('resultCode') != 0:
            return js.get('resultCode'), js
        # 申告する最短のクリア秒（min_clear_time。今は一律 BATTLE_TIME_MIN）
        need = self.min_clear_time(stageId, js)
        if need > wait:
            wait = need * random.uniform(1.0, 1.12)
        time.sleep(wait)
        # サーバー計測が下限を割らないよう、往復ぶんを含めて足りなければ詰める
        shortfall = need - (server_elapsed(js) or wait)
        if shortfall > 0:
            time.sleep(shortfall)
        return self.battle_end(stageId, battleType, js)

    def info(self):
        """ユーザー情報"""
        d = self.save.get('ywp_user_data')
        if isinstance(d, str):
            d = json.loads(d)
        return d or {}

    # 所持数の在りか: (キー名, 表示名, 取得元)
    # 取得元 'data' は ywp_user_data のキー、整数は ywp_user_item のアイテムID。
    # Yポイントだけ user_data に無く、アイテムとして持っている。
    # 名前で拾う方式だと eventPointUpItemId のような通貨でないものまで引っかかる
    # ので、確認済みのものだけを並べる。
    WALLET = (
        ('ymoney', 'Yマネー', 'data'),
        ('chargeYmoney', '有償Yマネー', 'data'),
        ('ypoint', 'Yポイント', 80102),
        ('hitodama', 'ひとだま', 'data'),
        ('freeHitodama', '無料ひとだま', 'data'),
        ('medalPoint', 'メダルP', 'data'),
    )
    WALLET_MAIN = ('ymoney', 'ypoint', 'hitodama')

    def points(self):
        """所持数。{キー名: 数量}"""
        info, items = self.raw_info(), self.raw_items()
        # 「テーブルが無い（未ログイン等）」と「0個で行が無い」は区別する
        has_items = (getattr(self, 'save', None) or {}).get('ywp_user_item') is not None
        out = {}
        for name, _, src in self.WALLET:
            if isinstance(src, int):
                # アイテム側は所持0だと行ごと存在しないので、無ければ0とみなす
                v = items.get(src, 0) if has_items else None
            else:
                v = info.get(name)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[name] = int(v)
        return out

    def wallet_text(self, keys=None):
        """所持数を1行にまとめる（表示用）。既定はYマネー・Yポイント・ひとだま"""
        got, want = self.points(), self.WALLET_MAIN if keys is None else keys
        return ' / '.join(f'{label} {got[name]:,}' for name, label, _ in self.WALLET
                          if name in want and name in got)

    def raw_info(self):
        """ywp_user_data の全キーと値（通貨キーの特定用）"""
        try:
            return dict(sorted(self.info().items()))
        except (ValueError, AttributeError, TypeError):
            return {}

    def youkai(self):
        """妖怪一覧"""
        return [{'id': int(r[0]), 'raw': r} for r in rows(self.save.get('ywp_user_youkai'))]

    def items(self):
        """アイテム一覧"""
        return {int(r[0]): int(r[1]) for r in rows(self.save.get('ywp_user_item')) if len(r) >= 2}

    # 進行状況が入っていそうなテーブル。変化を追うと「クリアしたのに解放されない」
    # のようなケースで、サーバー側の進行が動いているのかを切り分けられる。
    PROGRESS_TABLES = (
        'ywp_user_stage', 'ywp_user_map', 'ywp_user_menufunc',
        'ywp_user_tutorial_list', 'ywp_user_stage_relation_progress',
        'ywp_user_stage_rank', 'ywp_user_event', 'ywp_user_event_condition',
        'ywp_user_dictionary', 'ywp_user_youkai', 'ywp_user_item',
        'ywp_user_treasure', 'ywp_user_hist_total', 'ywp_user_menufunc',
    )

    def save_shape(self):
        """セーブ各テーブルの「行数」を取る（差分を見るための指紋）

        中身を丸ごと持つと重いので、行数と要素数だけ控える。
        """
        shape = {}
        for k in self.PROGRESS_TABLES:
            v = (self.save or {}).get(k)
            if v is None:
                continue
            try:
                shape[k] = len(rows(v))
            except Exception:
                shape[k] = len(v) if hasattr(v, '__len__') else -1
        info = self.raw_info()
        for k in ('nowStageId', 'ymoney', 'hitodama'):
            if k in info:
                shape[f'data.{k}'] = info[k]
        return shape

    @staticmethod
    def shape_diff(before, after):
        """save_shape() 同士の差分。{キー: (前, 後)}"""
        keys = set(before) | set(after)
        return {k: (before.get(k), after.get(k))
                for k in sorted(keys) if before.get(k) != after.get(k)}

    # 行数が変わらないまま中身だけ動くテーブル。ywp_user_menufunc は解放しても
    # 29行のままで値が 0→1 になるだけなので、行数の比較では変化を拾えない。
    DETAIL_TABLES = (
        'ywp_user_menufunc', 'ywp_user_tutorial_list', 'ywp_user_shop_item_unlock',
        'ywp_user_stage_relation_progress', 'ywp_user_event_tutorial',
    )

    def progress_detail(self):
        """進行テーブルを {テーブル: {行キー: 値}} に展開する

        行キーは最終列を除いた部分（menufunc なら 'ID'、
        tutorial_list なら '種別|ID'）、値は最終列。
        """
        out = {}
        for name in self.DETAIL_TABLES:
            v = (self.save or {}).get(name)
            if v is None:
                continue
            try:
                out[name] = {'|'.join(r[:-1]): r[-1] for r in rows(v) if len(r) >= 2}
            except Exception:
                pass
        return out

    @staticmethod
    def detail_diff(before, after):
        """progress_detail() 同士の差分。{テーブル: {行キー: (前, 後)}}"""
        changed = {}
        for name in sorted(set(before) | set(after)):
            a, b = before.get(name) or {}, after.get(name) or {}
            d = {k: (a.get(k), b.get(k))
                 for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}
            if d:
                changed[name] = d
        return changed

    @staticmethod
    def detail_diff_text(changed, limit=12):
        """detail_diff() をログ1行ぶんの文字列に"""
        parts = []
        for name, d in changed.items():
            items = list(d.items())[:limit]
            body = ', '.join(f'{k}: {a}→{b}' for k, (a, b) in items)
            if len(d) > limit:
                body += f' ほか{len(d) - limit}件'
            parts.append(f'{name}[{body}]')
        return ' / '.join(parts)

    # 実機キャプチャ（ywp_analysis/decrypted_session.json）で観測した
    # updateTutorialFlg.nhn の並び。(tutorialType, tutorialId, tutorialStatus)
    # ywp_user_tutorial_list の「種別|ID|状態」にそのまま対応する。
    # type=2 の ID は ywp_user_menufunc の ID と一致しているが、これは「メニューの
    # 案内を見た」という申告でしかない。メニュー自体はサーバーが 4/6/8/12面で
    # 妖怪が友だちになったときに開く（befriend_pick 参照。2026-09-27 に検証）。
    # まとめて送ると途中の 1|1000 を完了値で上書きしてフラグが落ちるので使わない。
    # 特定エリアに紐づく type=3/5/9 は、そのエリアに到達していない垢に送ると
    # 意味が変わりうるので既定から外してある（AREA_TUTORIALS 参照）。
    STARTER_TUTORIALS = (
        (99, 3000, 1),
        (1, 1000, 1000),
        (2, 4, 2),          # menufunc 4 の解放
        (2, 2000, 1),
        (2, 2005, 1),
        (99, 3003, 1),
        (2, 3, 2),          # menufunc 3 の解放
        (11, 3023, 1),
    )
    AREA_TUTORIALS = (
        (3, 29401, 1),
        (5, 29401001, 1),
        (9, 29401001, 1),
    )

    def update_tutorial(self, tutorial_type, tutorial_id, status):
        """チュートリアル進行を1件進める。戻り値は resultCode"""
        rc, j = self.call('updateTutorialFlg.nhn', {
            'tutorialType': tutorial_type,
            'tutorialId': tutorial_id,
            'tutorialStatus': status})
        code = j.get('resultCode')
        # 応答に最新の進行状況が載ってくるので手元のセーブも更新しておく
        for key in ('ywp_user_tutorial_list', 'ywp_user_data'):
            if j.get(key) is not None:
                self.save[key] = j[key]
        return code

    def complete_starter_tutorials(self, steps=None):
        """作りたてセーブのチュートリアルをまとめて進める

        戻り値 (成功件数, [(type, id, status, rc), ...])
        1件失敗しても残りは続ける。呼び出し側で結果を見て判断する。
        """
        out = []
        for ttype, tid, status in (self.STARTER_TUTORIALS if steps is None else steps):
            try:
                code = self.update_tutorial(ttype, tid, status)
            except Exception as e:
                code = f'例外:{str(e)[:60]}'
            out.append((ttype, tid, status, code))
            time.sleep(random.uniform(0.4, 1.1))   # 実機同様に間隔を空ける
        return sum(1 for x in out if x[3] == 0), out

    def refresh(self):
        """セーブを取り直す（所持数の更新用）

        セーブはログイン時に一度返ってくるきりなので、ログインし直すしかない。
        1回で3リクエスト走るため、周回のたびに呼ぶような使い方はしない。
        """
        return self.login(self.userId)

    def raw_items(self):
        """アイテム一覧。壊れたセーブでも例外を投げない版（表示用）"""
        try:
            return self.items()
        except (ValueError, AttributeError, TypeError):
            return {}

    def stages(self):
        """ステージ一覧"""
        return {int(r[0]): r for r in rows(self.save.get('ywp_user_stage'))}

# ============================================================================
# ログイン関数
# ============================================================================

# --- UDkeyのアカウントごとの使い回し（BAN対策） ---
# 実機は1台の端末に1つのUDkeyがあり、メール連携は一度きり。毎ログインで
# new_udkey() すると「同一アカウントが短期間に別端末から繰り返しログイン」
# という異常な指紋になる（テストアカウントが実際にBANされた主因）。
# udkeys.json に email→udkey を保存し、2回目以降は同じUDkeyでログインする。
UDKEY_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'udkeys.json')


def _load_udkeys():
    try:
        with open(UDKEY_STORE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_udkey_for(email, udkey):
    """email→udkey を保存（アカウント作成直後の呼び出しにも使う）"""
    d = _load_udkeys()
    if d.get(email) == udkey:
        return
    d[email] = udkey
    try:
        with open(UDKEY_STORE, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def udkey_for(email, proxy=None):
    """(udkey, 新規発行か) を返す。保存済みならそれを、無ければ発行して保存"""
    udkey = _load_udkeys().get(email)
    if udkey:
        return udkey, False
    udkey = new_udkey(proxy=proxy)
    save_udkey_for(email, udkey)
    return udkey, True


def login_email(email, pw, userId=None, proxy=None):
    """メール/パスでログイン。proxy 指定時はこのログインの通信だけがその出口を通る

    UDkeyはアカウントごとに保存して使い回す（udkey_for のコメント参照）。
    保存済みのUDkeyならメール連携はスキップする（連携は一度きりのはずなので）。
    """
    udkey, fresh = udkey_for(email, proxy=proxy)
    print('  UDkey: %s%s' % (udkey[:16] + '...', '' if fresh else '（保存済み）'))

    if fresh:
        print('  メール連携中...')
        try:
            link_email(udkey, email, pw, proxy=proxy)
            print('  メール連携成功')
        except Exception as e:
            print(f'  ⚠ メール連携エラー: {str(e)[:100]}')
            raise
        print('  サーバー反映待機中... 3秒')
        time.sleep(3)

    print('  ゲームサーバーログイン中...')
    try:
        c = Client(udkey, proxy=proxy)
        c.login(userId=userId)
    except RuntimeError as e:
        if fresh or 'gdkey' not in str(e):
            raise
        # 保存済みUDkeyでゲームデータが見えなくなった（珍しい）。
        # 新規発行して連携し直し、以後はそれを使う
        print('  ⚠ 保存済みUDkeyが無効なので再発行します')
        udkey = new_udkey(proxy=proxy)
        save_udkey_for(email, udkey)
        link_email(udkey, email, pw, proxy=proxy)
        print('  サーバー反映待機中... 3秒')
        time.sleep(3)
        c = Client(udkey, proxy=proxy)
        c.login(userId=userId)
    return c

def login_email_prepared(email, pw, proxy=None):
    """メール/パスで udkey発行〜セーブ一覧列挙まで（login()はまだ呼ばない）。

    proxy 指定時はこのログインの通信だけがその出口を通る
    戻り値は (Client, アカウント一覧)。一覧は accounts() と同じ形
    （idx / userId / playerName / gdkey）。セーブが1つならそのまま c.login() で
    よく、複数ならユーザーに選ばせた userId で c.login(userId) を呼ぶ。
    bot.py のセーブデータ選択UIのための分割版。
    UDkeyはアカウントごとに保存して使い回す（udkey_for のコメント参照）。
    """
    udkey, fresh = udkey_for(email, proxy=proxy)
    print('  UDkey: %s%s' % (udkey[:16] + '...', '' if fresh else '（保存済み）'))
    if fresh:
        print('  メール連携中...')
        link_email(udkey, email, pw, proxy=proxy)
        print('  メール連携成功')
        print('  サーバー反映待機中... 3秒')
        time.sleep(3)
    c = Client(udkey, proxy=proxy)
    print('  セーブデータ確認中...')
    try:
        accs = c.accounts()
    except RuntimeError as e:
        if fresh or 'gdkey' not in str(e):
            raise
        print('  ⚠ 保存済みUDkeyが無効なので再発行します')
        udkey = new_udkey(proxy=proxy)
        save_udkey_for(email, udkey)
        link_email(udkey, email, pw, proxy=proxy)
        print('  サーバー反映待機中... 3秒')
        time.sleep(3)
        c = Client(udkey, proxy=proxy)
        accs = c.accounts()
    if len(accs) > 1:
        print('  セーブデータ %d件: %s' % (len(accs), ', '.join(
            '%s(userId:%s)' % (a.get('playerName'), a.get('userId'))
            for a in accs)))
    return c, accs

def create_save(player_name='ぷに', icon_id=1, sex_type=1, proxy=None):
    """新規セーブデータ（ゲーム内ユーザー）を1から作る。戻り値 (Client, userId)

    proxy 指定時はこの一連の通信だけがその出口を通る（回し放題垢作成で使用）
    フロー: new_udkey → create_gdkey → init.nhn → createUser.nhn → login
    createUser.nhn の引数（iconId / playerName / sexType）は 2026-09 の
    実機キャプチャで確認したもの。sexType には数値1を文字化した '\\x01'
    （chr(1)）を送る（実機がそう送っている）。
    ログイン済みのClientを返すので、このままバトル等が行える。
    """
    print('  新規セーブ作成中...')
    px = normalize_proxy(proxy) or None
    udkey = new_udkey(proxy=proxy)
    gdkey = create_gdkey(udkey, proxies=px)
    c = Client(udkey, proxy=proxy)
    c.init_nhn()
    rc, j = post_nhn('createUser.nhn', {
        'appVer': APPVER, 'deviceId': udkey, 'iconId': icon_id,
        'level5UserId': gdkey, 'mstVersionVer': c.mst, 'osType': 2,
        'playerName': player_name, 'sexType': chr(sex_type),
        'userId': '0', 'ywpToken': '0'}, retry=2, proxies=px)
    if j.get('resultCode') != 0:
        raise RuntimeError('createUser失敗: rc=%s %s' % (
            j.get('resultCode'),
            (j.get('dialogMsg') or j.get('_error') or '')[:100]))
    d = j.get('ywp_user_data')
    d = json.loads(d) if isinstance(d, str) else (d or {})
    uid = d.get('userId')
    if not uid:
        raise RuntimeError('createUser応答にuserIdが無い: %s' % sorted(j.keys()))
    print('  セーブ作成OK userId=%s (%s)' % (uid, player_name))
    c.login(uid)
    return c, uid

# ============================================================================
# 対話型メイン処理
# ============================================================================

def input_with_default(prompt, default):
    """デフォルト値付き入力"""
    if default:
        result = input(f'{prompt} [{default}]: ').strip()
        return result if result else default
    else:
        while True:
            result = input(f'{prompt}: ').strip()
            if result:
                return result
            print('入力してください')

def input_int(prompt, default=None):
    """整数入力"""
    while True:
        try:
            if default:
                result = input(f'{prompt} [{default}]: ').strip()
                return int(result) if result else int(default)
            else:
                result = input(f'{prompt}: ').strip()
                return int(result)
        except ValueError:
            print('⚠ 正の整数を入力してください')

def input_float(prompt, default=None):
    """小数入力"""
    while True:
        try:
            if default:
                result = input(f'{prompt} [{default}]: ').strip()
                return float(result) if result else float(default)
            else:
                result = input(f'{prompt}: ').strip()
                return float(result)
        except ValueError:
            print('⚠ 数値を入力してください')

def confirm(prompt):
    """確認入力"""
    while True:
        result = input(f'{prompt} (y/n): ').strip().lower()
        if result in ['y', 'yes', 'はい']:
            return True
        elif result in ['n', 'no', 'いいえ']:
            return False
        print('⚠ y または n で答えてください')

def main():
    path = setup_log(tee=True)
    print('=' * 70)
    print('  ぷにぷに 対話型自動周回スクリプト')
    print('=' * 70)
    if path:
        print(f'  ログ: {path}（止めるには YWP_LOG=0）')
    print()

    # 入力取得
    print('【設定入力】')
    print('-' * 70)
    email = input_with_default('メールアドレス', None)
    pw = input_with_default('パスワード', '123qwe')
    stage_id = input_int('ステージID', None)
    count = input_int('周回回数', '10')
    request_delay = input_float('リクエスト前待機時間（秒）', '0.5')
    battle_time = input_float(f'バトル時間（秒・最低{BATTLE_TIME_MIN}）', str(BATTLE_TIME_MIN))
    cooldown = input_float(f'クールダウン時間（秒・最低{COOLDOWN_MIN}）', '3.0')

    # 下限を下回る入力は下限まで引き上げる
    if battle_time < BATTLE_TIME_MIN:
        print(f'  ⚠ バトル時間は最低{BATTLE_TIME_MIN}秒です → {BATTLE_TIME_MIN}秒に補正しました')
        battle_time = BATTLE_TIME_MIN
    if cooldown < COOLDOWN_MIN:
        print(f'  ⚠ クールダウンは最低{COOLDOWN_MIN}秒です → {COOLDOWN_MIN}秒に補正しました')
        cooldown = COOLDOWN_MIN

    print()
    print('【設定確認】')
    print('-' * 70)
    print(f'  メール: {email}')
    print(f'  ステージID: {stage_id}')
    print(f'  周回回数: {count}回')
    print(f'  リクエスト前待機: {request_delay}秒 (±30%でランダム変動)')
    print(f'  バトル時間: {battle_time}〜{battle_time * (1 + BATTLE_TIME_SPREAD):.1f}秒')
    print(f'  クールダウン: {cooldown}〜{cooldown * (1 + TIME_SPREAD):.1f}秒')
    print(f'  ※連戦間隔は約{MIN_BATTLE_CYCLE:.0f}秒以上になります')
    print()

    if not confirm('この設定で開始しますか？'):
        print('キャンセルしました')
        return 1

    print()
    print('【1/2】ログイン処理')
    print('-' * 70)

    # ログイン
    try:
        print('ログイン中...')
        c = login_email(email, pw)
        info = c.info()
        print(f'✓ ログイン成功')
        print(f'  プレイヤー名: {info.get("playerName")}')
        print(f'  ユーザーID: {c.userId}')
        print(f'  UDkey: {c.udkey}')
        pts = c.points()
        if pts:
            print('  所持: ' + ' / '.join(f'{k}={v:,}' for k, v in pts.items()))
        else:
            print('  所持: 通貨らしきキーなし（c.raw_info() で中身を確認）')
        print()
    except RuntimeError as e:
        error_msg = str(e)
        if 'login失敗: rc=-1' in error_msg or 'rc=-1' in error_msg:
            print(f'✗ ログイン失敗: ネットワークエラーまたはサーバー不具合 (rc=-1)')
            print(f'  対応策:')
            print(f'    1. メール/パスワードが正しいか確認してください')
            print(f'    2. インターネット接続を確認してください')
            print(f'    3. しばらく時間をおいて再度試してください')
            print(f'  詳細: {error_msg[:150]}')
        else:
            print(f'✗ ログイン失敗: {error_msg[:150]}')
        return 1
    except Exception as e:
        print(f'✗ ログイン失敗 (予期しないエラー): {str(e)[:150]}')
        return 1

    # ステージ確認
    stages = c.stages()
    if stage_id not in stages:
        print(f'✗ ステージID {stage_id} はクリアされていないか見つかりません')
        print(f'  利用可能なステージ: {sorted(list(stages.keys())[:10])}...')
        return 1

    print('【2/2】自動周回開始')
    print('-' * 70)
    print(f'ステージ: {stage_id}')
    print()

    # 自動周回
    success_count = 0
    fail_count = 0
    start_time = time.time()

    for i in range(1, count + 1):
        randomized_request_delay = request_delay * random.uniform(0.7, 1.3)
        randomized_battle_time = jitter(battle_time, BATTLE_TIME_MIN)
        randomized_cooldown = jitter(cooldown, COOLDOWN_MIN)

        print(f'[{i}/{count}] リクエスト前待機中 ({randomized_request_delay:.1f}秒)...', end='', flush=True)
        time.sleep(randomized_request_delay)
        print(' 完了')

        print(f'[{i}/{count}] バトル実行中 ({randomized_battle_time:.1f}秒)...', end='', flush=True)

        try:
            rc, result = c.battle(stage_id, wait=randomized_battle_time)
            result_code = result.get('resultCode')

            if result_code == 0:
                print(f' ✓ クリア', end='')
                event_point = result.get('eventPoint', 0)
                if event_point > 0:
                    print(f' (イベントポイント: {event_point})', end='')
                print()
                success_count += 1
            else:
                error_msg = RC.get(result_code, '不明なエラー')
                print(f' ✗ 失敗 (rc={result_code}: {error_msg})')
                fail_count += 1

                if result_code in [202, 30, 32]:
                    print(f'✗ 致命的なエラーが発生したため中止します')
                    break
        except Exception as e:
            print(f' ✗ 例外エラー: {str(e)[:60]}')
            fail_count += 1

        if i < count:
            print(f'  → {randomized_cooldown:.1f}秒 待機中...', end='', flush=True)
            time.sleep(randomized_cooldown)
            print(' 完了')

        print()

    # 結果表示
    elapsed_time = time.time() - start_time
    print('=' * 70)
    print('【周回完了】')
    print('-' * 70)
    print(f'実行時間: {elapsed_time:.1f}秒')
    print(f'成功: {success_count}回')
    print(f'失敗: {fail_count}回')
    print(f'成功率: {success_count / count * 100:.1f}%' if count > 0 else '成功率: N/A')
    print()

    if success_count == count:
        print('✓ すべてのバトルが成功しました！')
    elif success_count > 0:
        print(f'⚠ {fail_count}回のバトルが失敗しました')
    else:
        print('✗ すべてのバトルが失敗しました')

    print('=' * 70)

    return 0 if fail_count == 0 else 1

if __name__ == '__main__':
    try:
        exit(main())
    except KeyboardInterrupt:
        print()
        print()
        print('✗ ユーザーによって中断されました')
        exit(1)