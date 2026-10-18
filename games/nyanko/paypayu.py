import aiohttp
import datetime
from useragent_changer import UserAgent

ua =UserAgent('iphone')

# --- send login request ---
async def login(phoneNumber: str, password: str, uuid: str):
    headers = {
        'User-Agent': ua.set(),
        'Accept' : 'application/json, text/plain, */*',
        'Content-Type' : 'application/json',
        'Origin': 'https://www.paypay.ne.jp',
        'Referer':'https://www.paypay.ne.jp/app/account/sign-in',
    }
    payload = {
        "scope":"SIGN_IN",
        "client_uuid":f"{uuid}",
        "grant_type":"password",
        "username":phoneNumber,
        "password":password,
        "add_otp_prefix": True,
        "language":"ja"
    }
    async with aiohttp.ClientSession() as session:
        async with session.post("https://www.paypay.ne.jp/app/v1/oauth/token", headers=headers, json=payload) as login_request_response:
            return await login_request_response.json()

# --- one-time-password authentication ---
async def login_otp(set_uuid,otp,otpid,otp_pre):
    otp_number=otp
    headers = {
        'User-Agent': ua.set(),
        'Accept' : 'application/json, text/plain, */*',
        'Content-Type' : 'application/json',
        'Origin': 'https://www.paypay.ne.jp',
        'Referer':'https://www.paypay.ne.jp/app/account/sign-in',
    }
    payload = {
            "scope":"SIGN_IN",
            "client_uuid":f"{set_uuid}",
            "grant_type":"otp",
            "otp_prefix": str(otp_pre),
            "otp":otp_number,
            "otp_reference_id":otpid,
            "username_type":"MOBILE",
            "language":"ja"
    }
    async with aiohttp.ClientSession() as session:
        async with session.post("https://www.paypay.ne.jp/app/v1/oauth/token", headers=headers, json=payload) as response:
            login_response = await response.json()
            try:
                if login_response["response_type"]=="ErrorResponse":
                    return "ERR"
            except:
                return "OK"

async def check_link(cd):
    if "https://" in cd:
        cd=cd.replace("https://pay.paypay.ne.jp/","")

    headers={
        "Accept":"application/json, text/plain, */*",
        'User-Agent': ua.set(),
        "Content-Type":"application/json"
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"https://www.paypay.ne.jp/app/v2/p2p-api/getP2PLinkInfo?verificationCode={cd}", headers=headers) as response:
                response.raise_for_status()
                link_info = await response.json()
            
        except aiohttp.ClientError as e:
            print(f"API_REQ_EXC: {e}") #debug :)
            return False
    
    result_code = link_info.get("header", {}).get("resultCode")
    if result_code != "S0000":
        # リザルトコードがS0000以外だった場合は基本何かエラー起きてる
        return False

    order_status = link_info.get("payload", {}).get("orderStatus")
    if order_status == "PENDING":
        # 受取待ちだったらlink_infoを返す、じゃなかったら受け取られてるorキャンセルされてるor...からFalse
        return link_info
    else:
        return False
    
async def link_rev(cd: str, phoneNumber: str, password: str, uuid: str,link_password: str = None):
    if "https://" in cd:
        cd=cd.replace("https://pay.paypay.ne.jp/","")
        
    async with aiohttp.ClientSession() as session:
        base_headers = {
            "Accept": "application/json, text/plain, */*",
            'User-Agent': ua.set(),
            "Content-Type": "application/json"
        }
        
        try:
            async with session.get(f"https://www.paypay.ne.jp/app/v2/p2p-api/getP2PLinkInfo?verificationCode={cd}", headers=base_headers) as response:
                response.raise_for_status()
                link_info = await response.json()

            if link_info.get("payload", {}).get("orderStatus") != "PENDING":
                # ここでも受取待ちかチェック、受取待ちじゃなかったら弾く
                return False
            
            if link_info.get("payload", {}).get("pendingP2PInfo", {}).get("isSetPasscode") and link_password is None:
                return False

        except aiohttp.ClientError as e:
            print(f"LINK_REQ_EXC: {e}") #debug :)
            return False
        
        login_payload = {
            "scope":"SIGN_IN",
            "client_uuid":f"{uuid}",
            "grant_type":"password",
            "username":phoneNumber,
            "password":password,
            "add_otp_prefix": True,
            "language":"ja"
            }

        login_headers = {
            'User-Agent': ua.set(),
            'Accept' : 'application/json, text/plain, */*',
            'Content-Type' : 'application/json',
            'Origin': 'https://www.paypay.ne.jp',
            'Referer':'https://pay.paypay.ne.jp/'+cd,
        }

        async with session.post("https://www.paypay.ne.jp/app/v1/oauth/token", headers=login_headers, json=login_payload) as response:
            login_response = await response.json()
            try:
                login_response = (login_response["access_token"])
            except:
                try:
                    login_response["otp_reference_id"]
                    return "LOGINERR"
                except:
                    return "LOGINERR"
        
        receive_payload = {
            "verificationCode":cd,
            "client_uuid":uuid,
            "requestAt":str(datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%dT%H:%M:%S+0900')),
            "requestId":link_info["payload"]["message"]["data"]["requestId"],
            "orderId":link_info["payload"]["message"]["data"]["orderId"],
            "senderMessageId":link_info["payload"]["message"]["messageId"],
            "senderChannelUrl":link_info["payload"]["message"]["chatRoomId"],
            "iosMinimumVersion":"3.45.0",
            "androidMinimumVersion":"3.45.0"
            }
        
        if link_password:
            receive_payload["passcode"]=link_password

        try:
            async with session.post("https://www.paypay.ne.jp/app/v2/p2p-api/acceptP2PSendMoneyLink", json=receive_payload, headers=base_headers) as response:
                response.raise_for_status()
                receive_data = await response.json()

                if receive_data.get("header", {}).get("resultCode") == "S0000":
                    return True
                else:
                    return False

        except aiohttp.ClientError as e:
            print(f"REVERR: {e}") #debug :) 
            return False
    

async def create_send_link(phoneNumber: str, password: str, uuid: str, amount: int, link_password: str = None):
    """PayPay送金リンクを作成"""
    async with aiohttp.ClientSession() as session:
        # ログイン処理
        login_headers = {
            'User-Agent': ua.set(),
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/json',
            'Origin': 'https://www.paypay.ne.jp',
            'Referer': 'https://www.paypay.ne.jp/app/account/sign-in',
        }
        
        login_payload = {
            "scope": "SIGN_IN",
            "client_uuid": f"{uuid}",
            "grant_type": "password",
            "username": phoneNumber,
            "password": password,
            "add_otp_prefix": True,
            "language": "ja"
        }
        
        try:
            # ログイン
            async with session.post("https://www.paypay.ne.jp/app/v1/oauth/token", headers=login_headers, json=login_payload) as response:
                login_response = await response.json()
                
                if login_response.get("response_type") == "ErrorResponse":
                    return {"error": "ログインに失敗しました"}
                
                access_token = login_response.get("access_token")
                if not access_token:
                    return {"error": "アクセストークンの取得に失敗しました"}
            
            # 送金リンク作成
            send_headers = {
                'User-Agent': ua.set(),
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {access_token}',
                'Origin': 'https://www.paypay.ne.jp',
                'Referer': 'https://www.paypay.ne.jp/app/send',
            }
            
            send_payload = {
                "amount": amount,
                "theme": "default",
                "requestAt": str(datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%dT%H:%M:%S+0900')),
                "client_uuid": uuid
            }
            
            if link_password:
                send_payload["passcode"] = link_password
            
            # 送金リンク作成（複数のエンドポイントを試行）
            send_endpoints = [
                "https://www.paypay.ne.jp/app/v2/p2p-api/createP2PSendMoneyLink",
                "https://www.paypay.ne.jp/app/v1/p2p-api/createP2PSendMoneyLink",
                "https://www.paypay.ne.jp/app/v2/bff/createP2PSendMoneyLink",
                "https://www.paypay.ne.jp/app/v1/bff/createP2PSendMoneyLink"
            ]
            
            for endpoint in send_endpoints:
                try:
                    async with session.post(endpoint, headers=send_headers, json=send_payload) as response:
                        if response.status == 200:
                            send_response = await response.json()
                            
                            if send_response.get("header", {}).get("resultCode") == "S0000":
                                verification_code = send_response.get("payload", {}).get("verificationCode")
                                if verification_code:
                                    return {
                                        "link": f"https://pay.paypay.ne.jp/{verification_code}",
                                        "amount": amount,
                                        "has_password": bool(link_password)
                                    }
                        else:
                            print(f"Send endpoint {endpoint} returned status: {response.status}")
                except Exception as e:
                    print(f"Send endpoint {endpoint} failed: {e}")
                    continue
            
            return {"error": "送金リンクの作成に失敗しました（APIエンドポイントが見つかりません）"}
                
        except aiohttp.ClientError as e:
            print(f"SEND_LINK_ERR: {e}")
            return {"error": f"通信エラーが発生しました: {e}"}
        except Exception as e:
            print(f"SEND_LINK_EXCEPTION: {e}")
            return {"error": f"予期しないエラーが発生しました: {e}"}

async def create_request_link(phoneNumber: str, password: str, uuid: str, amount: int = None):
    """PayPay請求リンクを作成"""
    async with aiohttp.ClientSession() as session:
        # ログイン処理
        login_headers = {
            'User-Agent': ua.set(),
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/json',
            'Origin': 'https://www.paypay.ne.jp',
            'Referer': 'https://www.paypay.ne.jp/app/account/sign-in',
        }
        
        login_payload = {
            "scope": "SIGN_IN",
            "client_uuid": f"{uuid}",
            "grant_type": "password",
            "username": phoneNumber,
            "password": password,
            "add_otp_prefix": True,
            "language": "ja"
        }
        
        try:
            # ログイン
            async with session.post("https://www.paypay.ne.jp/app/v1/oauth/token", headers=login_headers, json=login_payload) as response:
                login_response = await response.json()
                
                if login_response.get("response_type") == "ErrorResponse":
                    return {"error": "ログインに失敗しました"}
                
                access_token = login_response.get("access_token")
                if not access_token:
                    return {"error": "アクセストークンの取得に失敗しました"}
            
            # 請求リンク作成
            request_headers = {
                'User-Agent': ua.set(),
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {access_token}',
                'Origin': 'https://www.paypay.ne.jp',
                'Referer': 'https://www.paypay.ne.jp/app/request',
            }
            
            request_payload = {
                "requestAt": str(datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%dT%H:%M:%S+0900')),
                "client_uuid": uuid
            }
            
            if amount:
                request_payload["amount"] = amount
            
            # 請求リンク作成（複数のエンドポイントを試行）
            request_endpoints = [
                "https://www.paypay.ne.jp/app/v2/p2p-api/createP2PRequestMoneyLink",
                "https://www.paypay.ne.jp/app/v1/p2p-api/createP2PRequestMoneyLink",
                "https://www.paypay.ne.jp/app/v2/bff/createP2PRequestMoneyLink",
                "https://www.paypay.ne.jp/app/v1/bff/createP2PRequestMoneyLink"
            ]
            
            for endpoint in request_endpoints:
                try:
                    async with session.post(endpoint, headers=request_headers, json=request_payload) as response:
                        if response.status == 200:
                            request_response = await response.json()
                            
                            if request_response.get("header", {}).get("resultCode") == "S0000":
                                verification_code = request_response.get("payload", {}).get("verificationCode")
                                if verification_code:
                                    return {
                                        "link": f"https://pay.paypay.ne.jp/{verification_code}",
                                        "amount": amount,
                                        "is_flexible": amount is None
                                    }
                        else:
                            print(f"Request endpoint {endpoint} returned status: {response.status}")
                except Exception as e:
                    print(f"Request endpoint {endpoint} failed: {e}")
                    continue
            
            return {"error": "請求リンクの作成に失敗しました（APIエンドポイントが見つかりません）"}
                
        except aiohttp.ClientError as e:
            print(f"REQUEST_LINK_ERR: {e}")
            return {"error": f"通信エラーが発生しました: {e}"}
        except Exception as e:
            print(f"REQUEST_LINK_EXCEPTION: {e}")
            return {"error": f"予期しないエラーが発生しました: {e}"}