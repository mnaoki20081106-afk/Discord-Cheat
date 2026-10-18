#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ぷにぷに マスタデータ（全ステージID）取得

`ywp_user_stage` はプレイヤーのセーブに行があるステージしか返さないため、
未解放・未配信のステージは拾えない。ここでは getMaster.nhn を叩いて
サーバ側のステージマスタを取得する。

マスタのキー名はアプリ内の定数でこちらからは分からないので、候補を順に試し
「自分のセーブ上のステージIDをどれだけ含んでいるか」で正解を判定する
（本物のステージマスタは自分のセーブIDの上位集合になるはず）。

当たったキーと結果は data/master_cache.json に mstVersion 単位でキャッシュし、
同じマスタ版では二度と取りに行かない。全滅した場合も記録して、しばらくは
再探索しない（無駄なリクエストを投げないため）。

単体実行でキー探索プローブとして使える。各キーの生レスポンスを
data/master_probe.json に落とすので、自動判定が外れたときはこれを見て
CANDIDATE_KEYS に正しいキーを足す。

    python ywp_master.py                       # 対話入力（本物の端末で実行すること）
    python ywp_master.py --email a@b.c         # パスだけ対話入力
    python ywp_master.py --maps                # マップ／イベント名だけ取る
                                               #   → data/map_names.json
                                               #   bot.py のステージ一覧がイベント別になる
    python ywp_master.py --drops               # ドロップ関連マスタの生ダンプ＋構造要約
                                               #   → data/mst_ywp_mst_youkai_friend_lot.txt 等
    python ywp_master.py --tables a,b,c        # 任意のテーブルを同様にダンプ
    python ywp_master.py --drops --probe 29601051,2116010
                                               # 実測値が何列目に出るかを調べる

プロキシ（通信を全部そこ経由にする）:
    python ywp_master.py --check-proxy --proxy 103.4.76.237:1111   # 疎通確認だけ
    python ywp_master.py --drops --proxy 103.4.76.237:1111
    $env:YWP_PROXY="103.4.76.237:1111"; python ywp_master.py --drops
    config.json に {"proxy": "103.4.76.237:1111"} を書いてもよい
    --proxy "" で一時的にプロキシ無しにできる
    set YWP_EMAIL=a@b.c & set YWP_PASSWORD=xxx & python ywp_master.py
    # config.json に {"email": "...", "password": "..."} を書いておいてもよい

標準入力が無い環境（エディタ/エージェント経由など）では対話入力できないので、
上のいずれかで渡すこと。
"""

import base64
import collections
import gzip
import json
import os
import re
import sys
import time
import zlib

from ywp_auto import rows

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
CACHE_FILE = os.path.join(DATA_DIR, 'master_cache.json')
PROBE_FILE = os.path.join(DATA_DIR, 'master_probe.json')
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

# getMaster.nhn に渡すキーの候補。上から順に試し、当たったら止める。
# 命名はセーブ側の規則（ywp_user_stage / ywp_mst_game_const）に合わせた推測。
CANDIDATE_KEYS = [
    'ywp_mst_stage',
    'ywp_mst_stage_list',
    'ywp_mst_stage_info',
    'ywp_mst_stage_data',
    'ywp_mst_quest',
    'ywp_mst_quest_stage',
    'ywp_mst_map',
    'ywp_mst_map_stage',
    'ywp_mst_event_stage',
    'ywp_mst_area',
    'mst_stage',
    'stage',
]

STAGE_ID_MIN = 1_000_000        # ステージIDは7〜8桁（例: 10010101）
STAGE_ID_MAX = 99_999_999

MIN_COVERAGE = 0.6              # セーブ上のIDの何割を含めば「本物」とみなすか
MIN_IDS_NO_SAVE = 100           # セーブが空のときの代替基準（最低これだけIDが取れること）
PROBE_INTERVAL = 0.4            # キー探索の間隔（秒）
FAIL_RETRY = 6 * 3600           # 全滅したあと再探索するまでの間隔（秒）

# マスタ側でステージIDが入っていそうなフィールド名
ID_FIELDS = ('stageid', 'stage_id', 'stageno', 'stage_no', 'questid', 'quest_id')


# ============================================================================
# 取り出し
# ============================================================================

def _maybe_decompress(s: str) -> str | None:
    """base64(+gzip/zlib)で包まれた文字列なら中身を返す。違えば None"""
    if len(s) < 64 or re.search(r'[^A-Za-z0-9+/=_-]', s):
        return None
    try:
        raw = base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))
    except Exception:
        return None
    for f in (lambda b: zlib.decompress(b, 47), zlib.decompress, gzip.decompress):
        try:
            return f(raw).decode('utf-8', 'replace')
        except Exception:
            continue
    return None


def _ids_from_text(s: str) -> set:
    """セーブと同じ 行='*' 列='|' 形式から1列目のIDを拾う"""
    out = set()
    if '*' not in s and '|' not in s:
        return out
    try:
        parsed = rows(s)
    except (ValueError, AttributeError):
        return out
    for r in parsed or []:
        if not r:
            continue
        try:
            v = int(r[0] if isinstance(r, (list, tuple)) else r)
        except (TypeError, ValueError):
            continue
        if STAGE_ID_MIN <= v <= STAGE_ID_MAX:
            out.add(v)
    return out


def extract_stage_ids(obj, depth: int = 0) -> set:
    """レスポンスの形が分からないので、JSONを再帰的に舐めてIDらしき値を集める"""
    out = set()
    if depth > 12:
        return out

    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in ID_FIELDS:
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    n = None
                if n is not None and STAGE_ID_MIN <= n <= STAGE_ID_MAX:
                    out.add(n)
            out |= extract_stage_ids(v, depth + 1)

    elif isinstance(obj, list):
        for v in obj:
            out |= extract_stage_ids(v, depth + 1)

    elif isinstance(obj, str):
        inner = _maybe_decompress(obj)
        if inner is not None:
            obj = inner
        t = obj.strip()
        if t[:1] in '[{':
            try:
                return out | extract_stage_ids(json.loads(t), depth + 1)
            except json.JSONDecodeError:
                pass
        out |= _ids_from_text(obj)

    return out


def own_stage_ids(client) -> set:
    """自分のセーブ（ywp_user_stage）にあるステージID"""
    out = set()
    try:
        parsed = rows(client.save.get('ywp_user_stage'))
    except (ValueError, AttributeError):
        return out
    for r in parsed or []:
        if not r:
            continue
        try:
            v = int(r[0] if isinstance(r, (list, tuple)) else r)
        except (TypeError, ValueError):
            continue
        if STAGE_ID_MIN <= v <= STAGE_ID_MAX:
            out.add(v)
    return out


# ============================================================================
# キー探索
# ============================================================================

def _strip_secrets(js):
    """プローブのダンプにtokenを残さない"""
    if isinstance(js, dict):
        return {k: ('***' if str(k).lower() in ('token', 'ywptoken', 'gdsig', 'signature')
                    else _strip_secrets(v))
                for k, v in js.items()}
    if isinstance(js, list):
        return [_strip_secrets(v) for v in js[:20]]
    if isinstance(js, str) and len(js) > 400:
        return js[:400] + f'...(全{len(js)}字)'
    return js


def fetch_tables(client, table_names) -> dict:
    """getMaster.nhn でマスタを取る。返ってきた ywp_mst_* をそのまま返す。

    呼び方は ywp_probe_food.py と同じ（1テーブルずつ client.call）。実際に
    ywp_mst_youkai / ywp_mst_item がこの方法で取れている（probe_food_*.txt）。

    注意: getMaster を連続で何十回も叩くと token がズレて rc=32 になるので、
    必要なテーブルだけに絞ること。
    """
    names = table_names.split('|') if isinstance(table_names, str) else list(table_names)
    out = {}
    for i, name in enumerate(names):
        if i:
            time.sleep(PROBE_INTERVAL)
        try:
            _, js = client.call('getMaster.nhn', {'tableNames': name})
        except Exception as e:
            print(f'[master] {name}: 例外 {str(e)[:100]}')
            continue
        if js.get('resultCode') != 0:
            print(f'[master] {name}: rc={js.get("resultCode")}')
            continue
        for k, v in js.items():
            if k.startswith('ywp_mst_') and v:
                out[k] = v
    return out


def try_key(client, key: str, own: set) -> dict:
    """1キー分だけ getMaster.nhn を叩いて評価する"""
    r = {'key': key, 'rc': None, 'ids': set(), 'score': 0.0, 'ok': False,
         'note': '', 'raw': None}
    try:
        _, js = client.call('getMaster.nhn', {'tableNames': key})
    except Exception as e:
        r['note'] = f'例外: {str(e)[:100]}'
        return r

    r['rc'] = js.get('resultCode')
    r['raw'] = _strip_secrets(js)
    if r['rc'] != 0:
        r['note'] = f'resultCode={r["rc"]}'
        return r

    ids = extract_stage_ids(js)
    r['ids'] = ids
    if not ids:
        r['note'] = 'ステージIDらしき値なし'
        return r

    if own:
        r['score'] = len(ids & own) / len(own)
        r['ok'] = r['score'] >= MIN_COVERAGE and len(ids) >= len(own)
        r['note'] = (f'{len(ids)}件 / セーブID被覆 {r["score"]:.0%}'
                     f'（{len(ids & own)}/{len(own)}）')
    else:
        # セーブが空なら被覆率で測れないので件数だけで判断する
        r['score'] = min(len(ids) / 1000, 1.0)
        r['ok'] = len(ids) >= MIN_IDS_NO_SAVE
        r['note'] = f'{len(ids)}件（セーブが空のため件数のみで判定）'
    return r


def discover(client, keys=None, own=None, stop_early: bool = True,
             verbose: bool = False) -> list:
    """候補キーを順に試す。stop_early なら合格した時点で打ち切る"""
    own = own_stage_ids(client) if own is None else own
    results = []
    for i, key in enumerate(keys or CANDIDATE_KEYS):
        if i:
            time.sleep(PROBE_INTERVAL)
        r = try_key(client, key, own)
        results.append(r)
        if verbose:
            mark = 'OK ' if r['ok'] else '   '
            print(f'  {mark}{key:<24} {r["note"]}')
        if r['ok'] and stop_early:
            break
    return results


# ============================================================================
# キャッシュ
# ============================================================================

def _load_cache() -> dict:
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_probe(results: list) -> bool:
    """各候補キーの生レスポンスを data/master_probe.json に残す"""
    dump = [{'key': r['key'], 'rc': r['rc'], 'ids': len(r['ids']),
             'score': round(r['score'], 3), 'ok': r['ok'], 'note': r['note'],
             'sample': sorted(r['ids'])[:20], 'raw': r['raw']}
            for r in results]
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(PROBE_FILE, 'w', encoding='utf-8') as f:
            json.dump(dump, f, ensure_ascii=False, indent=2)
        return True
    except OSError as e:
        print(f'[master] プローブ結果の保存に失敗: {e}')
        return False


def _save_cache(entry: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CACHE_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(entry, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except OSError as e:
        print(f'[master] キャッシュ保存に失敗: {e}')


def cached_stage_ids(client) -> set:
    """キャッシュ済みの全ステージIDを返す。通信はしない。無ければ空集合

    「マスタ込み」で一度取れていれば、通常の一覧でも未解放面を出すのに使える。
    マスタ版が変わっていたら古いので使わない。
    """
    cache = _load_cache()
    if cache.get('mst') == getattr(client, 'mst', None) and cache.get('ids'):
        return set(cache['ids'])
    return set()


def fetch_all_stage_ids(client, force: bool = False,
                        verbose: bool = False) -> tuple[set, str | None, str]:
    """全ステージIDを取得する。

    戻り値 (ids, key, source):
        ids    … ステージIDの集合（失敗時は空）
        key    … 当たった getMaster.nhn のキー（失敗時 None）
        source … 'cache' / 'fetch' / 'failed' / 'skipped'
                 skipped は直近に全滅していて再探索を見送った場合
    """
    cache = _load_cache()
    same_ver = cache.get('mst') == client.mst

    if not force and same_ver and cache.get('ids'):
        return set(cache['ids']), cache.get('key'), 'cache'

    if (not force and same_ver and cache.get('failed_at')
            and time.time() - cache['failed_at'] < FAIL_RETRY):
        return set(), None, 'skipped'

    # 前回当たったキーがあれば最優先で試す
    keys = list(CANDIDATE_KEYS)
    prev = cache.get('key')
    if prev:
        if prev in keys:
            keys.remove(prev)
        keys.insert(0, prev)

    own = own_stage_ids(client)
    results = discover(client, keys, own, stop_early=True, verbose=verbose)
    hit = next((r for r in results if r['ok']), None)

    if hit is None:
        # 失敗したときは生レスポンスを残す。Bot経由で呼ばれた場合でも、これを見れば
        # 正しいキー名を特定できる（単体でプローブを回し直さなくてよい）
        _save_probe(results)
        _save_cache({'mst': client.mst, 'key': prev, 'ids': [],
                     'failed_at': int(time.time())})
        return set(), None, 'failed'

    ids = hit['ids'] | own          # マスタに無い行が自分のセーブにある場合も拾う
    _save_cache({'mst': client.mst, 'key': hit['key'], 'ids': sorted(ids),
                 'fetched_at': int(time.time())})
    return ids, hit['key'], 'fetch'


# ============================================================================
# マップ／イベント名マスタ（bot.py のステージ一覧がイベント別に出せるように）
# ============================================================================

MAP_NAMES_FILE = os.path.join(DATA_DIR, 'map_names.json')
MAP_RAW_FILE = os.path.join(DATA_DIR, 'mst_map_raw.txt')
MAP_SCHEMA_FILE = os.path.join(DATA_DIR, 'mst_map_schema.json')

# login.nhn の ywp_mst_map は dict で来るので列名が分かる。getMaster.nhn は
# 区切り文字列なので列順が分からない。両方を突き合わせて列位置を特定する。
MAP_FIELDS = ('mapName', 'description', 'eventId', 'sort', 'mapType',
              'openDt', 'closeDt', 'nextMapId', 'reverseMapId', 'mapSubType')


def login_maps(client) -> dict:
    """login 応答の ywp_mst_map（dict形式・開催中のぶんだけ）を {mapId: dict} に"""
    raw = (client.save or {}).get('ywp_mst_map')
    out = {}
    for m in raw if isinstance(raw, list) else []:
        if isinstance(m, dict) and str(m.get('mapId', '')).isdigit():
            out[int(m['mapId'])] = m
    return out


def infer_map_columns(table_data: str, known: dict) -> tuple:
    """(mapId列, {フィールド名: 列index}) を返す。特定できなければ (None, {})

    known は login 応答由来の {mapId: dict}。この値が tableData のどの列に
    現れるかを数えて列位置を決める（推測ではなく実値の一致で確定させる）。
    """
    rows_ = [r.split('|') for r in table_data.split('*') if r]
    if not rows_ or not known:
        return None, {}
    width = min(len(r) for r in rows_)

    # mapId列: 全行が数値で重複なし、かつ known の mapId を全部含む列
    id_col = None
    for c in range(width):
        vals = [r[c] for r in rows_]
        if not all(v.isdigit() for v in vals):
            continue
        ints = [int(v) for v in vals]
        if len(set(ints)) == len(ints) and set(known) <= set(ints):
            id_col = c
            break
    if id_col is None:
        return None, {}

    by_id = {int(r[id_col]): r for r in rows_}
    cols = {}
    for field in MAP_FIELDS:
        hits = None
        for mid, m in known.items():
            want = str(m.get(field, ''))
            row = by_id.get(mid)
            if row is None:
                continue
            here = {c for c in range(len(row)) if row[c] == want and c != id_col}
            hits = here if hits is None else (hits & here)
        if hits and len(hits) == 1:
            cols[field] = hits.pop()
    return id_col, cols


def fetch_map_names(client, verbose: bool = True) -> dict:
    """ywp_mst_map を取って {mapId: {name,event,eventId,sort,...}} を作り保存する。

    getMaster.nhn が ywp_mst_map を返さない場合は login 応答のぶんだけで返す。
    bot.py はこの data/map_names.json を読むので、一度走らせれば一覧に
    イベント名が出るようになる。
    """
    known = login_maps(client)
    if verbose:
        print(f'login の ywp_mst_map: {len(known)}件')

    tables = fetch_tables(client, ['ywp_mst_map'])
    raw = tables.get('ywp_mst_map')
    table_data = raw.get('tableData') if isinstance(raw, dict) else raw

    result = {}
    if isinstance(table_data, str) and table_data:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(MAP_RAW_FILE, 'w', encoding='utf-8') as f:
            f.write(table_data)
        id_col, cols = infer_map_columns(table_data, known)
        if verbose:
            print(f'getMaster の ywp_mst_map: {table_data.count("*") + 1}行  '
                  f'mapId列={id_col}  特定できた列={sorted(cols)}')
        if id_col is not None and 'mapName' in cols:
            with open(MAP_SCHEMA_FILE, 'w', encoding='utf-8') as f:
                json.dump({'mapId': id_col, **cols}, f, ensure_ascii=False, indent=1)
            for row in (r.split('|') for r in table_data.split('*') if r):
                if len(row) <= id_col or not row[id_col].isdigit():
                    continue

                def g(field, default=''):
                    c = cols.get(field)
                    return row[c] if c is not None and c < len(row) else default

                result[int(row[id_col])] = {
                    'name': g('mapName'),
                    'event': ' '.join(g('description').split()),
                    'eventId': int(g('eventId', '0') or 0),
                    'sort': int(g('sort', '0') or 0),
                    'mapType': int(g('mapType', '0') or 0),
                    'openDt': g('openDt'),
                    'closeDt': g('closeDt'),
                }
        elif verbose:
            print('  → 列順を確定できませんでした。'
                  f'{MAP_RAW_FILE} を見て MAP_FIELDS を調整してください。')
    elif verbose:
        print('getMaster.nhn は ywp_mst_map を返しませんでした（login のぶんだけ使います）')

    # login 側は列名が確実なので、こちらを優先して上書きする
    for mid, m in known.items():
        result[mid] = {
            'name': str(m.get('mapName') or '').strip(),
            'event': ' '.join(str(m.get('description') or '').split()),
            'eventId': int(m.get('eventId') or 0),
            'sort': int(m.get('sort') or 0),
            'mapType': int(m.get('mapType') or 0),
            'openDt': str(m.get('openDt') or ''),
            'closeDt': str(m.get('closeDt') or ''),
        }

    if result:
        # 既存キャッシュに足す（過去に見たイベントを消さない）
        try:
            with open(MAP_NAMES_FILE, 'r', encoding='utf-8') as f:
                old = {int(k): v for k, v in json.load(f).items()}
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            old = {}
        added = len(set(result) - set(old))
        old.update(result)
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(MAP_NAMES_FILE, 'w', encoding='utf-8') as f:
            json.dump({str(k): v for k, v in old.items()}, f, ensure_ascii=False)
        if verbose:
            named = sum(1 for v in old.values() if v.get('event') or v.get('name'))
            print(f'✓ {MAP_NAMES_FILE} に保存: 全{len(old)}件（新規{added}件 / 名前あり{named}件）')
        return old
    return {}


# ============================================================================
# 任意テーブルの生ダンプ＋構造要約
#   列順はアプリ内の定数なので外からは分からない。推測で解釈すると嘘の表が
#   できるので、ここでは (1) 生の tableData を保存し (2) 列ごとの性質を数える
#   だけにする。列の意味は要約と実キャプチャの突き合わせで後から確定させる。
# ============================================================================

# 妖怪ドロップ／アイテムドロップ／敵構成
DROP_TABLES = (
    'ywp_mst_youkai_friend_lot',        # 仲間になる妖怪の抽選
    'ywp_mst_youkai_enemy_item_lot',    # 敵からのアイテムドロップ抽選
    'ywp_mst_youkai_enemy_set',         # ステージの敵構成
)

SAMPLE_COLS = 3         # 列ごとに出す実例の数
NUMERIC_KINDS = (
    ('stageId', 1_000_000, 99_999_999),
    ('youkaiId', 1_000_000, 9_999_999),
    ('enemyId', 10_000_000, 999_999_999),
    ('itemId', 10_000, 999_999),
    ('mapId', 1_000, 99_999),
)


def table_data_of(v):
    """getMaster.nhn の戻りから tableData 文字列を取り出す"""
    if isinstance(v, dict):
        return v.get('tableData')
    return v if isinstance(v, str) else None


def describe_table(table_data: str) -> dict:
    """列ごとの性質を数える。{'rows':n, 'cols':[{...}]}"""
    rows_ = [r.split('|') for r in table_data.split('*') if r]
    if not rows_:
        return {'rows': 0, 'cols': []}
    widths = collections.Counter(len(r) for r in rows_)
    width = max(len(r) for r in rows_)

    cols = []
    for c in range(width):
        vals = [r[c] for r in rows_ if c < len(r)]
        nums = [v for v in vals if v.lstrip('-').isdigit()]
        info = {
            'i': c,
            'n': len(vals),
            'distinct': len(set(vals)),
            'numeric': len(nums) == len(vals) and bool(vals),
            'blank': sum(1 for v in vals if v == ''),
            'sample': list(dict.fromkeys(v for v in vals if v))[:SAMPLE_COLS],
        }
        if info['numeric']:
            iv = [int(v) for v in nums]
            info['min'], info['max'] = min(iv), max(iv)
            # 値域から「IDっぽさ」を推定（あくまでヒント。確定はさせない）
            info['looks'] = [name for name, lo, hi in NUMERIC_KINDS
                             if info['min'] >= lo and info['max'] <= hi]
        cols.append(info)
    return {'rows': len(rows_), 'widths': dict(widths), 'cols': cols}


def locate_values(table_data: str, values) -> dict:
    """指定した値が何列目に出るかを数える。実キャプチャと突き合わせて列を確定させる用

    例: gameStart で見た enemyId と、図鑑差分で確定した妖怪IDを渡すと、
        enemyId列 / youkaiId列 がどこか分かる。
    """
    rows_ = [r.split('|') for r in table_data.split('*') if r]
    out = {}
    for want in values:
        w = str(want)
        hits = collections.Counter()
        rows_hit = []
        for r in rows_:
            for c, v in enumerate(r):
                if v == w:
                    hits[c] += 1
                    if len(rows_hit) < 3:
                        rows_hit.append('|'.join(r))
        out[w] = {'cols': dict(hits), 'rows': rows_hit}
    return out


def print_description(name: str, d: dict):
    print(f'\n■ {name}   {d["rows"]}行  列数={d.get("widths")}')
    print('   %-4s %-8s %-8s %-7s %s' % ('col', 'distinct', 'min', 'max', '例 / 推定'))
    for c in d['cols']:
        looks = ('  ← ' + '/'.join(c['looks'])) if c.get('looks') else ''
        rng = (f'{c["min"]:<8} {c["max"]:<7}' if c['numeric'] else
               f'{"(文字列)":<8} {"":<7}')
        ex = ', '.join(c['sample'])[:46]
        print('   %-4d %-8d %s %s%s' % (c['i'], c['distinct'], rng, ex, looks))


def dump_tables(client, names=DROP_TABLES, probe_values=(), verbose=True) -> dict:
    """テーブルを取って data/mst_<名前>.txt に生保存し、構造要約を表示する"""
    os.makedirs(DATA_DIR, exist_ok=True)
    got = fetch_tables(client, list(names))
    if verbose:
        miss = [n for n in names if n not in got]
        print(f'\n取得できたテーブル: {sorted(got)}')
        if miss:
            print(f'返ってこなかったテーブル: {miss}')
            print('  （getMaster.nhn で配信されていない＝masterServer側の可能性）')

    summary = {}
    for name, v in got.items():
        td = table_data_of(v)
        if not isinstance(td, str) or not td:
            if verbose:
                print(f'\n■ {name}: tableData が空（{type(v).__name__}）')
            continue
        path = os.path.join(DATA_DIR, f'mst_{name}.txt')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(td)
        d = describe_table(td)
        summary[name] = {'path': path, 'bytes': len(td), **d}
        if verbose:
            print_description(name, d)
            print(f'   → 生データ: {path}  ({len(td):,}バイト)')
            if probe_values:
                loc = locate_values(td, probe_values)
                print('   実キャプチャの値がどの列に出るか:')
                for w, r in loc.items():
                    if r['cols']:
                        print(f'     {w:>12} → 列 {r["cols"]}')
                        for row in r['rows'][:1]:
                            print(f'                    例: {row[:160]}')
                    else:
                        print(f'     {w:>12} → 出現なし')
    if summary:
        with open(os.path.join(DATA_DIR, 'mst_dump_summary.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=1)
    return summary


# ============================================================================
# プローブ（単体実行）
# ============================================================================

def _fix_console():
    """Windowsのcp932コンソールでも日本語が化けないようにする（ベストエフォート）"""
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, OSError, ValueError):
            pass


def _cli_arg(name: str) -> str | None:
    """--name value / --name=value を拾う"""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == f'--{name}' and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(f'--{name}='):
            return a.split('=', 1)[1]
    return None


def _config_value(key: str) -> str | None:
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    v = cfg.get(key) if isinstance(cfg, dict) else None
    return str(v).strip() if v else None


def _credentials() -> tuple[str, str] | None:
    """引数 → 環境変数 → config.json → 対話入力 の順で拾う"""
    email = (_cli_arg('email') or os.environ.get('YWP_EMAIL')
             or _config_value('email') or '').strip()
    password = (_cli_arg('password') or os.environ.get('YWP_PASSWORD')
                or _config_value('password') or '')

    if not email or not password:
        # isatty()が真でも実際には読めない環境があるのでEOFErrorで判断する
        import getpass
        try:
            if not email:
                email = input('メールアドレス: ').strip()
            if not password:
                password = getpass.getpass('パスワード: ')
        except (EOFError, OSError):
            print('\n✗ この環境では対話入力ができません（標準入力が空）。')
            print('  PowerShellを直接開いて実行するか、次のどれかで渡してください:\n')
            print('    python ywp_master.py --email a@b.c --password xxx')
            print('    $env:YWP_EMAIL="a@b.c"; $env:YWP_PASSWORD="xxx"; '
                  'python ywp_master.py')
            print('    config.json に {"email": "...", "password": "..."} を追記')
            return None
        except KeyboardInterrupt:
            print('\n中断しました。')
            return None

    if not email or not password:
        print('✗ メール/パスワードが空です。')
        return None
    return email, password


def main():
    _fix_console()

    from ywp_auto import login_email, set_proxy, check_proxy

    # プロキシ（--proxy > YWP_PROXY > config.json の "proxy"）
    px = _cli_arg('proxy')
    if px is not None:
        set_proxy(px)
    st = check_proxy()
    print('出口: %s  外から見えるIP: %s  LEVEL5 ID: %s'
          % (st['proxy'], st.get('ip') or '取得失敗', st.get('level5_id_status')))
    for k in ('error', 'level5_id_error'):
        if st.get(k):
            print('  ⚠ %s: %s' % (k, st[k]))
    if '--check-proxy' in sys.argv:
        return
    if st.get('ip') is None:
        print('✗ 外部に出られていません。プロキシの指定を確認してください。')
        print('  (プロキシ無しで試すなら --proxy "" を付ける)')
        return
    print()

    print('=== ぷにぷに マスタキー探索 ===')
    print('メール/パスはこの端末から直接ゲームサーバへ送るだけで、保存はしません。\n')

    creds = _credentials()
    if creds is None:
        return
    email, password = creds

    print('\nログイン中...')
    try:
        c = login_email(email, password)
    except Exception as e:
        print(f'✗ ログイン失敗: {e}')
        return
    finally:
        password = None

    own = own_stage_ids(c)
    print(f'✓ ログイン成功  mstVersion={c.mst}  セーブ上のステージID={len(own)}件\n')

    if '--maps' in sys.argv:
        # ステージ一覧をイベント別に出すためのマップ／イベント名だけ取る
        print('=== マップ／イベント名マスタ ===')
        fetch_map_names(c)
        return

    if '--drops' in sys.argv or _cli_arg('tables'):
        names = ([t.strip() for t in (_cli_arg('tables') or '').split(',') if t.strip()]
                 or list(DROP_TABLES))
        # 実キャプチャで確定している値。これが何列目に出るかで列の意味を決める
        #   29601051 / 29601052 … stage 29601005 の敵（gameStart 実測）
        #   2116010 / 2624000   … その敵の妖怪ID（ywp_user_dictionary_diff で確定）
        probe = [x.strip() for x in (_cli_arg('probe')
                 or '29601051,29601052,2116010,2624000,29601005').split(',') if x.strip()]
        print(f'=== テーブルダンプ: {names} ===')
        print(f'    突き合わせる実測値: {probe}\n')
        dump_tables(c, names, probe)
        return

    print('候補キーを順に試します（全部試すので少し時間がかかります）:')

    results = discover(c, CANDIDATE_KEYS, own, stop_early=False, verbose=True)

    if _save_probe(results):
        print(f'\n生レスポンスを {PROBE_FILE} に保存しました。')

    hits = [r for r in results if r['ok']]
    if not hits:
        print('\n✗ 当たりキーは見つかりませんでした。')
        print('  master_probe.json の raw を見て、ステージIDが入っているキーを')
        print('  CANDIDATE_KEYS の先頭に追加してからもう一度実行してください。')
        return

    best = max(hits, key=lambda r: r['score'])
    ids = best['ids'] | own
    _save_cache({'mst': c.mst, 'key': best['key'], 'ids': sorted(ids),
                 'fetched_at': int(time.time())})
    print(f'\n✓ 当たり: {best["key"]}  → 全{len(ids)}件をキャッシュしました')
    print(f'  未解放（セーブに無い）: {len(ids - own)}件')
    print(f'  保存先: {CACHE_FILE}')


if __name__ == '__main__':
    main()
