"""
paypay_engine.py — PayPay決済+ログインコア (p.py から抽出・共通モジュール化)

にゃんこ代行 bot (bot_paypay.py / main.py) が import して使う。
- ログイン: WAF突破(Playwright) → OAuth2 PAR → password → SMS OTP → token交換
- 決済: 請求リンク作成 (createP2PCode) + 取引番号照合 (getPaymentHistory)

JSONファイル (paypay_accounts.json / paypay_udid_map.json / paypay_tokens.json) は
PAY管理BOT のものを共有する (PAY管理 が見つからないときだけこのファイルと同じディレクトリ)。

依存: httpx, playwright (WAF突破=ログイン時のみ)
"""

import base64
import hashlib
import json
import os
import secrets
import time
from uuid import uuid4
from urllib.parse import urlparse, parse_qs

import httpx

# playwright はログイン(WAF突破)時のみ必要。未インストールでも決済は動くよう遅延import。
try:
    from playwright.sync_api import sync_playwright
    _HAS_PLAYWRIGHT = True
except Exception:
    _HAS_PLAYWRIGHT = False

from datetime import datetime, timezone, timedelta

BASE_PORTAL = "https://www.paypay.ne.jp"
BASE_APP4 = "https://app4.paypay.ne.jp"
CLIENT_ID = "pay2-mobile-app-client"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# PayPayのトークンは PAY管理BOT が維持しているものを全BOTで共有する。
# フォルダごとに自前のコピーを持つと、どれか1つがリフレッシュした時点で
# 他のコピーの refresh_token が失効し、ある日突然決済が通らなくなる。
# 別の場所を使うときは環境変数 PAYMASTER_DIR で指定する（bot2 はこれで本家の PAY管理 を見る）。
PAY_DIR = (os.environ.get("PAYMASTER_DIR", "").strip()
           or os.path.join(os.path.dirname(BASE_DIR), "PAY管理"))
if not os.path.isfile(os.path.join(PAY_DIR, "paypay_accounts.json")):
    print(f"[paypay] ⚠️ PAY管理 が見つかりません: {PAY_DIR} / "
          f"このフォルダのトークンを使います")
    PAY_DIR = BASE_DIR
TOKEN_FILE = os.path.join(PAY_DIR, "paypay_tokens.json")
ACCOUNTS_FILE = os.path.join(PAY_DIR, "paypay_accounts.json")
UDID_MAP_FILE = os.path.join(PAY_DIR, "paypay_udid_map.json")
JST = timezone(timedelta(hours=9))

UA = (
    "Mozilla/5.0 (Linux; Android 16; Pixel 6a Build/CP1A.260305.018; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/147.0.7727.138 Mobile Safari/537.36 jp.pay2.app.android/5.50.0"
)



HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Client-Id": CLIENT_ID,
    "Client-OS-Type": "ANDROID",
    "Client-OS-Version": "36.0.0",
    "Client-OS-Release-Version": "16",
    "Client-Type": "PAYPAYAPP",
    "Client-Version": "5.55.0",
    "Client-Mode": "NORMAL",
    "Client-UUID": "a8657e69-5d49-4bbf-b86c-e085652646ea",
    "Device-UUID": "15c46d4e-f3fe-4324-8458-78e37016b00e",
    "Device-Name": "Pixel 6a",
    "Device-Hardware-Name": "bluejay",
    "Device-Manufacturer-Name": "Google",
    "Device-Brand-Name": "google",
    "Is-Emulator": "false",
    "Network-Status": "WIFI",
    "System-Locale": "ja",
    "Timezone": "Asia/Tokyo",
    "User-Agent": "PaypayApp/5.55.0 Android16",
    "Device-Lock-Type": "DEVICE",
    "Device-Lock-App-Setting": "false",
    "Device-In-Call": "false",
    "Device-Screen-Recording": "false",
    "App-Mode": "domestic_automatic",
    "Device-App-Check": "FAILURE - UNKNOWN",
    "Device-Acceleration": "-0.0040711286_3.9784907E-4_-0.037927248",
    "Device-Acceleration-2": "NULL",
    "Device-Orientation": "-0.7248919_-0.00937534_0.010662667",
    "Device-Orientation-2": "NULL",
    "Device-Rotation": "0.74846226_-0.6630254_0.014197531",
    "Device-Rotation-2": "NULL",
    "Accept-Charset": "UTF-8",
    "Accept-Encoding": "gzip",
}


# ============================================================
# 汎用ユーティリティ


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def update_cookies(cookies, resp):
    for h in resp.headers.get_list("set-cookie"):
        parts = h.split(";")
        nv = parts[0].split("=", 1)
        if len(nv) != 2:
            continue
        name, val = nv[0].strip(), nv[1].strip()
        if "1970" in h or val == "":
            cookies.pop(name, None)
        else:
            cookies[name] = val


def cookie_str(cookies):
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def deep_find(d, *keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d:
            return d[k]
    for v in d.values():
        if isinstance(v, dict):
            r = deep_find(v, *keys)
            if r is not None:
                return r
    return None


def extract_code_from_url(url):
    if not url:
        return None
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    codes = params.get("code", [])
    return codes[0] if codes else None


def make_headers(device_uuid=None):
    h = {**HEADERS}
    if device_uuid:
        h["Device-UUID"] = device_uuid
    return h


def jst_now():
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")


def yen(n):
    try:
        return f"¥{int(n):,}"
    except Exception:
        return f"¥{n}"


# ============================================================
# アカウント管理（マルチアカウント / 永続化）
# ============================================================


def _read_json(path):
    """共有ファイルを読む。他のBOTが置き換えている一瞬に当たると
    PermissionError / 読みかけの JSON になることがあるので少し待って読み直す。"""
    for i in range(10):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            raise
        except (PermissionError, json.JSONDecodeError):
            if i == 9:
                raise
            time.sleep(0.05)


def _atomic_dump(path, data):
    """一時ファイルへ書いてから置換する。

    ここには全BOT共有のPayPayトークンが入っている。直接 open(..., "w") すると
    書いている途中を他のBOTが読んで壊れたJSONを掴んだり、落ちた瞬間に
    ファイルが空のまま残って全BOTが同時に決済不能になる。
    """
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        # 他のBOTが読んでいる最中は置換できない (PermissionError) ので少し待つ
        for i in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if i == 19:
                    raise
                time.sleep(0.05)
        return True
    except Exception as e:
        print(f"[paypay] ❌ 保存失敗 {os.path.basename(path)}: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def load_accounts():
    try:
        return _read_json(ACCOUNTS_FILE)
    except FileNotFoundError:
        return {"active": None, "accounts": {}}


def save_accounts(data):
    return _atomic_dump(ACCOUNTS_FILE, data)


def load_tokens():
    accounts = load_accounts()
    active_phone = accounts.get("active")
    if active_phone and active_phone in accounts.get("accounts", {}):
        return accounts["accounts"][active_phone]
    try:
        return _read_json(TOKEN_FILE)
    except FileNotFoundError:
        return None


def save_tokens(access_token, refresh_token, phone="", device_uuid=""):
    accounts = load_accounts()
    if not phone:
        phone = accounts.get("active") or "unknown"
    account_data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "phone": phone,
        "device_uuid": device_uuid or HEADERS.get("Device-UUID", ""),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    accounts.setdefault("accounts", {})[phone] = account_data
    if not accounts.get("active"):
        accounts["active"] = phone
    save_accounts(accounts)
    _atomic_dump(TOKEN_FILE, account_data)
    print(f"[+] トークン保存: {ACCOUNTS_FILE} (phone={phone})")


def switch_account(phone):
    accounts = load_accounts()
    if phone not in accounts.get("accounts", {}):
        print(f"[!] アカウント {phone} が見つかりません")
        print(f"    登録済み: {list(accounts.get('accounts', {}).keys())}")
        return False
    accounts["active"] = phone
    save_accounts(accounts)
    _atomic_dump(TOKEN_FILE, accounts["accounts"][phone])
    print(f"[+] アクティブアカウント切替: {phone}")
    return True


# ============================================================
# UDID-電話番号 紐付け管理 (paypay_udid_map.json)
# ============================================================

def load_udid_map():
    """UDID-電話番号紐付けマップを読み込み
    構造: {"phone_to_udid": {"090xxxx": "uuid-..."}, "udid_to_phone": {"uuid-...": "090xxxx"}}"""
    try:
        return _read_json(UDID_MAP_FILE)
    except FileNotFoundError:
        return {"phone_to_udid": {}, "udid_to_phone": {}}


def save_udid_map(data):
    _atomic_dump(UDID_MAP_FILE, data)


def generate_new_udid():
    """新しいランダムUDID (UUID v4) を生成"""
    return str(uuid4())


def get_udid_for_phone(phone, new_device=False):
    """電話番号に対応するUDIDを取得
    - new_device=True: 新規UDID生成して紐付け保存
    - new_device=False: 既存紐付けがあればそれを使う、なければ新規生成して保存
    戻り値: (udid, is_new)"""
    udid_map = load_udid_map()
    p2u = udid_map.setdefault("phone_to_udid", {})
    u2p = udid_map.setdefault("udid_to_phone", {})

    existing_udid = p2u.get(phone)
    if existing_udid and not new_device:
        print(f"[+] 既存UDID使用: phone={phone} → udid={existing_udid[:16]}...")
        return existing_udid, False

    # 新規生成
    new_udid = generate_new_udid()
    p2u[phone] = new_udid
    u2p[new_udid] = phone
    save_udid_map(udid_map)
    print(f"[+] 新規UDID生成: phone={phone} → udid={new_udid[:16]}...")
    return new_udid, True


def get_phone_for_udid(udid):
    """UDIDから電話番号を逆引き"""
    udid_map = load_udid_map()
    return udid_map.get("udid_to_phone", {}).get(udid)


# ============================================================
# WAF突破 (Playwright)
# ============================================================


def get_waf_token():
    print("[*] Step 0: WAF突破 (Playwright headless)...")
    cookies = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA)
        page = ctx.new_page()
        page.goto(
            f"{BASE_PORTAL}/portal/oauth2/sign-in?client_id={CLIENT_ID}",
            wait_until="networkidle",
            timeout=30000,
        )
        page.wait_for_timeout(3000)
        for c in ctx.cookies():
            cookies[c["name"]] = c["value"]
        browser.close()
    print(f"[+] WAF: {cookies.get('aws-waf-token', 'MISSING')[:40]}...")
    cookies["OA2_last_method"] = "mobile"
    cookies["Lang"] = "ja"
    return cookies


# ============================================================
# OAuth2フロー
# ============================================================

def do_par(session, cookies, headers):
    print("\n[*] Step 1: PAR...")
    cv = _b64url(secrets.token_bytes(32))
    cc = _b64url(hashlib.sha256(cv.encode()).digest())
    state = _b64url(secrets.token_bytes(32))
    r = session.post(
        f"{BASE_APP4}/bff/v2/oauth2/par",
        params={"payPayLang": "ja"},
        data={
            "clientId": CLIENT_ID, "clientAppVersion": "5.55.0",
            "clientOsVersion": "36.0.0", "clientOsType": "ANDROID",
            "redirectUri": "paypay://oauth2/callback", "responseType": "code",
            "state": state, "codeChallenge": cc, "codeChallengeMethod": "S256",
            "scope": "REGULAR", "tokenVersion": "v2", "prompt": "", "uiLocales": "ja",
        },
        headers=headers,
    )
    update_cookies(cookies, r)
    request_uri = r.json()["payload"]["requestUri"]
    print(f"[+] PAR: {request_uri}")
    return cv, cc, state, request_uri


def do_authorize(session, cookies, request_uri, headers):
    print("\n[*] Step 2: authorize...")
    r = session.get(
        f"{BASE_PORTAL}/portal/api/v2/oauth2/authorize",
        params={"client_id": CLIENT_ID, "request_uri": request_uri},
        headers={**headers, "Cookie": cookie_str(cookies)},
    )
    print(f"[+] authorize: {r.status_code}")
    update_cookies(cookies, r)
    if r.status_code == 302:
        location = r.headers.get("location", "")
        if location.startswith("/"):
            location = f"{BASE_PORTAL}{location}"
        r_page = session.get(location, headers={**headers, "Cookie": cookie_str(cookies)})
        update_cookies(cookies, r_page)
    print("[*] par/check...")
    r_check = session.get(
        f"{BASE_PORTAL}/portal/api/v2/oauth2/par/check",
        headers={**headers, "Cookie": cookie_str(cookies)},
    )
    print(f"[+] par/check: {r_check.status_code} {r_check.json().get('header', {}).get('resultCode')}")
    update_cookies(cookies, r_check)


def do_password(session, cookies, phone, password, headers):
    """device_uuid既知→redirectUrlにcode、未知→OTP必要"""
    print("\n[*] Step 3: sign-in/password...")
    r = session.post(
        f"{BASE_PORTAL}/portal/api/v2/oauth2/sign-in/password",
        json={"username": phone, "password": password, "signInAttemptCount": 1},
        headers={
            **headers, "Content-Type": "application/json",
            "Origin": BASE_PORTAL, "Cookie": cookie_str(cookies),
            "Referer": f"{BASE_PORTAL}/portal/oauth2/sign-in-with-password?client_id={CLIENT_ID}&mode=navigation-notitle",
        },
    )
    data = r.json()
    update_cookies(cookies, r)
    header = data.get("header", {})
    result = header.get("resultCode", "")
    print(f"[+] password: status={r.status_code} result={result}")
    if result != "S0000":
        print(f"[!] 失敗: {header.get('resultMessage', '?')}")
        return None

    payload = data.get("payload", {}) or {}
    redirect_url = payload.get("redirectUrl") or deep_find(data, "redirectUrl", "redirect_url")
    if redirect_url:
        code = extract_code_from_url(redirect_url)
        if code:
            print(f"[+] OTP不要! redirectUrlからcode取得: {code[:30]}...")
            return {"otp_required": False, "code": code}

    otp_ref = deep_find(data, "otpReferenceId", "otp_reference_id", "otpId", "referenceId")
    otp_prefix = deep_find(data, "otpPrefix", "otp_prefix", "prefix")
    print(f"[+] OTP必要 (device_uuid未知)")
    return {"otp_required": True, "otp_ref": otp_ref, "otp_prefix": otp_prefix}


def trigger_sms_otp(session, cookies, headers):
    """SELECT_FLOW → next-action-polling でSMS送信
    友人ライブラリ準拠版: flow:OTL追加 / Refererをステップごとに更新 / Sec-Fetch追加 / 厳密チェック"""
    print("\n[*] Step 4: SMS OTP トリガー...")
    base_hdrs = {
        **headers, "Content-Type": "application/json",
        "Origin": BASE_PORTAL, "Cookie": cookie_str(cookies),
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Requested-With": "jp.ne.paypay.android.app",
    }

    def _check_result(stage, r):
        update_cookies(cookies, r)
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"{stage} JSON解釈失敗: {r.text[:300]}")
        result = data.get("header", {}).get("resultCode", "?")
        msg = data.get("header", {}).get("resultMessage", "")
        print(f"    result={result}")
        if result != "S0000":
            raise RuntimeError(f"{stage}失敗 (resultCode={result}): {msg}")
        return data

    # 4a: code-grant/update (空ボディ) — Referer: sign-in landing
    print("  [4a] code-grant/update (空ボディ)...")
    hdrs = {**base_hdrs, "Referer": f"{BASE_PORTAL}/portal/oauth2/sign-in?client_id={CLIENT_ID}&mode=landing"}
    r = session.post(
        f"{BASE_PORTAL}/portal/api/v2/oauth2/extension/code-grant/update",
        json={}, headers=hdrs,
    )
    _check_result("code-grant/update(初期)", r)

    # 4b: SELECT_FLOW — Referer: verification-method, flow:OTL追加
    print("  [4b] SELECT_FLOW (flow=OTL, sign_in_method=MOBILE)...")
    hdrs = {**base_hdrs, "Referer": f"{BASE_PORTAL}/portal/oauth2/verification-method?client_id={CLIENT_ID}&mode=navigation-2fa"}
    r = session.post(
        f"{BASE_PORTAL}/portal/api/v2/oauth2/extension/code-grant/update",
        json={"params": {"extension_id": "user-main-2fa-v1", "data": {"type": "SELECT_FLOW",
            "payload": {
                "flow": "OTL",
                "sign_in_method": "MOBILE",
                "base_url": "https://www.paypay.ne.jp/portal/oauth2/l",
            }}}},
        headers=hdrs,
    )
    _check_result("SELECT_FLOW", r)

    # 4c: next-action-polling — Referer: otl-request
    print("  [4c] next-action-polling...")
    hdrs = {**base_hdrs, "Referer": f"{BASE_PORTAL}/portal/oauth2/otl-request?client_id={CLIENT_ID}&mode=navigation-2fa"}
    r = session.post(
        f"{BASE_PORTAL}/portal/api/v2/oauth2/extension/code-grant/side-channel/next-action-polling",
        json={"waitUntil": "PT5S"}, headers=hdrs,
    )
    _check_result("next-action-polling", r)
    print("  [+] SMS OTP送信完了 📩")


def verify_otp_and_get_code(session, cookies, otp, otp_info, headers):
    """OTLフローでcode取得 (paypay_auth.py準拠の堅牢版)
    GET OTLリンク → otl/verify(複数パターン) → COMPLETE_OTL(複数パターン) → next-action-polling"""
    print("\n[*] Step 5: OTL検証 + code取得...")
    hdrs = {
        **headers,
        "Content-Type": "application/json",
        "Origin": BASE_PORTAL,
        "Cookie": cookie_str(cookies),
        "Referer": f"{BASE_PORTAL}/portal/oauth2/sign-in?client_id={CLIENT_ID}",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Requested-With": "jp.ne.paypay.android.app",
    }
    otp_ref = otp_info.get("otp_ref") or ""
    otp_prefix = str(otp_info.get("otp_prefix") or "")

    # 5a: OTLリンクにGETアクセス (リンクを「クリック」してOTLを消費)
    print("  [5a] OTLリンクアクセス (GET)...")
    otl_url = f"{BASE_PORTAL}/portal/oauth2/l?id={otp}"
    r_get = session.get(
        otl_url,
        headers={
            **headers, "Cookie": cookie_str(cookies),
            "Referer": f"{BASE_PORTAL}/portal/oauth2/otl-request?client_id={CLIENT_ID}&mode=navigation-2fa",
        },
    )
    update_cookies(cookies, r_get)
    print(f"    GET {otl_url} → {r_get.status_code}")
    # リダイレクトを追跡
    if r_get.status_code in (301, 302, 303, 307, 308):
        location = r_get.headers.get("location", "")
        print(f"    redirect → {location[:200]}")
        if location.startswith("/"):
            location = f"{BASE_PORTAL}{location}"
        r_redirect = session.get(location, headers={**headers, "Cookie": cookie_str(cookies)})
        update_cookies(cookies, r_redirect)
        print(f"    redirect result → {r_redirect.status_code}")

    # 5a-2: otl/verify — 複数のペイロードパターンを試行
    print("  [5a-2] otl/verify...")
    verify_bodies = [
        {"code": otp}, {"otp": otp},
        {"code": otp, "otpReferenceId": otp_ref},
        {"otp": otp, "otpReferenceId": otp_ref, "otpPrefix": otp_prefix},
    ]
    verified = False
    verify_response_data = None
    for i, body in enumerate(verify_bodies):
        r = session.post(
            f"{BASE_PORTAL}/portal/api/v2/oauth2/extension/sign-in/2fa/otl/verify",
            json=body, headers=hdrs,
        )
        try:
            data = r.json()
            update_cookies(cookies, r)
            result = data.get("header", {}).get("resultCode", "")
            print(f"    [try {i+1}] result={result}")
            print(f"    [try {i+1}] full response: {json.dumps(data, ensure_ascii=False)[:800]}")
            if result == "S0000":
                verified = True
                verify_response_data = data
                break
        except Exception:
            print(f"    [try {i+1}] raw: {r.text[:200]}")
    if not verified:
        print("[!] OTP検証失敗 — verifyなしでCOMPLETE_OTLを試みます")

    # 5b: COMPLETE_OTL — 複数のペイロードパターンを試行
    print("  [5b] COMPLETE_OTL...")

    # verifyレスポンスから追加情報を抽出
    verify_payload = verify_response_data.get("payload", {}) if verify_response_data else {}
    print(f"    verify payload: {json.dumps(verify_payload, ensure_ascii=False)[:500]}")

    complete_bodies = [
        # パターン1: code=otp
        {"params": {"extension_id": "user-main-2fa-v1", "data": {"type": "COMPLETE_OTL",
            "payload": {"code": otp}}}},
        # パターン2: otlId を使用
        {"params": {"extension_id": "user-main-2fa-v1", "data": {"type": "COMPLETE_OTL",
            "payload": {"otlId": otp, "code": otp}}}},
        # パターン3: payload=None
        {"params": {"extension_id": "user-main-2fa-v1", "data": {"type": "COMPLETE_OTL",
            "payload": None}}},
        # パターン4: payload空dict
        {"params": {"extension_id": "user-main-2fa-v1", "data": {"type": "COMPLETE_OTL",
            "payload": {}}}},
        # パターン5: linkIdを使用
        {"params": {"extension_id": "user-main-2fa-v1", "data": {"type": "COMPLETE_OTL",
            "payload": {"linkId": otp}}}},
        # パターン6: COMPLETE
        {"params": {"extension_id": "user-main-2fa-v1", "data": {"type": "COMPLETE",
            "payload": {"code": otp}}}},
        # パターン7: OTL_COMPLETE
        {"params": {"extension_id": "user-main-2fa-v1", "data": {"type": "OTL_COMPLETE",
            "payload": {"code": otp}}}},
    ]

    for i, body in enumerate(complete_bodies):
        type_val = body["params"]["data"]["type"]
        print(f"    [try {i+1}] type={type_val}")
        r = session.post(
            f"{BASE_PORTAL}/portal/api/v2/oauth2/extension/code-grant/update",
            json=body, headers=hdrs,
        )
        try:
            data = r.json()
            update_cookies(cookies, r)
            result = data.get("header", {}).get("resultCode", "?")
            msg = data.get("header", {}).get("resultMessage", "")
            print(f"    result={result} msg={msg}")
            print(f"    full response: {json.dumps(data, ensure_ascii=False)[:800]}")
            # code取得を試みる (payload.code または redirect_uri)
            payload = data.get("payload", {}) or {}
            code = payload.get("code") or deep_find(data, "code")
            if code:
                print(f"    [+] code取得: {code[:30]}...")
                return code
            redirect_uri = payload.get("redirect_uri") or payload.get("redirectUri") or ""
            if redirect_uri:
                code = extract_code_from_url(redirect_uri)
                if code:
                    print(f"    [+] redirect_uriからcode取得: {code[:30]}...")
                    return code
                if "code=" in redirect_uri:
                    code_part = redirect_uri.split("code=")[1].split("&")[0]
                    print(f"    [+] code取得(直接パース): {code_part[:30]}...")
                    return code_part
            # S0000でもcodeがない場合は次へ
            if result == "S0000":
                print(f"    S0000だがcodeなし → pollingへ")
                break
        except Exception:
            print(f"    raw: {r.text[:200]}")
        time.sleep(0.5)

    # 5c: next-action-polling でcode取得
    print("  [5c] next-action-polling でcode取得...")
    for attempt in range(10):
        r = session.post(
            f"{BASE_PORTAL}/portal/api/v2/oauth2/extension/code-grant/side-channel/next-action-polling",
            json={"waitUntil": "PT5S"}, headers=hdrs,
        )
        try:
            data = r.json()
            update_cookies(cookies, r)
            payload = data.get("payload", {}) or {}
            result = data.get("header", {}).get("resultCode", "?")
            print(f"    [poll {attempt+1}/10] result={result}")
            print(f"    payload: {json.dumps(payload, ensure_ascii=False)[:500]}")
            code = payload.get("code") or deep_find(data, "code")
            if code:
                print(f"    [+] code取得: {code[:30]}...")
                return code
            redirect_url = payload.get("redirectUrl") or payload.get("redirect_uri") or deep_find(data, "redirectUrl")
            if redirect_url:
                code = extract_code_from_url(redirect_url)
                if code:
                    print(f"    [+] redirectUrlからcode取得: {code[:30]}...")
                    return code
                if "code=" in redirect_url:
                    code_part = redirect_url.split("code=")[1].split("&")[0]
                    print(f"    [+] code取得(直接パース): {code_part[:30]}...")
                    return code_part
        except Exception:
            print(f"    raw: {r.text[:200]}")
        time.sleep(1)

    print("[!] code取得失敗")
    return None


def exchange_token(session, cv, code, headers):
    print("\n[*] Step 6: token交換...")
    r = session.post(
        f"{BASE_APP4}/bff/v2/oauth2/token",
        params={"payPayLang": "ja"},
        data={
            "clientId": CLIENT_ID, "grantType": "authorization_code",
            "code": code, "codeVerifier": cv, "redirectUri": "paypay://oauth2/callback",
        },
        headers=headers,
    )
    data = r.json()
    payload = data.get("payload", {}) or {}
    access_token = payload.get("accessToken") or data.get("access_token")
    refresh_token = payload.get("refreshToken") or data.get("refresh_token")
    if access_token:
        print(f"[+] accessToken: {access_token[:40]}...")
        print(f"[+] refreshToken: {refresh_token[:40] if refresh_token else 'NONE'}...")
    else:
        print(f"[!] 失敗: {json.dumps(data, ensure_ascii=False)[:500]}")
    return access_token, refresh_token


# ============================================================
# refresh / verify / alive / balance
# ============================================================


def refresh_access_token(refresh_token, device_uuid=None):
    print(f"\n[*] refresh_token更新...")
    headers = make_headers(device_uuid)
    session = httpx.Client(follow_redirects=False, timeout=30.0)
    r = session.post(
        f"{BASE_APP4}/bff/v2/oauth2/refresh",
        params={"payPayLang": "ja"},
        data={
            "clientId": CLIENT_ID,
            "grantType": "refresh_token",
            "refreshToken": refresh_token,
            "code": refresh_token,
            "redirectUri": "paypay://oauth2/callback",
        },
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        data = r.json()
    except Exception:
        print(f"[!] JSON失敗: {r.text[:500]}")
        return None, None
    payload = data.get("payload", {}) or {}
    at = payload.get("accessToken") or data.get("access_token")
    if at:
        rt = payload.get("refreshToken") or data.get("refresh_token") or refresh_token
        print(f"[+] accessToken: {at[:40]}...")
        return at, rt
    print(f"[!] 失敗: {json.dumps(data, ensure_ascii=False)[:500]}")
    return None, None


def _api_session(access_token, device_uuid=None):
    headers = make_headers(device_uuid)
    hdrs = {**headers, "Authorization": f"Bearer {access_token}"}
    return httpx.Client(follow_redirects=False, timeout=30.0), hdrs


def get_balance_info(access_token, device_uuid=None):
    """GET /bff/v1/getBalanceInfo → 残高・ポイント
    PayPaython-mobile ライブラリ準拠 (パラメータ追加 / cashBackBalanceInfo使用)"""
    print(f"\n[*] getBalanceInfo...")
    session, hdrs = _api_session(access_token, device_uuid)
    # ライブラリ準拠のパラメータ
    params = {
        "includePendingBonusLite": "false",
        "includePending": "true",
        "noCache": "true",
        "includeKycInfo": "true",
        "includePayPaySecuritiesInfo": "true",
        "includePointInvestmentInfo": "true",
        "includePayPayBankInfo": "true",
        "includeGiftVoucherInfo": "true",
        "payPayLang": "ja",
    }
    r = session.get(f"{BASE_APP4}/bff/v1/getBalanceInfo", params=params, headers=hdrs)
    print(f"    status={r.status_code}")
    try:
        data = r.json()
        print(f"    [DEBUG] full_response={json.dumps(data, ensure_ascii=False)[:800]}")
        if data.get("header", {}).get("resultCode") == "S0000":
            payload = data.get("payload", {})
            ws = payload.get("walletSummary", {}) or {}
            wd = payload.get("walletDetail", {}) or {}

            def _bal(v):
                """{balance: N, currency: ...} または数値から金額を取得"""
                if isinstance(v, dict):
                    return v.get("balance", 0) or 0
                if isinstance(v, (int, float)):
                    return v
                return 0

            # ライブラリ準拠: points は cashBackBalanceInfo から取得 (フォールバック: totalPayPayPointsInfo)
            points = _bal(wd.get("cashBackBalanceInfo"))
            if not points:
                points = _bal(wd.get("totalPayPayPointsInfo"))

            result = {
                "money": _bal(wd.get("emoneyBalanceInfo")),
                "money_light": _bal(wd.get("prepaidBalanceInfo")),
                "all_balance": _bal(ws.get("allTotalBalanceInfo")),
                "useable_balance": _bal(ws.get("usableBalanceInfoWithoutCashback")),
                "points": points,
            }
            print(f"    [+] parsed: {result}")
            return result
        print(f"    resultCode={data.get('header', {}).get('resultCode', '?')}")
        print(f"    message={data.get('header', {}).get('resultMessage', '?')}")
    except Exception as e:
        print(f"    [!] JSONパース例外: {type(e).__name__}: {e}")
        print(f"    raw: {r.text[:500]}")
    return None


def get_payment_history(access_token, device_uuid=None, order_types=None, page_token=None, page_size=20):
    """GET /bff/v3/getPaymentHistory → 取引履歴
    PayPaython-mobile ライブラリ準拠 (必須パラメータ追加)"""
    print(f"\n[*] getPaymentHistory...")
    session, hdrs = _api_session(access_token, device_uuid)
    # ライブラリ準拠のパラメータ
    params = {
        "pageSize": str(page_size),
        "orderTypes": "",
        "paymentMethodTypes": "",
        "signUpCompletedAt": "2021-01-02T10:16:24Z",
        "isOverdraftOnly": "false",
        "payPayLang": "ja",
    }
    if order_types:
        params["orderTypes"] = ",".join(order_types)
    if page_token:
        # APIが期待するパラメータ名は "nextPageKey" (レスポンスのフィールド名と同じ)
        params["nextPageKey"] = page_token
    r = session.get(f"{BASE_APP4}/bff/v3/getPaymentHistory", params=params, headers=hdrs)
    print(f"    status={r.status_code}")
    print(f"    [DEBUG] params={json.dumps(params, ensure_ascii=False)}")
    try:
        data = r.json()
        print(f"    [DEBUG] full_response={json.dumps(data, ensure_ascii=False)[:1200]}")
        if data.get("header", {}).get("resultCode") == "S0000":
            payload = data.get("payload", {})
            items = payload.get("paymentInfoList", [])
            print(f"    [+] paymentInfoList: {len(items)}件, hasNextPage: {payload.get('hasNextPage')}, nextPageKey: {payload.get('nextPageKey')}")
            # ページネーション検証: page_token指定時に同じデータが返ってきた場合の警告
            if page_token and items:
                print(f"    [DEBUG] page_token使用時: 最初のorderId={items[0].get('orderId', '?')}")
            return payload
        print(f"    resultCode={data.get('header', {}).get('resultCode', '?')}")
        print(f"    message={data.get('header', {}).get('resultMessage', '?')}")
    except Exception as e:
        print(f"    [!] JSONパース例外: {type(e).__name__}: {e}")
        print(f"    raw: {r.text[:500]}")
    return None


# ============================================================
# 取引履歴パース・フォーマット
# ============================================================


INCOMING_ORDER_TYPES = {
    "CASHBACK", "P2P_CODE_RECEPTION", "INTEREST", "P2PRECEIVE",
    "RECEIVE", "CAMPAIGN_REWARD", "POINT_CONVERSION", "GIFT",
}

# 出金(支払)系のorderType
OUTGOING_ORDER_TYPES = {
    "P2PSEND", "PAYMENT", "TOPUP", "WITHDRAW", "BANK_TRANSFER",
    "DONATION", "PURCHASE", "SEND", "TOPUP_BANK",
}

# orderType → 日本語ラベル
ORDER_TYPE_LABELS = {
    "P2PSEND": "送金",
    "P2P_CODE_RECEPTION": "QR受取",
    "CASHBACK": "キャッシュバック",
    "PAYMENT": "支払い",
    "TOPUP": "チャージ",
    "WITHDRAW": "出金",
    "BANK_TRANSFER": "銀行送金",
    "DONATION": "寄付",
    "INTEREST": "利息",
    "PURCHASE": "購入",
    "GIFT": "ギフト",
    "CAMPAIGN_REWARD": "キャンペーン",
    "POINT_CONVERSION": "ポイント交換",
}

# orderStatus → 絵文字
ORDER_STATUS_EMOJI = {
    "PENDING": "🟡",
    "COMPLETED": "✅",
    "RECEIVED": "✅",
    "REJECTED": "❌",
    "CANCELED": "🚫",
    "EXPIRED": "⏰",
    "FAILED": "⚠️",
    "SUCCESS": "✅",
    "REFUNDED": "💸",
}


def _is_incoming(order_type):
    """取引タイプが入金(受取)かどうかを判定"""
    if not order_type:
        return False
    ot = str(order_type).upper()
    if ot in INCOMING_ORDER_TYPES:
        return True
    if ot in OUTGOING_ORDER_TYPES:
        return False
    # 未知のタイプはキーワードで推測
    return any(k in ot for k in ("RECEIVE", "CASHBACK", "INTEREST", "REWARD", "GIFT"))


def _parse_datetime_jst(dt_str):
    """ISO 8601 UTC → JST短縮文字列 (例: 2026-06-30T01:09:29Z → 06/30 10:09)"""
    if not dt_str:
        return "?"
    try:
        dt = datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
        dt_jst = dt.astimezone(JST)
        return dt_jst.strftime("%m/%d %H:%M")
    except Exception:
        return str(dt_str)[:16]


def _normalize_paypay_link(value):
    """値を正規PayPay請求リンクURLに正規化 (QR形式)
    - 二重プレフィックス (https://pay.paypay.ne.jp/https://qr.paypay.ne.jp/xxx) を解消
    - 既にフルURL (qr/pay.paypay.ne.jp) ならコードを抽出して正規化
    - コードのみ (p2p01_xxx 等) なら https://qr.paypay.ne.jp/ を付与
    - 空文字/None は空文字を返す"""
    if not value:
        return ""
    value = str(value).strip()
    # 再帰的にプレフィックスを削除 (二重/三重プレフィックス対策)
    changed = True
    while changed:
        changed = False
        for host in (
            "https://qr.paypay.ne.jp/", "http://qr.paypay.ne.jp/",
            "https://pay.paypay.ne.jp/", "http://pay.paypay.ne.jp/",
            "https://www.paypay.ne.jp/", "http://www.paypay.ne.jp/",
            "qr.paypay.ne.jp/", "pay.paypay.ne.jp/", "www.paypay.ne.jp/",
        ):
            if value.lower().startswith(host.lower()):
                value = value[len(host):]
                changed = True
                break
    # 残りが http/https URL ならそのまま (予期せぬURLの安全策)
    if value.startswith("http://") or value.startswith("https://"):
        return value
    # コードのみ → qr.paypay.ne.jp を付与 (請求QRの正しいホスト)
    return f"https://qr.paypay.ne.jp/{value}"


def _extract_verification_code(value):
    """PayPayリンクURLまたはコードからverificationCodeを抽出
    例: https://qr.paypay.ne.jp/p2p01_xxx → p2p01_xxx
        https://pay.paypay.ne.jp/p2p01_xxx → p2p01_xxx
        https://pay.paypay.ne.jp/https://qr.paypay.ne.jp/p2p01_xxx → p2p01_xxx (二重プレフィックス解消)
        p2p01_xxx → p2p01_xxx"""
    if not value:
        return ""
    value = str(value).strip()
    # 再帰的にプレフィックスを削除 (二重/三重プレフィックス対策)
    changed = True
    while changed:
        changed = False
        for host in (
            "https://qr.paypay.ne.jp/", "http://qr.paypay.ne.jp/",
            "https://pay.paypay.ne.jp/", "http://pay.paypay.ne.jp/",
            "https://www.paypay.ne.jp/", "http://www.paypay.ne.jp/",
            "qr.paypay.ne.jp/", "pay.paypay.ne.jp/", "www.paypay.ne.jp/",
        ):
            if value.lower().startswith(host.lower()):
                value = value[len(host):]
                changed = True
                break
    return value


def create_p2p_code(access_token, device_uuid=None, amount=0, session_id=None):
    """POST /bff/v1/createP2PCode → 請求QRコード生成
    レスポンス構造はSEND系(executeP2PSendMoneyLink)と同じで payload.link が含まれる"""
    print(f"\n[*] createP2PCode (amount=¥{amount:,})...")
    session, hdrs = _api_session(access_token, device_uuid)
    body = {
        "amount": amount,
        "sessionId": session_id or str(uuid4()),
    }
    r = session.post(
        f"{BASE_APP4}/bff/v1/createP2PCode",
        params={"payPayLang": "ja"},
        json=body,
        headers={**hdrs, "Content-Type": "application/json"},
    )
    print(f"    status={r.status_code}")
    try:
        data = r.json()
        print(f"    [DEBUG] full_response={json.dumps(data, ensure_ascii=False)[:800]}")
        payload = data.get("payload", {}) or {}
        result_code = data.get("header", {}).get("resultCode", "?")
        # link, url, p2pLink, p2pCode, verificationCode を幅広くチェック → 正規化
        raw_link = (
            payload.get("link")
            or payload.get("url")
            or payload.get("p2pLink")
            or payload.get("p2pCode")
            or payload.get("verificationCode")
            or ""
        )
        link = _normalize_paypay_link(raw_link)
        if link:
            print(f"    [+] link取得成功 (resultCode={result_code}): {link[:80]}...")
            payload["link"] = link
            # verificationCode / p2pCode もコードのみに正規化して格納 (二重プレフィックス防止)
            vc_only = _extract_verification_code(raw_link)
            if vc_only:
                payload["verificationCode"] = vc_only
                payload["p2pCode"] = vc_only
            return payload
        print(f"    resultCode={result_code}")
        print(f"    message={data.get('header', {}).get('resultMessage', '?')}")
        print(f"    raw_payload={json.dumps(payload, ensure_ascii=False)[:400]}")
    except Exception as e:
        print(f"    [!] JSONパース例外: {type(e).__name__}: {e}")
        print(f"    raw: {r.text[:500]}")
    return None


# ============================================================
# 決済検証 (取引番号照合) — 新決済フローの中核
# ============================================================

def verify_payment(access_token, device_uuid, expected_amount, order_id,
                   allow_status=None, max_pages=3):
    """ユーザーが入力した取引番号(order_id)を取引履歴から照合する。

    Args:
        access_token: bot(受取側)アカウントのaccess_token
        device_uuid:  同アカウントのdevice_uuid
        expected_amount: 期待する金額(円)
        order_id: ユーザーが入力した取引番号
        allow_status: 許可するorderStatusの集合 (省略時 COMPLETED/RECEIVED/SUCCESS)
        max_pages: 遡る履歴ページ数

    Returns:
        (ok: bool, message: str, detail: dict|None)
    """
    if allow_status is None:
        allow_status = {"COMPLETED", "RECEIVED", "SUCCESS"}
    order_id = (order_id or "").strip()
    if not order_id:
        return False, "取引番号が空です。", None

    page_token = None
    checked = 0
    for _page in range(max_pages):
        payload = get_payment_history(access_token, device_uuid,
                                      page_token=page_token, page_size=20)
        if payload is None:
            return False, "取引履歴の取得に失敗しました (通信エラー/トークン失効の可能性)。", None
        items = payload.get("paymentInfoList", []) or []
        for item in items:
            checked += 1
            cand_ids = [
                str(item.get("orderId") or ""),
                str(item.get("subOrderId") or ""),
                str(item.get("transactionId") or ""),
            ]
            cand_ids = [c for c in cand_ids if c]
            # ★完全一致のみ。前方一致を許すと、本物の取引番号に1文字足した
            #   別文字列でも照合が通ってしまう。呼び出し側は「ユーザーが入力した
            #   文字列」を使い回し防止キーにしているため、1文字違えば未使用扱いに
            #   なり、同じ入金を何度でも売上として計上できてしまう。
            #   (前方6文字だけ打って他人の入金に相乗りすることも可能だった)
            matched = any(order_id == cid for cid in cand_ids)
            if not matched:
                continue

            amount = item.get("totalAmount") or item.get("amount") or 0
            try:
                amount = abs(int(amount))
            except Exception:
                amount = 0
            ot = str(item.get("orderType", "")).upper()
            status = str(item.get("orderStatus", "")).upper()
            incoming = _is_incoming(ot)

            if not incoming:
                return False, ("この取引番号は支払い(出金)取引です。"
                               "受取取引の番号を入力してください。"), item
            if status and status not in allow_status:
                return False, ("取引は見つかりましたが、ステータスが未完了です "
                               "(status=" + status + ")。"), item
            if amount != int(expected_amount):
                return False, ("金額が一致しません。必要額: "
                               + format(int(expected_amount), ",") + "円 / この取引: "
                               + format(amount, ",") + "円"), item
            return True, ("支払い確認OK: " + format(amount, ",") + "円"), item

        if not payload.get("hasNextPage"):
            break
        page_token = payload.get("nextPageKey")
        if not page_token:
            break

    return False, ("取引番号が直近の取引履歴(" + str(checked) + "件)に見つかりませんでした。"
                   "番号が正しいか / 支払い先アカウントが正しいか / "
                   "反映待ち(数十秒後に再試行)を確認してください。"), None


def get_active_account():
    """現在アクティブなアカウントの (access_token, device_uuid, phone) を返す。"""
    tok = load_tokens()
    if not tok:
        return None, None, None
    return tok.get("access_token"), tok.get("device_uuid"), tok.get("phone")


def refresh_active_account():
    """アクティブアカウントのトークンをrefreshして保存。成功時 access_token を返す。"""
    tok = load_tokens()
    if not tok:
        return None
    rt = tok.get("refresh_token")
    duid = tok.get("device_uuid")
    phone = tok.get("phone", "")
    if not rt:
        return None
    at, new_rt = refresh_access_token(rt, duid)
    if at:
        save_tokens(at, new_rt or rt, phone, duid)
        return at
    return None


def create_billing_link(amount, access_token=None, device_uuid=None,
                        auto_refresh=True):
    """請求リンクを作成して返す。

    Returns: (link: str|None, message: str)
    """
    if access_token is None or device_uuid is None:
        access_token, device_uuid, _ = get_active_account()
    if not access_token:
        return None, "PayPayアカウントが未ログインです。"
    payload = create_p2p_code(access_token, device_uuid, amount=int(amount))
    if payload and payload.get("link"):
        return payload["link"], "請求リンクを作成しました。"
    if auto_refresh:
        new_at = refresh_active_account()
        if new_at:
            _, duid, _ = get_active_account()
            payload = create_p2p_code(new_at, duid, amount=int(amount))
            if payload and payload.get("link"):
                return payload["link"], "請求リンクを作成しました (トークン更新後)。"
    return None, "請求リンクの作成に失敗しました (トークン失効の可能性)。"


# ============================================================
# ログイン高レベルフロー (bot から呼ぶ)
# ============================================================

def _login_step1_sync(phone, password, device_uuid):
    headers = make_headers(device_uuid)
    session = httpx.Client(follow_redirects=False, timeout=30.0)
    cookies = get_waf_token()
    cv, cc, state, request_uri = do_par(session, cookies, headers)
    do_authorize(session, cookies, request_uri, headers)
    pw_result = do_password(session, cookies, phone, password, headers)
    if not pw_result:
        return {"status": "password_fail", "session": session}
    if not pw_result.get("otp_required", True):
        code = pw_result.get("code")
        if not code:
            return {"status": "code_missing", "session": session}
        access_token, refresh_token = exchange_token(session, cv, code, headers)
        if access_token:
            save_tokens(access_token, refresh_token, phone, device_uuid)
            return {"status": "ok_no_otp", "access_token": access_token,
                    "refresh_token": refresh_token, "session": session}
        return {"status": "token_exchange_fail", "session": session}
    # OTP必要 → SMSトリガー (この中で失敗するとRuntimeError raise)
    trigger_sms_otp(session, cookies, headers)
    return {
        "status": "otp_required",
        "session": session,
        "cookies": cookies,
        "cv": cv,
        "headers": headers,
        "otp_info": pw_result,
    }


def _login_step2_sync(state, otp):
    # OTLリンクURLの場合はidパラメータを抽出 (paypay_auth.py準拠)
    if otp.startswith("http"):
        parsed_otp = urlparse(otp)
        otp_params = parse_qs(parsed_otp.query)
        otl_id = otp_params.get("id", [None])[0]
        if otl_id:
            print(f"[+] OTLリンクからid抽出: {otl_id}")
            otp = otl_id
        else:
            # パスから抽出を試行 (/portal/oauth2/l?id=xxx)
            path_parts = parsed_otp.path.split("/")
            if len(path_parts) > 1 and path_parts[-1]:
                otp = path_parts[-1]
                print(f"[+] パスからid抽出: {otp}")
    code = verify_otp_and_get_code(
        state["session"], state["cookies"], otp, state["otp_info"], state["headers"]
    )
    if not code:
        return {"status": "otp_fail"}
    access_token, refresh_token = exchange_token(
        state["session"], state["cv"], code, state["headers"]
    )
    if not access_token:
        return {"status": "token_exchange_fail"}
    save_tokens(access_token, refresh_token, state["phone"], state["device_uuid"])
    return {"status": "ok", "access_token": access_token, "refresh_token": refresh_token}


# ============================================================
# ログイン状態管理 + アカウント管理ヘルパー (bot から使う)
# ============================================================

import time as _time

# Discord user_id → ログイン中間状態 (step1後、OTP待ち)
_LOGIN_STATE = {}
_LOGIN_STATE_TTL = 600  # 10分


def _cleanup_login_state():
    """期限切れのログイン中間状態を掃除。"""
    now = _time.time()
    expired = [uid for uid, st in _LOGIN_STATE.items()
               if now - st.get("_ts", 0) > _LOGIN_STATE_TTL]
    for uid in expired:
        st = _LOGIN_STATE.pop(uid, None)
        if st and st.get("session"):
            try:
                st["session"].close()
            except Exception:
                pass


def login_start(user_id, phone, password, new_device=False):
    """ログイン開始 (電話+パスワード → SMS送信)。

    Returns: (ok: bool, status: str, message: str)
      status == 'otp_required': OTP入力待ち (login_complete を次に呼ぶ)
      status == 'ok_no_otp':    OTP不要でログイン完了
    """
    _cleanup_login_state()
    device_uuid, _is_new = get_udid_for_phone(phone, new_device=new_device)
    try:
        result = _login_step1_sync(phone, password, device_uuid)
    except RuntimeError as e:
        return False, "sms_fail", "SMS送信に失敗しました: " + str(e)
    except Exception as e:
        return False, "error", "ログイン開始でエラー: " + str(e)

    status = result.get("status")
    if status == "ok_no_otp":
        return True, "ok_no_otp", "ログイン完了 (OTP不要)。"
    if status == "otp_required":
        result["phone"] = phone
        result["device_uuid"] = device_uuid
        result["_ts"] = _time.time()
        _LOGIN_STATE[user_id] = result
        return True, "otp_required", "SMS/OTLの認証コードを入力してください。"
    if status == "password_fail":
        return False, "password_fail", "電話番号またはパスワードが違います。"
    return False, status or "error", "ログインに失敗しました (status=" + str(status) + ")。"


def login_complete(user_id, otp):
    """OTP入力でログイン完了。

    Returns: (ok: bool, message: str)
    """
    _cleanup_login_state()
    state = _LOGIN_STATE.get(user_id)
    if not state:
        return False, "保留中のログインがありません。先にログイン開始してください。"
    try:
        result = _login_step2_sync(state, otp)
    except Exception as e:
        return False, "OTP確定でエラー: " + str(e)
    status = result.get("status")
    # 後始末
    try:
        if state.get("session"):
            state["session"].close()
    except Exception:
        pass
    _LOGIN_STATE.pop(user_id, None)
    if status == "ok":
        return True, "ログイン完了。アカウントを保存しました。"
    if status == "otp_fail":
        return False, "OTP認証に失敗しました (コードが違う/期限切れ)。"
    return False, "ログイン完了に失敗しました (status=" + str(status) + ")。"


def list_account_phones():
    """登録済みアカウントの電話番号リストを返す。"""
    accts = load_accounts()
    return list(accts.get("accounts", {}).keys())


def get_active_phone():
    """現在アクティブなアカウントの電話番号。"""
    return load_accounts().get("active")


def remove_account(phone):
    """指定電話番号のアカウントを削除。Returns: bool"""
    accts = load_accounts()
    if phone not in accts.get("accounts", {}):
        return False
    del accts["accounts"][phone]
    if accts.get("active") == phone:
        remaining = list(accts.get("accounts", {}).keys())
        accts["active"] = remaining[0] if remaining else None
    save_accounts(accts)
    return True


def alive_check(phone=None):
    """指定(または全)アカウントの生存確認。残高照会が通ればalive。

    Returns: dict {phone: (ok: bool, info: str)}
    """
    accts = load_accounts()
    targets = ([phone] if phone else list(accts.get("accounts", {}).keys()))
    results = {}
    for ph in targets:
        acc = accts.get("accounts", {}).get(ph)
        if not acc:
            results[ph] = (False, "未登録")
            continue
        at = acc.get("access_token")
        duid = acc.get("device_uuid")
        try:
            info = get_balance_info(at, duid)
            if info is not None:
                results[ph] = (True, "残高照会OK (all=" + str(info.get("all_balance", "?")) + ")")
            else:
                # トークン失効 → refresh 試行
                rt = acc.get("refresh_token")
                new_at, new_rt = refresh_access_token(rt, duid) if rt else (None, None)
                if new_at:
                    save_tokens(new_at, new_rt or rt, ph, duid)
                    info2 = get_balance_info(new_at, duid)
                    if info2 is not None:
                        results[ph] = (True, "残高照会OK (トークン更新後)")
                    else:
                        results[ph] = (False, "トークン更新したが照会失敗")
                else:
                    results[ph] = (False, "トークン失効 (再ログイン要)")
        except Exception as e:
            results[ph] = (False, "エラー: " + str(e)[:50])
    return results


def is_ready():
    """決済に使えるアクティブアカウントがあるか。"""
    at, _, _ = get_active_account()
    return bool(at)


def lookup_incoming_payment(access_token, device_uuid, order_id,
                            allow_status=None, max_pages=3):
    """取引番号から受取(入金)取引を照会し、金額を返す (金額可変の入金用)。

    verify_payment と違い「期待金額」を指定せず、実際の入金額を取得する。

    Returns:
        (ok: bool, amount: int, message: str, detail: dict|None)
    """
    if allow_status is None:
        allow_status = {"COMPLETED", "RECEIVED", "SUCCESS"}
    order_id = (order_id or "").strip()
    if not order_id:
        return False, 0, "取引番号が空です。", None

    page_token = None
    checked = 0
    for _page in range(max_pages):
        payload = get_payment_history(access_token, device_uuid,
                                      page_token=page_token, page_size=20)
        if payload is None:
            return False, 0, "取引履歴の取得に失敗しました。", None
        items = payload.get("paymentInfoList", []) or []
        for item in items:
            checked += 1
            cand_ids = [str(item.get("orderId") or ""),
                        str(item.get("subOrderId") or ""),
                        str(item.get("transactionId") or "")]
            cand_ids = [c for c in cand_ids if c]
            # ★完全一致のみ (verify_payment と同じ理由。前方一致は使い回し防止を素通りする)
            matched = any(order_id == cid for cid in cand_ids)
            if not matched:
                continue
            amount = item.get("totalAmount") or item.get("amount") or 0
            try:
                amount = abs(int(amount))
            except Exception:
                amount = 0
            ot = str(item.get("orderType", "")).upper()
            status = str(item.get("orderStatus", "")).upper()
            if not _is_incoming(ot):
                return False, 0, "この取引番号は支払い(出金)取引です。受取取引の番号を入力してください。", item
            if status and status not in allow_status:
                return False, 0, ("取引は見つかりましたが未完了です (status=" + status + ")。"), item
            if amount <= 0:
                return False, 0, "取引の金額が取得できませんでした。", item
            return True, amount, ("入金確認OK: " + format(amount, ",") + "円"), item
        if not payload.get("hasNextPage"):
            break
        page_token = payload.get("nextPageKey")
        if not page_token:
            break
    return False, 0, ("取引番号が直近の取引履歴(" + str(checked) + "件)に見つかりませんでした。"), None


# ============================================================
# 収入集計 (今日の収入確認コマンド用)
# ============================================================

def _parse_datetime_obj(dt_str):
    """ISO 8601文字列 → datetime(JST, tz付き)。パース不可なら None。
    (日付フィルタ・集計用。表示用の _parse_datetime_jst とは別)"""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(JST)
    except Exception:
        return None


def _extract_wallet_breakdown(item):
    """1取引から支払い方法の内訳(マネー/マネーライト)を推定して返す。

    PayPayの取引レスポンスは環境によりフィールド名が異なるため、
    複数の候補キーを探索する。判別できない分は 'unknown' に入れる。

    Returns: dict {"money": int, "money_light": int, "point": int, "unknown": int}
    """
    total = item.get("totalAmount") or item.get("amount") or 0
    try:
        total = abs(int(total))
    except Exception:
        total = 0

    money = 0
    money_light = 0
    point = 0
    matched = False

    # 候補1: payMethodInfoList / paymentMethodInfoList のような内訳リスト
    for list_key in ("payMethodInfoList", "paymentMethodInfoList",
                     "walletBreakdown", "amountBreakdown"):
        lst = item.get(list_key)
        if isinstance(lst, list) and lst:
            for pm in lst:
                if not isinstance(pm, dict):
                    continue
                amt = pm.get("amount") or pm.get("value") or 0
                try:
                    amt = abs(int(amt))
                except Exception:
                    amt = 0
                mtype = str(pm.get("walletType") or pm.get("paymentMethodType")
                            or pm.get("type") or "").upper()
                if "LIGHT" in mtype or "PREPAID" in mtype:
                    money_light += amt
                    matched = True
                elif "POINT" in mtype or "CASHBACK" in mtype:
                    point += amt
                    matched = True
                elif "MONEY" in mtype or "BALANCE" in mtype:
                    money += amt
                    matched = True
            if matched:
                break

    # 候補2: 単一フィールドで内訳が入っているケース
    if not matched:
        ml = item.get("prepaidAmount") or item.get("moneyLightAmount")
        mn = item.get("balanceAmount") or item.get("moneyAmount")
        if ml is not None or mn is not None:
            try:
                money_light = abs(int(ml or 0))
                money = abs(int(mn or 0))
                matched = True
            except Exception:
                pass

    unknown = 0
    if not matched:
        # 内訳不明 → 全額を unknown に (合計は取れる)
        unknown = total
    else:
        # 内訳合計が total と合わない場合、差を unknown に
        diff = total - (money + money_light + point)
        if diff > 0:
            unknown = diff

    return {"money": money, "money_light": money_light,
            "point": point, "unknown": unknown}


def summarize_income(access_token, device_uuid, since_jst=None, until_jst=None,
                     max_pages=10):
    """指定期間の受取(入金)取引を集計する。

    Args:
        since_jst / until_jst: datetime (JST, tz付き) で期間指定。省略時は全件。
        max_pages: 遡る履歴ページ数上限

    Returns: dict {
        "total": 合計受取額,
        "count": 取引件数,
        "money": PayPayマネー合計,
        "money_light": PayPayマネーライト合計,
        "point": ポイント合計,
        "unknown": 内訳不明分,
        "items": [各取引の要約],
        "has_breakdown": 内訳が取得できたか(bool),
    }
    """
    total = 0
    count = 0
    money = 0
    money_light = 0
    point = 0
    unknown = 0
    items_out = []
    any_breakdown = False

    page_token = None
    for _page in range(max_pages):
        payload = get_payment_history(access_token, device_uuid,
                                      page_token=page_token, page_size=20)
        if payload is None:
            break
        items = payload.get("paymentInfoList", []) or []
        stop = False
        for item in items:
            ot = str(item.get("orderType", "")).upper()
            if not _is_incoming(ot):
                continue
            # 日付フィルタ
            dt_raw = (item.get("orderReceivedTime") or item.get("completedAt")
                      or item.get("createdAt") or item.get("orderCreatedAt"))
            dt = _parse_datetime_obj(dt_raw) if dt_raw else None
            if since_jst and dt and dt < since_jst:
                # 履歴は新しい順なので、since より古くなったら打ち切り
                stop = True
                break
            if until_jst and dt and dt > until_jst:
                continue

            amt = item.get("totalAmount") or item.get("amount") or 0
            try:
                amt = abs(int(amt))
            except Exception:
                amt = 0

            bd = _extract_wallet_breakdown(item)
            if bd["money"] or bd["money_light"] or bd["point"]:
                any_breakdown = True

            total += amt
            count += 1
            money += bd["money"]
            money_light += bd["money_light"]
            point += bd["point"]
            unknown += bd["unknown"]
            items_out.append({
                "amount": amt,
                "order_id": item.get("orderId") or item.get("subOrderId") or "",
                "datetime": dt.isoformat() if dt else "",
                "order_type": ot,
                "breakdown": bd,
            })
        if stop:
            break
        if not payload.get("hasNextPage"):
            break
        page_token = payload.get("nextPageKey")
        if not page_token:
            break

    return {
        "total": total, "count": count,
        "money": money, "money_light": money_light,
        "point": point, "unknown": unknown,
        "items": items_out, "has_breakdown": any_breakdown,
    }


def summarize_income_today(access_token=None, device_uuid=None):
    """今日(JST)の受取を集計する。アクティブアカウントを使う。"""
    if access_token is None or device_uuid is None:
        access_token, device_uuid, _ = get_active_account()
    if not access_token:
        return None
    now = datetime.now(JST)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return summarize_income(access_token, device_uuid, since_jst=start, until_jst=now)
