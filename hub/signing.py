"""Xaccount-Bot(Worker)側 apps/worker/src/cheat-bridge.ts と同じ署名。

    HMAC-SHA256(secret, timestamp \\n METHOD \\n path+query \\n body) の16進
"""
import hashlib
import hmac


def sign(secret: str, timestamp: str, method: str, target: str, body: str) -> str:
    message = "\n".join([timestamp, method.upper(), target, body])
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
