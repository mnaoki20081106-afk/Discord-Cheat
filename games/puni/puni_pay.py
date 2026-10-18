"""
puni_pay.py — PayPay決済（請求リンク＋取引番号照合）。AutoTsum/tsum_pay.py と同じもの

自動代行と同じ方式:
  BOTが請求リンクを作る（createP2PCode）→ お客様が支払う → 取引番号を入力
  → 受取口座の直近の取引履歴で照合（受取・完了・金額一致）→ 使用済みに記録 → 処理開始
PayPayとの通信は paypay_engine.py（自動代行と同じもの）。送金リンクの受け取り（旧 paypayu）は使わない。

トークンはほかのBOTと同じく PAY管理BOT のフォルダ（paypay_accounts.json / paypay_tokens.json）の
アクティブアカウントをそのまま使う。更新結果も paypay_engine が PAY管理 へ書き戻すので、
同じトークンを使うほかのBOTもそのまま動く。ログイン・アカウント切替は PAY管理BOT で行う。
（PAY管理 が見つからない時だけ、paypay_engine がこのフォルダの同じ形式のファイルを使う。
  その場合のログインは BOT の PayPayログイン画面から）

どの関数も同期（PayPayと通信する）。BOTからは asyncio.to_thread で呼ぶ。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import paypay_engine as pe

JST = timezone(timedelta(hours=9))

# PAY管理 のトークンを使っているか（False = PAY管理 が見つからずこのフォルダのトークン）
TOKEN_DIR = pe.PAY_DIR
USING_PAYMASTER = (os.path.normcase(os.path.abspath(pe.PAY_DIR))
                   != os.path.normcase(os.path.abspath(pe.BASE_DIR)))

NO_WALLET_MSG = "PayPay受取アカウントが未設定です。管理者にご連絡ください。"
NET_ERR_MSG = "PayPayとの通信に失敗しました。少し待ってからもう一度お試しください。"
PAYMASTER_LOGIN_MSG = ("このBOTは PAY管理BOT のPayPayトークンを使っています。\n"
                       "ログイン・アカウント切替は PAY管理BOT で行ってください。")


def _now():
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def mask_phone(phone) -> str:
    p = str(phone or "")
    return ("*" * max(0, len(p) - 4) + p[-4:]) if p else "(不明)"


# ============================================================
# トークン（PAY管理 のアクティブアカウント）
# ============================================================

def get_tokens():
    """→ (access_token, device_uuid)。無ければ (None, None)"""
    at, du, _phone = pe.get_active_account()
    return (at, du) if at else (None, None)


def is_ready() -> bool:
    return bool(get_tokens()[0])


def refresh(used_at):
    """2回目用のトークン。他のBOTが更新済みならそれを使い、なければ更新して PAY管理 へ保存する。"""
    at, du = get_tokens()
    if at and at != used_at:
        return at, du
    if not pe.refresh_active_account():
        return None, None
    return get_tokens()


def wallet_status():
    """→ (本文, "ok"|"warn")。トークン本体は出さない。"""
    at, _du, phone = pe.get_active_account()
    if USING_PAYMASTER:
        head = f"PAY管理BOT のPayPayを使用中（ログイン・切替は PAY管理BOT）\n-# {TOKEN_DIR}\n"
        if not at:
            return head + "アクティブなアカウントがありません。PAY管理BOTでログインしてください。", "warn"
    else:
        head = f"PAY管理 が見つからないため、このBOTのフォルダのトークンを使用中\n-# {TOKEN_DIR}\n"
        if not at:
            return head + "アカウントがありません。`/paypayログイン1` でログインしてください。", "warn"
    return head + f"アカウント: `{mask_phone(phone)}`\n※トークン本体は表示していません。", "ok"


# ============================================================
# 請求リンク・取引番号照合
# ============================================================

def create_billing_link(amount):
    """→ (リンク|None, エラー文)"""
    try:
        at, du = get_tokens()
        if not at:
            return None, NO_WALLET_MSG
        payload = pe.create_p2p_code(at, du, amount=int(amount))
        if payload and payload.get("link"):
            return payload["link"], ""
        at, du = refresh(at)
        if at:
            payload = pe.create_p2p_code(at, du, amount=int(amount))
            if payload and payload.get("link"):
                return payload["link"], ""
        return None, "請求リンクを作成できませんでした。少し待ってからもう一度お試しください。"
    except Exception as e:
        print(f"[puni_pay] 請求リンク作成エラー: {type(e).__name__}: {e}")
        return None, NET_ERR_MSG


def normalize_order_id(raw) -> str:
    """全角・空白混じりで貼られても照合できるように整える"""
    s = unicodedata.normalize("NFKC", str(raw or ""))
    return re.sub(r"\s+", "", s)


def canonical_order_id(detail, fallback="") -> str:
    """照合できた明細の『その入金を一意に指すID』。
    明細1件に orderId / subOrderId / transactionId があり、どれを打っても照合は通る。
    入力値をそのまま使用済みキーにすると同じ入金を3回まで使えてしまうので、ここで1つに揃える
    （自動代行と同じ優先順位）。"""
    d = detail or {}
    for key in ("orderId", "transactionId", "subOrderId"):
        v = str(d.get(key) or "").strip()
        if v:
            return v
    return str(fallback or "").strip()


HISTORY_LIMIT = 10                              # 照合で見る取引履歴（直近の件数）
_DONE_STATUS = {"COMPLETED", "RECEIVED", "SUCCESS"}


def _verify_recent(at, du, expected, order_id, limit=HISTORY_LIMIT):
    """直近 limit 件の取引履歴から取引番号を探して照合する。
    判定は paypay_engine.verify_payment と同じ（完全一致・受取・完了・金額一致）。"""
    payload = pe.get_payment_history(at, du, page_size=limit)
    if payload is None:
        return False, "取引履歴の取得に失敗しました（通信エラー/トークン失効の可能性）。", None
    for item in (payload.get("paymentInfoList") or [])[:limit]:
        ids = {str(item.get(k) or "") for k in ("orderId", "subOrderId", "transactionId")}
        if order_id not in ids:
            continue
        try:
            got = abs(int(item.get("totalAmount") or item.get("amount") or 0))
        except (TypeError, ValueError):
            got = 0
        status = str(item.get("orderStatus") or "").upper()
        if not pe._is_incoming(item.get("orderType")):
            return False, "この取引番号は支払い（出金）の取引です。受け取りの取引番号を入力してください。", item
        if status and status not in _DONE_STATUS:
            return False, (f"取引は見つかりましたが、まだ完了していません（{status}）。"
                           "少し待ってから同じ取引番号でもう一度お試しください。"), item
        if got != int(expected):
            return False, f"金額が一致しません。必要額: {int(expected):,}円 / この取引: {got:,}円", item
        return True, f"支払い確認OK: {got:,}円", item
    return False, (f"取引番号が直近{limit}件の取引履歴に見つかりませんでした。\n"
                   "番号が正しいか確認してください。支払った直後は反映まで少しかかることがあります"
                   "（数十秒後にもう一度お試しください）。"), None


def verify(amount, order_id):
    """取引番号を受取口座の直近の取引履歴で照合する。→ (ok, メッセージ, 明細|None)"""
    order_id = normalize_order_id(order_id)
    if not order_id:
        return False, "取引番号を入力してください。", None
    try:
        at, du = get_tokens()
        if not at:
            return False, NO_WALLET_MSG, None
        ok, msg, detail = _verify_recent(at, du, int(amount), order_id)
        if not ok and detail is None and "取得に失敗" in msg:
            at, du = refresh(at)
            if at:
                ok, msg, detail = _verify_recent(at, du, int(amount), order_id)
        return ok, msg, detail
    except Exception as e:
        print(f"[puni_pay] 照合エラー: {type(e).__name__}: {e}")
        return False, "PayPayとの通信に失敗しました。少し待ってから同じ取引番号でもう一度お試しください。", None


# ============================================================
# 使用済み取引番号
# ============================================================

class StoreError(Exception):
    pass


class _XLock:
    """プロセス間ロック（ロックファイルの排他生成）。自動代行の used_orders.json と同じ作法なので、
    設定で同じファイルを指せば自動代行のBOTと安全に共有できる。"""

    STALE = 15.0

    def __init__(self, path, timeout=20.0):
        self.path, self.timeout = path, timeout
        self._fd = None

    def __enter__(self):
        start = time.time()
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                return self
            except (FileExistsError, PermissionError):
                try:
                    if time.time() - os.path.getmtime(self.path) > self.STALE:
                        os.remove(self.path)
                        continue
                except OSError:
                    pass
                if time.time() - start > self.timeout:
                    raise StoreError(f"ロックを取得できません: {self.path}")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            if self._fd is not None:
                os.close(self._fd)
            os.remove(self.path)
        except OSError:
            pass


class UsedOrderStore:
    """照合に成功した取引番号を記録し、再利用を拒否する。形式は自動代行と同じ {取引番号: 情報}。"""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self._lock = threading.Lock()
        self._lockfile = self.path + ".lock"

    def _load(self) -> dict:
        # 壊れたファイルを空扱いにすると全取引番号を再利用できてしまうので、読めなければ止める
        for i in range(10):
            try:
                with open(self.path, encoding="utf-8") as f:
                    d = json.load(f)
                break
            except FileNotFoundError:
                return {}
            except (PermissionError, json.JSONDecodeError) as e:
                if i == 9:
                    raise StoreError(f"使用済み取引番号を読めません: {self.path}: {e}") from e
                time.sleep(0.05)
        if not isinstance(d, dict):
            raise StoreError(f"使用済み取引番号の形式が不正です: {self.path}")
        return d

    def _save(self, data):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            for i in range(20):
                try:
                    os.replace(tmp, self.path)
                    return
                except PermissionError:
                    if i == 19:
                        raise
                    time.sleep(0.05)
        except Exception as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise StoreError(f"使用済み取引番号を保存できません: {self.path}: {e}") from e

    def get_info(self, order_id):
        with self._lock, _XLock(self._lockfile):
            return self._load().get(str(order_id).strip())

    def try_consume(self, order_id, *, user_id=None, guild_id=None, amount=None, kind="") -> bool:
        """未使用なら記録して True、使用済みなら False"""
        oid = str(order_id).strip()
        if not oid:
            return False
        with self._lock, _XLock(self._lockfile):
            data = self._load()
            if oid in data:
                return False
            data[oid] = {"used_at_jst": _now(), "user_id": user_id, "guild_id": guild_id,
                         "amount": amount, "kind": kind}
            self._save(data)
            return True


# ============================================================
# ログイン（PAY管理 が見つからない時だけ）
#   自動代行と同じく paypay_engine の login_start / login_complete をそのまま使う
#   （保存先は paypay_engine が使うフォルダ = このBOTのフォルダ）。
# ============================================================

def login_start(user_id, phone, password):
    """→ (status, メッセージ)  status: "otp_required" / "ok" / "error" """
    if USING_PAYMASTER:
        return "error", PAYMASTER_LOGIN_MSG
    if not getattr(pe, "_HAS_PLAYWRIGHT", False):
        return "error", ("PayPayログインには playwright が必要です。\n"
                         "`pip install playwright` のあと `playwright install chromium` を実行してください。")
    phone = re.sub(r"\D", "", unicodedata.normalize("NFKC", phone or ""))
    if not phone or not password:
        return "error", "電話番号とパスワードを入力してください。"
    ok, status, msg = pe.login_start(user_id, phone, password)
    if status == "otp_required":
        return "otp_required", "SMSに認証用のURL（またはコード）を送りました。"
    return ("ok" if ok else "error"), msg


def login_complete(user_id, otp):
    """→ (ok, メッセージ)"""
    if USING_PAYMASTER:
        return False, PAYMASTER_LOGIN_MSG
    return pe.login_complete(user_id, (otp or "").strip())
