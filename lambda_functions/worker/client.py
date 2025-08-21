import os, time, hmac, hashlib, base64, urllib.parse
import requests

BITGET_BASE = os.environ.get("BITGET_BASE", "https://api.bitget.com")
API_KEY = os.environ["BITGET_API_KEY"]
API_SECRET = os.environ["BITGET_API_SECRET"]
API_PASSPHRASE = os.environ["BITGET_API_PASSPHRASE"]

def _ts_ms() -> str:
    return str(int(time.time() * 1000))

def _canonical_qs(params: dict) -> str:
    # orden ascendente por clave, urlencode estándar
    if not params: return ""
    items = sorted((k, str(v)) for k, v in params.items() if v is not None)
    return urllib.parse.urlencode(items)

def _sign(method: str, path: str, qs: str, body: str, ts: str) -> str:
    prehash = ts + method.upper() + path + ("?" + qs if qs else "") + body
    digest = hmac.new(
        API_SECRET.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode()

def _headers(ts: str, signature: str) -> dict:
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": signature,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
        "locale": "en-US"
    }

def bitget_get(path: str, params: dict = None) -> dict:
    qs = _canonical_qs(params or {})
    ts = _ts_ms()
    sig = _sign("GET", path, qs, "", ts)
    url = f"{BITGET_BASE}{path}" + (("?" + qs) if qs else "")
    resp = requests.get(url, headers=_headers(ts, sig), timeout=15)
    text = resp.text

    # Propaga detalles si es 4xx/5xx
    try:
        resp.raise_for_status()
    except requests.HTTPError as http_err:
        try:
            j = resp.json()
            raise RuntimeError(f"HTTP {resp.status_code} Bitget: {j}") from http_err
        except Exception:
            raise RuntimeError(f"HTTP {resp.status_code} Bitget (raw): {text[:500]}") from http_err

    # 2xx: valida que sea JSON dict
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"Bitget non-JSON 2xx response: {text[:500]}")

    if not isinstance(data, dict):
        raise RuntimeError(f"Bitget unexpected JSON type: {type(data).__name__} value={repr(data)[:200]}")

    if str(data.get("code")) not in ("00000", "0"):
        raise RuntimeError(f"Bitget error: {data}")

    return data


def history_orders(symbol: str, start_ms: int = None, end_ms: int = None, max_pages: int = 100):
    """
    Pagina usando limit=100 e idLessThan (navegando hacia atrás).
    Devuelve lista de órdenes (data[]).
    """
    results = []
    id_less_than = None
    pages = 0
    while pages < max_pages:
        params = {
            "symbol": symbol,
            "limit": 100,
            "idLessThan": id_less_than,
            "startTime": start_ms,
            "endTime": end_ms,
        }
        data = bitget_get("/api/v2/spot/trade/history-orders", params)
        page = data.get("data", []) or []
        if not isinstance(page, list):
            break
        # Filtrar elementos que no son diccionarios
        page = [item for item in page if isinstance(item, dict)]
        if not page:
            break
        results.extend(page)
        # siguiente página: usar el orderId más bajo de la página como idLessThan
        order_ids = []
        for x in page:
            if isinstance(x, dict) and "orderId" in x:
                try:
                    order_ids.append(int(x["orderId"]))
                except (ValueError, TypeError):
                    continue
        if not order_ids:
            break
        min_id = min(order_ids)
        id_less_than = str(min_id)
        pages += 1
    return results