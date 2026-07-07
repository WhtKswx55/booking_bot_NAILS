"""
webapp_auth.py — проверка подписи initData, которую Telegram передаёт WebApp'у.

Это критично: без проверки кто угодно мог бы слать запросы в API
от имени любой клиентки, подделав tg_id. Алгоритм описан в официальной
документации Telegram (validate data received via the Mini App).
"""
import hashlib
import hmac
from urllib.parse import parse_qsl


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> dict | None:
    """
    Возвращает распарсенные данные (dict) если подпись валидна, иначе None.
    """
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    # опциональная проверка свежести данных
    auth_date = pairs.get("auth_date")
    if auth_date:
        import time
        if time.time() - int(auth_date) > max_age_seconds:
            return None

    return pairs
