import os
import time
import hmac
import json
import hashlib
import base64
import urllib.parse
import boto3
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone
from threading import Lock, Thread
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from collections import defaultdict, deque
import signal
import threading
from dataclasses import dataclass
from enum import Enum

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Importar optimizador de respuestas
try:
    from response_optimizer import (
        optimize_orders_response,
        create_minimal_response,
    )
    OPTIMIZER_AVAILABLE = True
except ImportError:
    OPTIMIZER_AVAILABLE = False
    print("WARNING: response_optimizer not available, using basic truncation")

# ---------- Config ----------
BITGET_BASE = os.environ.get("BITGET_BASE", "https://api.bitget.com").rstrip("/")
API_KEY = os.environ["BITGET_API_KEY"]
API_SECRET = os.environ["BITGET_API_SECRET"]
API_PASSPHRASE = os.environ["BITGET_API_PASSPHRASE"]

# Configuración general - OPTIMIZADA para Lambda
TIMEOUT = float(os.environ.get("BITGET_TIMEOUT", "15"))
RETRIES = int(os.environ.get("BITGET_RETRIES", "2"))
USE_SERVER_TIME = os.environ.get("BITGET_USE_SERVER_TIME", "false").lower() == "true"

# LÍMITES AGRESIVOS para evitar timeouts
MAX_PAGES = int(os.environ.get("BITGET_MAX_PAGES", "50"))
PAGE_LIMIT = int(os.environ.get("BITGET_PAGE_LIMIT", "100"))
MAX_EXECUTION_TIME = int(os.environ.get("MAX_EXECUTION_TIME", "50"))

# Timeout específico por tipo de consulta
SPOT_MAX_PAGES = int(os.environ.get("SPOT_MAX_PAGES", "25"))
FUTURES_MAX_PAGES = int(os.environ.get("FUTURES_MAX_PAGES", "25"))

# PARALELIZACIÓN CONTROLADA
MAX_CONCURRENT_FUTURES = int(os.environ.get("MAX_CONCURRENT_FUTURES", "3"))
MAX_CONCURRENT_SPOT = int(os.environ.get("MAX_CONCURRENT_SPOT", "2"))
ENABLE_SMART_PAGINATION = os.environ.get("ENABLE_SMART_PAGINATION", "true").lower() == "true"
ENABLE_CIRCUIT_BREAKER = os.environ.get("ENABLE_CIRCUIT_BREAKER", "true").lower() == "true"

# ADAPTIVE REQUEST SIZING
MIN_PAGE_SIZE = int(os.environ.get("MIN_PAGE_SIZE", "50"))
MAX_PAGE_SIZE = int(os.environ.get("MAX_PAGE_SIZE", "100"))
ADAPTIVE_PAGE_SIZING = os.environ.get("ADAPTIVE_PAGE_SIZING", "true").lower() == "true"

# Sufijos v1 para futures history
ALL_FUTURES_V1_SUFFIXES = [
    "UMCBL",
    "DMCBL",
    "CMCBL",
]

# S3 Configuration for storing large results
RESULTS_BUCKET = os.environ.get("RESULTS_BUCKET")
RESULTS_PREFIX = os.environ.get("RESULTS_PREFIX", "per-symbol/").rstrip("/") + "/"
AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-2"

S3_THRESHOLD = int(os.environ.get("S3_THRESHOLD", "1"))

# S3 client (only initialize if bucket is configured)
S3 = None
if RESULTS_BUCKET:
    S3 = boto3.client("s3")

# ---------- CIRCUIT BREAKER IMPLEMENTATION ----------
@dataclass
class CircuitBreakerState:
    failure_count: int = 0
    last_failure_time: float = 0
    state: str = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
    success_count: int = 0

class CircuitBreaker:
    """
    Circuit Breaker optimizado para símbolos de Bitget que fallan frecuentemente
    """
    def __init__(self, failure_threshold: int = 3, recovery_timeout: int = 30, success_threshold: int = 2):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout  
        self.success_threshold = success_threshold
        self.states: Dict[str, CircuitBreakerState] = defaultdict(CircuitBreakerState)
        self.lock = Lock()

    def can_execute(self, symbol: str) -> bool:
        """Determina si se puede ejecutar una request para el símbolo dado"""
        if not ENABLE_CIRCUIT_BREAKER:
            return True
            
        with self.lock:
            state = self.states[symbol]
            now = time.time()
            
            if state.state == "OPEN":
                if now - state.last_failure_time > self.recovery_timeout:
                    state.state = "HALF_OPEN"
                    state.success_count = 0
                    return True
                return False
            
            return True  # CLOSED o HALF_OPEN

    def record_success(self, symbol: str):
        """Registra una operación exitosa"""
        if not ENABLE_CIRCUIT_BREAKER:
            return
            
        with self.lock:
            state = self.states[symbol]
            state.failure_count = 0
            
            if state.state == "HALF_OPEN":
                state.success_count += 1
                if state.success_count >= self.success_threshold:
                    state.state = "CLOSED"

    def record_failure(self, symbol: str, error: str):
        """Registra una falla"""
        if not ENABLE_CIRCUIT_BREAKER:
            return
            
        with self.lock:
            state = self.states[symbol]
            state.failure_count += 1
            state.last_failure_time = time.time()
            
            if state.failure_count >= self.failure_threshold:
                state.state = "OPEN"
                print(f"🔴 Circuit breaker OPEN for {symbol} due to {state.failure_count} failures")

# Instancia global del circuit breaker
circuit_breaker = CircuitBreaker()

# ---------- ADAPTIVE REQUEST SIZING ----------
class AdaptiveRequestSizer:
    """
    Ajusta dinámicamente el tamaño de página basado en el rendimiento observado
    """
    def __init__(self):
        self.symbol_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            'avg_response_time': 0.0,
            'avg_results_per_page': 0.0,
            'sample_count': 0,
            'last_page_size': PAGE_LIMIT,
            'optimal_page_size': PAGE_LIMIT
        })
        self.lock = Lock()

    def get_optimal_page_size(self, symbol: str, api_type: str) -> int:
        """Obtiene el tamaño de página óptimo para un símbolo y tipo de API"""
        if not ADAPTIVE_PAGE_SIZING:
            return PAGE_LIMIT
            
        with self.lock:
            key = f"{symbol}_{api_type}"
            stats = self.symbol_stats[key]
            
            if stats['sample_count'] < 2:
                return PAGE_LIMIT
            
            # Si la respuesta promedio es muy rápida, incrementar página
            if stats['avg_response_time'] < 1.0 and stats['optimal_page_size'] < MAX_PAGE_SIZE:
                stats['optimal_page_size'] = min(MAX_PAGE_SIZE, stats['optimal_page_size'] + 20)
            # Si es muy lenta, decrementar
            elif stats['avg_response_time'] > 3.0 and stats['optimal_page_size'] > MIN_PAGE_SIZE:
                stats['optimal_page_size'] = max(MIN_PAGE_SIZE, stats['optimal_page_size'] - 20)
            
            return stats['optimal_page_size']

    def record_request_stats(self, symbol: str, api_type: str, response_time: float, results_count: int, page_size: int):
        """Registra estadísticas de una request"""
        if not ADAPTIVE_PAGE_SIZING:
            return
            
        with self.lock:
            key = f"{symbol}_{api_type}"
            stats = self.symbol_stats[key]
            
            # Actualizar promedios usando media móvil
            alpha = 0.3  # Factor de suavizado
            if stats['sample_count'] == 0:
                stats['avg_response_time'] = response_time
                stats['avg_results_per_page'] = results_count
            else:
                stats['avg_response_time'] = alpha * response_time + (1 - alpha) * stats['avg_response_time']
                stats['avg_results_per_page'] = alpha * results_count + (1 - alpha) * stats['avg_results_per_page']
            
            stats['sample_count'] += 1
            stats['last_page_size'] = page_size

# Instancia global del adaptive sizer
adaptive_sizer = AdaptiveRequestSizer()

# ---------- SMART PAGINATION PREDICTOR ----------
class SmartPaginationPredictor:
    """
    Predice cuándo parar la paginación basándose en patrones de datos
    """
    def __init__(self):
        self.pagination_patterns: Dict[str, List[int]] = defaultdict(list)
        self.lock = Lock()

    def should_continue_pagination(self, symbol: str, current_page: int, results_in_page: int, page_limit: int) -> bool:
        """Determina si continuar con la paginación basándose en patrones"""
        if not ENABLE_SMART_PAGINATION:
            return results_in_page >= page_limit * 0.8  # Continuar si hay >= 80% del límite
        
        with self.lock:
            # Registrar este resultado
            key = f"{symbol}_pattern"
            self.pagination_patterns[key].append(results_in_page)
            
            # Mantener solo las últimas 10 páginas para el patrón
            if len(self.pagination_patterns[key]) > 10:
                self.pagination_patterns[key] = self.pagination_patterns[key][-10:]
            
            # Si tenemos suficientes datos, predecir
            if len(self.pagination_patterns[key]) >= 3:
                recent_results = self.pagination_patterns[key][-3:]
                
                # Si las últimas 3 páginas han sido consistentemente bajas, probablemente no hay más datos
                if all(r < page_limit * 0.3 for r in recent_results):
                    return False
                
                # Si hay una tendencia decreciente fuerte, considerar parar
                if len(recent_results) >= 3:
                    decreasing_trend = all(recent_results[i] > recent_results[i+1] for i in range(len(recent_results)-1))
                    if decreasing_trend and results_in_page < page_limit * 0.2:
                        return False
            
            # Lógica por defecto
            return results_in_page >= page_limit * 0.5  # Continuar si hay >= 50% del límite

# Instancia global del predictor
pagination_predictor = SmartPaginationPredictor()

# ---------- TIMEOUT HANDLER ----------
class TimeoutException(Exception):
    pass

class ExecutionTimer:
    def __init__(self, max_execution_time: int):
        self.max_execution_time = max_execution_time
        self.start_time = time.time()
        
    def check_timeout(self, context_msg: str = ""):
        """Verifica si hemos excedido el tiempo máximo de ejecución"""
        elapsed = time.time() - self.start_time
        if elapsed > self.max_execution_time:
            raise TimeoutException(f"Execution timeout after {elapsed:.1f}s while {context_msg}")
        return elapsed
    
    def remaining_time(self) -> float:
        """Retorna el tiempo restante en segundos"""
        elapsed = time.time() - self.start_time
        return max(0, self.max_execution_time - elapsed)

# Timer global
execution_timer = None

def init_timer(max_time: int = MAX_EXECUTION_TIME):
    global execution_timer
    execution_timer = ExecutionTimer(max_time)

# ---------- Rate Limiting - OPTIMIZADO ----------
class RateLimiter:
    """
    Rate limiter optimizado que respeta los límites de la API de Bitget
    pero prioriza velocidad sobre capacidad máxima.
    """
    def __init__(self):
        self.last_requests = {
            'spot': [],
            'futures': []
        }
        self.lock = Lock()
        self.limits = {
            'spot': 15,
            'futures': 8
        }

    def wait_if_needed(self, api_type: str):
        """
        Espera si es necesario para respetar el rate limit.
        Versión optimizada con timeouts más cortos.
        """
        if execution_timer:
            execution_timer.check_timeout(f"rate limiting {api_type}")
            
        with self.lock:
            now = time.time()
            limit = self.limits.get(api_type, 8)

            self.last_requests[api_type] = [
                req_time for req_time in self.last_requests[api_type]
                if now - req_time < 1.0
            ]

            if len(self.last_requests[api_type]) >= limit:
                oldest_in_window = min(self.last_requests[api_type])
                wait_time = 1.0 - (now - oldest_in_window)
                if wait_time > 0:
                    # MÁXIMO 500ms de espera para evitar timeouts
                    actual_wait = min(wait_time, 0.5)
                    print(f"⏳ Rate limiting: waiting {actual_wait:.3f}s for {api_type} API")
                    time.sleep(actual_wait)
                    now = time.time()
                    self.last_requests[api_type] = [
                        req_time for req_time in self.last_requests[api_type]
                        if now - req_time < 1.0
                    ]

            # Registrar esta request
            self.last_requests[api_type].append(now)

# Instancia global del rate limiter
rate_limiter = RateLimiter()

# ---------- HTTP Session with retries----------
_session = requests.Session()
retry_cfg = Retry(
    total=RETRIES,  # Reducido
    connect=RETRIES,
    read=RETRIES,
    backoff_factor=0.3,  # Reducido de 0.6 a 0.3
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST", "DELETE"])
)
_adapter = HTTPAdapter(max_retries=retry_cfg)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

# ---------- S3 Helpers ----------
def _generate_s3_key(symbol: str, suffix: str = "json") -> str:
    """Generate S3 key for storing symbol data"""
    now = datetime.now(timezone.utc)
    timestamp = now.strftime('%Y%m%d-%H%M%S')
    return f"{RESULTS_PREFIX}{symbol}/{timestamp}.{suffix}"

def _store_orders_in_s3(symbol: str, orders: List[Dict[str, Any]]) -> Dict[str, str]:
    """
    Store orders in S3 and return metadata
    Returns dict with s3_key, s3_uri, and public_url
    """
    if not S3 or not RESULTS_BUCKET:
        raise RuntimeError("S3 not configured but needed for large result storage")

    s3_key = _generate_s3_key(symbol)

    payload = {
        "symbol": symbol,
        "orders": orders,
        "count": len(orders),
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }

    S3.put_object(
        Bucket=RESULTS_BUCKET,
        Key=s3_key,
        Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )

    return {
        "s3_key": s3_key,
        "s3_uri": f"s3://{RESULTS_BUCKET}/{s3_key}",
        "public_url": f"https://{RESULTS_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"
    }

def _ts_ms_local() -> str:
    return str(int(time.time() * 1000))

def _timestamp_ms() -> str:
    # SIEMPRE usar tiempo local para velocidad
    return _ts_ms_local()

def _canonical_qs(params: Dict[str, Any]) -> str:
    """QS con orden por clave y % encoding estándar (sin '+')."""
    if not params:
        return ""
    items = []
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        else:
            v = str(v)
        items.append((k, v))
    items.sort(key=lambda kv: kv[0])
    return urllib.parse.urlencode(items, quote_via=urllib.parse.quote, safe=",")

def _sign(method: str, path: str, qs: str, body: str, ts: str) -> str:
    prehash = ts + method.upper() + path + (("?" + qs) if qs else "") + (body or "")
    digest = hmac.new(API_SECRET.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()

def _headers(ts: str, signature: str) -> Dict[str, str]:
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": signature,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
        "locale": "en-US",
    }

def _parse_bitget_error(error_text: str, symbol: str) -> str:
    """
    Parsea errores de Bitget y devuelve mensajes más limpios en inglés.
    """
    try:
        if "HTTP 400 Bitget" in error_text and "40034" in error_text:
            return f"Symbol '{symbol}' does not exist on Bitget"
        elif "HTTP 400 Bitget" in error_text and "Parameter" in error_text and "does not exist" in error_text:
            return f"Symbol '{symbol}' does not exist on Bitget"
        elif "HTTP 401" in error_text:
            return "Authentication failed - check API credentials"
        elif "HTTP 403" in error_text:
            return "Access forbidden - check API permissions"
        elif "HTTP 429" in error_text:
            return "Rate limit exceeded - too many requests (will retry automatically)"
        elif "HTTP 500" in error_text or "HTTP 502" in error_text or "HTTP 503" in error_text:
            return "Bitget server error - will retry automatically"
        elif "timeout" in error_text.lower():
            return f"Request timeout - consider increasing BITGET_TIMEOUT (current: {TIMEOUT}s)"
        elif "connection" in error_text.lower():
            return "Network connection error to Bitget API - will retry automatically"
        else:
            return f"Bitget API error for symbol '{symbol}': {error_text[:100]}..."
    except Exception:
        return f"Bitget API error for symbol '{symbol}'"

def _bitget_get(path: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
    # Verificar timeout antes de hacer request
    if execution_timer:
        execution_timer.check_timeout(f"making request to {path}")
    
    # Determinar tipo de API basado en el path
    api_type = 'spot' if '/spot/' in path else 'futures'

    # Aplicar rate limiting
    rate_limiter.wait_if_needed(api_type)

    # MEDIR TIEMPO DE RESPUESTA PARA ADAPTIVE SIZING
    request_start_time = time.time()
    
    qs = _canonical_qs(params or {})
    ts = _timestamp_ms()
    sig = _sign("GET", path, qs, "", ts)
    url = f"{BITGET_BASE}{path}" + (("?" + qs) if qs else "")

    resp = _session.get(url, headers=_headers(ts, sig), timeout=TIMEOUT)
    text = resp.text
    
    # Registrar tiempo de respuesta
    response_time = time.time() - request_start_time

    try:
        resp.raise_for_status()
    except requests.HTTPError as http_err:
        try:
            j = resp.json()
            raise RuntimeError(f"HTTP {resp.status_code} Bitget: {j}") from http_err
        except Exception:
            raise RuntimeError(f"HTTP {resp.status_code} Bitget (raw): {text}") from http_err

    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"Bitget non-JSON 2xx response: {text[:500]}")

    if not isinstance(data, dict):
        raise RuntimeError(f"Bitget unexpected JSON type: {type(data).__name__} value={repr(data)[:200]}")

    code = str(data.get("code", ""))
    if code not in ("00000", "0"):
        raise RuntimeError(f"Bitget error: {data}")
    
    # Registrar estadísticas para adaptive sizing si tenemos parámetros de símbolo
    if params and 'symbol' in params:
        symbol = params['symbol']
        results_count = 0
        if data.get('data'):
            if isinstance(data['data'], list):
                results_count = len(data['data'])
            elif isinstance(data['data'], dict) and 'orderList' in data['data']:
                results_count = len(data['data']['orderList'])
        
        page_size = params.get('limit', params.get('pageSize', PAGE_LIMIT))
        adaptive_sizer.record_request_stats(symbol, api_type, response_time, results_count, page_size)
    
    return data

def _min_order_id(page: List[Dict[str, Any]]) -> Optional[int]:
    vals = []
    for x in page:
        if not isinstance(x, dict):
            continue
        oid = x.get("orderId")
        if oid is None:
            continue
        try:
            vals.append(int(oid))
        except Exception:
            pass
    return min(vals) if vals else None

def _extract_ctime_range(page: List[Dict[str, Any]]) -> tuple[Optional[int], Optional[int]]:
    """
    Extrae el rango de cTime (earliest, latest) de una página de órdenes.
    """
    ctimes = []
    for order in page:
        if not isinstance(order, dict):
            continue
        ctime = order.get("cTime")
        if ctime:
            try:
                ctimes.append(int(ctime))
            except (ValueError, TypeError):
                continue

    if not ctimes:
        return None, None

    return min(ctimes), max(ctimes)

def _validate_time_boundary(orders: List[Dict[str, Any]], start_ms: Optional[int], end_ms: Optional[int]) -> List[Dict[str, Any]]:
    """
    Filtra órdenes que están dentro del rango temporal especificado basándose en cTime.
    """
    if not start_ms and not end_ms:
        return orders

    filtered_orders = []
    for order in orders:
        if not isinstance(order, dict):
            continue

        ctime = order.get("cTime")
        if not ctime:
            filtered_orders.append(order)
            continue

        try:
            ctime_ms = int(ctime)

            # Verificar que esté dentro del rango
            within_range = True
            if start_ms and ctime_ms < start_ms:
                within_range = False
            if end_ms and ctime_ms > end_ms:
                within_range = False

            if within_range:
                filtered_orders.append(order)

        except (ValueError, TypeError):
            filtered_orders.append(order)

    return filtered_orders

def _coerce_ms(x: Any) -> Optional[int]:
    if x is None:
        return None
    try:
        xi = int(x)
        if xi < 10_000_000_000:  # ~2286-11-20
            return xi * 1000
        return xi
    except Exception:
        return None

# ---------- SPOT - OPTIMIZADO ----------
def spot_history_orders(
    symbol: str,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    limit: int = PAGE_LIMIT,
    max_pages: int = SPOT_MAX_PAGES,
) -> List[Dict[str, Any]]:
    """
    Versión OPTIMIZADA para Lambda
    """
    # Omitir spot si el símbolo tiene guion bajo
    if "_" in str(symbol):
        print(f"Skipping spot retrieval for underscored symbol: {symbol}")
        return []

    all_results: List[Dict[str, Any]] = []

    # VENTANA REDUCIDA para velocidad: 30 días
    if start_ms is None and end_ms is None:
        now_ms = int(time.time() * 1000)
        thirty_days_ms = 30 * 24 * 60 * 60 * 1000
        start_ms = now_ms - thirty_days_ms
        end_ms = now_ms
        print(f"Auto-setting 30-day window for speed: {start_ms} to {end_ms}")

    return _get_spot_orders_chunk(symbol, start_ms, end_ms, limit, max_pages)

def _get_spot_orders_chunk(
    symbol: str,
    start_ms: Optional[int],
    end_ms: Optional[int],
    limit: int,
    max_pages: int
) -> List[Dict[str, Any]]:
    """
    OPTIMIZADO: Paralelización inteligente para NORMAL y TPSL con circuit breaker
    """
    # Verificar circuit breaker
    if not circuit_breaker.can_execute(symbol):
        print(f"Circuit breaker OPEN for {symbol}, skipping spot orders")
        return []

    all_orders = []

    # Verificar tiempo restante
    if execution_timer and execution_timer.remaining_time() < 10:
        print(f"Less than 10s remaining, skipping spot orders to avoid timeout")
        return []

    # PARALELIZACIÓN CONTROLADA para NORMAL y TPSL
    tasks = []
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_SPOT) as executor:
        # TASK 1: NORMAL orders
        future_normal = executor.submit(
            _get_spot_orders_by_type_with_circuit_breaker,
            symbol, "normal", start_ms, end_ms, limit, max_pages
        )
        tasks.append(("normal", future_normal))
        
        # TASK 2: TPSL orders
        if execution_timer and execution_timer.remaining_time() > 15:
            future_tpsl = executor.submit(
                _get_spot_orders_by_type_with_circuit_breaker,
                symbol, "tpsl", start_ms, end_ms, limit, min(max_pages//2, 10)
            )
            tasks.append(("tpsl", future_tpsl))
        else:
            print(f"Skipping TPSL")

        # Recopilar resultados conforme van completándose
        for task_type, future in tasks:
            try:
                if execution_timer and execution_timer.remaining_time() < 3:
                    print(f"Cancelling remaining tasks")
                    future.cancel()
                    continue
                
                timeout = min(10, execution_timer.remaining_time() - 2) if execution_timer else 10
                orders = future.result(timeout=timeout)
                
                for order in orders:
                    order["_tpsl_type"] = task_type
                all_orders.extend(orders)
                print(f"✅ Retrieved {len(orders)} {task_type} spot orders concurrently")
                
                # Registrar éxito en circuit breaker
                circuit_breaker.record_success(f"{symbol}_{task_type}")
                
            except Exception as e:
                error_str = str(e)
                print(f"⚠️ Failed to get {task_type} orders: {error_str}")
                
                # Registrar falla en circuit breaker
                circuit_breaker.record_failure(f"{symbol}_{task_type}", error_str)

    total = len(all_orders)
    print(f"Total spot orders for {symbol}: {total}")
    return all_orders

def _get_spot_orders_by_type_with_circuit_breaker(
    symbol: str,
    tpsl_type: str,
    start_ms: Optional[int],
    end_ms: Optional[int],
    limit: int,
    max_pages: int
) -> List[Dict[str, Any]]:
    """
    Wrapper con circuit breaker para _get_spot_orders_by_type
    """
    symbol_key = f"{symbol}_{tpsl_type}"
    
    if not circuit_breaker.can_execute(symbol_key):
        print(f"🔴 Circuit breaker prevents execution for {symbol_key}")
        return []
    
    try:
        return _get_spot_orders_by_type(symbol, tpsl_type, start_ms, end_ms, limit, max_pages)
    except Exception as e:
        circuit_breaker.record_failure(symbol_key, str(e))
        raise

def _get_spot_orders_by_type(
    symbol: str,
    tpsl_type: str,
    start_ms: Optional[int],
    end_ms: Optional[int],
    limit: int,
    max_pages: int
) -> List[Dict[str, Any]]:
    """
    OPTIMIZADO: Intenta ventana completa, fallback más rápido a 30 días.
    """
    try:
        return _get_spot_orders_by_type_single_chunk(symbol, tpsl_type, start_ms, end_ms, limit, max_pages)
    except Exception as e:
        error_str = str(e)

        if "cannot be greater than 30 days" in error_str:
            print(f"⚠️ 30-day limit detected, using 30-day window")
            return _get_spot_orders_with_chunking(symbol, tpsl_type, start_ms, end_ms, 30, limit, max_pages)
        elif "cannot be greater than 90 days" in error_str:
            print(f"⚠️ 90-day limit detected, using 30-day window for speed")
            return _get_spot_orders_with_chunking(symbol, tpsl_type, start_ms, end_ms, 30, limit, max_pages)
        else:
            print(f"❌ Error for {tpsl_type} orders: {error_str}")
            raise

def _get_spot_orders_with_chunking(
    symbol: str,
    tpsl_type: str,
    start_ms: Optional[int],
    end_ms: Optional[int],
    max_days: int,
    limit: int,
    max_pages: int
) -> List[Dict[str, Any]]:
    """
    OPTIMIZADO: Chunking con límites más agresivos
    """
    if not start_ms or not end_ms:
        now_ms = int(time.time() * 1000)
        max_days_ms = max_days * 24 * 60 * 60 * 1000
        start_ms = now_ms - max_days_ms
        end_ms = now_ms
        return _get_spot_orders_by_type_single_chunk(symbol, tpsl_type, start_ms, end_ms, limit, max_pages)

    # MÁXIMO 2 chunks para evitar timeout
    total_range_ms = end_ms - start_ms
    max_days_ms = max_days * 24 * 60 * 60 * 1000
    num_chunks = min(2, (total_range_ms + max_days_ms - 1) // max_days_ms)

    print(f"🔄 Processing max {num_chunks} chunks for {tpsl_type}")

    all_results = []
    current_start = start_ms
    chunk_num = 1

    while current_start < end_ms and chunk_num <= num_chunks:
        # Verificar timeout
        if execution_timer and execution_timer.remaining_time() < 5:
            print(f"⏰ Stopping chunking due to time constraint")
            break

        chunk_end = min(current_start + max_days_ms, end_ms)
        print(f"Chunk {chunk_num}/{num_chunks}: {current_start} to {chunk_end}")

        chunk_results = _get_spot_orders_by_type_single_chunk(
            symbol, tpsl_type, current_start, chunk_end, limit, max_pages//num_chunks
        )
        all_results.extend(chunk_results)

        current_start = chunk_end
        chunk_num += 1

    return all_results

def _get_spot_orders_by_type_single_chunk(
    symbol: str,
    tpsl_type: str,
    start_ms: Optional[int],
    end_ms: Optional[int],
    limit: int,
    max_pages: int
) -> List[Dict[str, Any]]:
    """
    OPTIMIZADO: Paginación inteligente con adaptive sizing y smart pagination
    """
    results: List[Dict[str, Any]] = []
    id_less_than: Optional[str] = None
    pages = 0
    current_end_time = end_ms

    # ADAPTIVE PAGE SIZING
    current_limit = adaptive_sizer.get_optimal_page_size(symbol, f"spot_{tpsl_type}")
    print(f"Using adaptive page size {current_limit} for {symbol} {tpsl_type}")

    while pages < max_pages:
        # VERIFICAR TIMEOUT en cada página
        if execution_timer and execution_timer.remaining_time() < 3:
            print(f"Stopping pagination due to time constraint at page {pages}")
            break

        params = {
            "symbol": symbol,
            "limit": current_limit,
            "idLessThan": id_less_than,
            "startTime": start_ms,
            "endTime": current_end_time,
            "tpslType": tpsl_type,
            "receiveWindow": 5000
        }

        try:
            data = _bitget_get("/api/v2/spot/trade/history-orders", params)
            page = data.get("data") or []

            if not isinstance(page, list) or not page:
                break

            page = [item for item in page if isinstance(item, dict)]
            if not page:
                break

            # SMART PAGINATION PREDICTION
            if not pagination_predictor.should_continue_pagination(
                f"{symbol}_{tpsl_type}", pages, len(page), current_limit
            ):
                print(f"Smart pagination suggests stopping at page {pages} for {symbol} {tpsl_type}")
                results.extend(page)
                break

            earliest_ctime, latest_ctime = _extract_ctime_range(page)
            filtered_page = _validate_time_boundary(page, start_ms, current_end_time)
            results.extend(filtered_page)

            if earliest_ctime and start_ms and earliest_ctime < start_ms:
                break

            if earliest_ctime:
                current_end_time = earliest_ctime - 1
                if start_ms and current_end_time <= start_ms:
                    break

            min_id = _min_order_id(page)
            if min_id:
                id_less_than = str(min_id)

            pages += 1

            # DYNAMIC PAGE SIZE ADJUSTMENT basado en results
            if ADAPTIVE_PAGE_SIZING and pages > 2:
                if len(page) == current_limit and current_limit < MAX_PAGE_SIZE:
                    # Si siempre llenamos la página, incrementar
                    current_limit = min(MAX_PAGE_SIZE, current_limit + 20)
                elif len(page) < current_limit * 0.5 and current_limit > MIN_PAGE_SIZE:
                    # Si consistentemente obtenemos pocos resultados, decrementar
                    current_limit = max(MIN_PAGE_SIZE, current_limit - 10)

            if len(page) < current_limit * 0.8:  # Si no hay suficientes resultados, probablemente no hay más
                break

        except Exception as e:
            error_str = str(e)
            if "cannot be greater than 30 days" in error_str or "cannot be greater than 90 days" in error_str:
                raise
            print(f"Error in pagination: {error_str}")
            break

    try:
        results.sort(key=lambda x: int(x.get("cTime", 0)), reverse=True)
    except (ValueError, TypeError):
        pass

    print(f"{symbol} {tpsl_type}: {len(results)} orders in {pages} pages (avg: {len(results)/max(pages,1):.1f} per page)")
    return results

# ---------- FUTURES: OPTIMIZADO----------
def futures_history_orders_v1(
    symbol_with_suffix: str,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    limit: int = 1000,
    max_pages: int = FUTURES_MAX_PAGES,
) -> List[Dict[str, Any]]:
    """
    Versión OPTIMIZADA con timeout checking
    """
    # Verificar tiempo restante
    if execution_timer and execution_timer.remaining_time() < 5:
        print(f"Skipping {symbol_with_suffix}")
        return []

    symbol_v1 = str(symbol_with_suffix).upper()
    results: List[Dict[str, Any]] = []
    
    if end_ms is None:
        end_ms = int(time.time() * 1000)
    if start_ms is None:
        start_ms = 0
    
    end_id = ""
    count = 0
    limit_attempts = 10
    
    while True:
        if execution_timer and execution_timer.remaining_time() < 2:
            print(f"Stopping futures query for {symbol_v1} due to time constraint")
            break

        try:
            params = {
                "startTime": start_ms,
                "endTime": end_ms,
                "symbol": symbol_v1,
                "pageSize": limit
            }
            
            if end_id:
                params["lastEndId"] = end_id
            
            rate_limiter.wait_if_needed('futures')
            data = _bitget_get("/api/mix/v1/order/history", params)
            
            if data.get('data') is not None:
                orders_data = data['data']
                
                if orders_data.get('orderList') is not None:
                    page = orders_data['orderList']
                    
                    for order in page:
                        if isinstance(order, dict):
                            order['category'] = 'Future'
                    
                    results.extend(page)
                    
                    if orders_data.get('nextFlag'):
                        end_id = orders_data.get('endId', "")
                    else:
                        break
                else:
                    break
            else:
                error_code = data.get('code', '')
                
                if error_code == '40034':
                    print(f"Symbol {symbol_v1} does not exist")
                    break
                elif error_code == '40309':
                    print(f"Error 40309 for {symbol_v1}")
                    break
                elif error_code in ['40004', '40008', '40911']:
                    count += 1
                    if count > limit_attempts:
                        raise RuntimeError(f"Max retry attempts reached for {symbol_v1}")
                    time.sleep(0.2)  # REDUCIDO de 0.33 a 0.2
                    continue
                else:
                    raise RuntimeError(f"Bitget error {error_code}: {data.get('msg', 'Unknown error')}")
                    
        except Exception as e:
            count += 1
            if count > limit_attempts:
                raise RuntimeError(f"Max retry attempts reached for {symbol_v1}: {str(e)}")
            
            print(f"Retrying {symbol_v1}, attempt {count}: {str(e)}")
            time.sleep(0.2)  # REDUCIDO
            continue
    
    return results

def futures_get_orders_for_symbol(
    symbol: str,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    favorite_symbols: List[str] = None,
    limit: int = 1000,
    max_pages: int = FUTURES_MAX_PAGES,
) -> List[Dict[str, Any]]:
    """
    SUPER OPTIMIZADO: Paralelización masiva de futures con circuit breaker y batching inteligente
    """
    if not symbol:
        return []
    
    # Verificar tiempo restante antes de empezar
    if execution_timer and execution_timer.remaining_time() < 10:
        print(f"⏰ Insufficient time remaining for futures processing: {execution_timer.remaining_time():.1f}s")
        return []
    
    if end_ms is None:
        end_ms = int(time.time() * 1000)
    if start_ms is None:
        start_ms = 0
    
    base_symbol = str(symbol).upper()
    all_orders: List[Dict[str, Any]] = []
    
    # Generar símbolos con sufijos
    symbols_to_query = []
    for suffix in ALL_FUTURES_V1_SUFFIXES:
        symbol_with_suffix = f"{base_symbol}_{suffix}"
        
        # Aplicar circuit breaker a nivel de símbolo
        if circuit_breaker.can_execute(symbol_with_suffix):
            symbols_to_query.append(symbol_with_suffix)
        else:
            print(f"Circuit breaker prevents {symbol_with_suffix}")
    
    # Filtrar por símbolos favoritos si se proporcionan
    if favorite_symbols:
        filtered_symbols = []
        for fav in favorite_symbols:
            fav_upper = fav.upper()
            for symbol_suffix in symbols_to_query:
                if fav_upper in symbol_suffix:
                    filtered_symbols.append(symbol_suffix)
        symbols_to_query = filtered_symbols
    
    symbols_count = len(symbols_to_query)
    print(f"🎯 Processing {symbols_count} futures symbols for {base_symbol} (parallel)")
    
    if symbols_count == 0:
        return []

    # PARALELIZACIÓN MASIVA CON BATCHING INTELIGENTE
    batch_size = min(MAX_CONCURRENT_FUTURES, symbols_count)
    batches = [symbols_to_query[i:i + batch_size] for i in range(0, symbols_count, batch_size)]
    
    print(f"📦 Processing {len(batches)} batches of max {batch_size} symbols each")
    
    for batch_idx, batch_symbols in enumerate(batches):
        # Verificar tiempo restante para este batch
        if execution_timer:
            remaining = execution_timer.remaining_time()
            if remaining < 5:
                print(f"⏰ Stopping futures processing at batch {batch_idx+1}/{len(batches)} due to time constraint")
                break
            print(f"� Processing batch {batch_idx+1}/{len(batches)} with {len(batch_symbols)} symbols ({remaining:.1f}s remaining)")
        
        # Procesar batch en paralelo
        batch_orders = _process_futures_batch_parallel(
            batch_symbols, start_ms, end_ms, limit, max_pages, base_symbol
        )
        all_orders.extend(batch_orders)
        
        # Early exit si ya no hay tiempo
        if execution_timer and execution_timer.remaining_time() < 3:
            print(f"⏰ Early exit from futures processing due to time constraints")
            break
    
    print(f"🎯 Total futures orders for {base_symbol}: {len(all_orders)}")
    return all_orders

def _process_futures_batch_parallel(
    batch_symbols: List[str],
    start_ms: Optional[int],
    end_ms: Optional[int],
    limit: int,
    max_pages: int,
    base_symbol: str
) -> List[Dict[str, Any]]:
    """
    Procesa un batch de símbolos de futures en paralelo
    """
    all_orders = []
    
    with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_FUTURES, len(batch_symbols))) as executor:
        # Enviar todas las tareas del batch
        future_to_symbol = {}
        for symbol_with_suffix in batch_symbols:
            future = executor.submit(
                _get_futures_orders_with_circuit_breaker,
                symbol_with_suffix, start_ms, end_ms, limit, max_pages, base_symbol
            )
            future_to_symbol[future] = symbol_with_suffix
        
        # Recopilar resultados conforme van completándose
        for future in as_completed(future_to_symbol, timeout=30):
            symbol_with_suffix = future_to_symbol[future]
            
            try:
                # Timeout dinámico basado en tiempo restante
                timeout = min(15, execution_timer.remaining_time() - 2) if execution_timer else 15
                orders = future.result(timeout=timeout)
                
                all_orders.extend(orders)
                print(f"✅ Retrieved {len(orders)} orders from {symbol_with_suffix} (parallel)")
                
                # Registrar éxito en circuit breaker
                circuit_breaker.record_success(symbol_with_suffix)
                
            except Exception as e:
                error_msg = _parse_bitget_error(str(e), symbol_with_suffix)
                print(f"❌ Error getting {symbol_with_suffix} (parallel): {error_msg}")
                
                # Registrar falla en circuit breaker
                circuit_breaker.record_failure(symbol_with_suffix, str(e))
                continue
    
    return all_orders

def _get_futures_orders_with_circuit_breaker(
    symbol_with_suffix: str,
    start_ms: Optional[int],
    end_ms: Optional[int],
    limit: int,
    max_pages: int,
    base_symbol: str
) -> List[Dict[str, Any]]:
    """
    Wrapper con circuit breaker para futures_history_orders_v1
    """
    if not circuit_breaker.can_execute(symbol_with_suffix):
        print(f"🔴 Circuit breaker prevents execution for {symbol_with_suffix}")
        return []
    
    try:
        orders = futures_history_orders_v1(
            symbol_with_suffix=symbol_with_suffix,
            start_ms=start_ms,
            end_ms=end_ms,
            limit=limit,
            max_pages=max_pages
        )
        
        # Agregar metadatos
        for order in orders:
            if isinstance(order, dict):
                order["_symbol"] = base_symbol
                order["_market"] = "futures_history"
                order["_contractType"] = symbol_with_suffix.split("_")[-1]
                order["_endpoint"] = "/api/mix/v1/order/history"
                order["category"] = "Future"
        
        return orders
        
    except Exception as e:
        circuit_breaker.record_failure(symbol_with_suffix, str(e))
        raise

# ---------- Early Exit Helper ----------
def should_continue_processing() -> bool:
    """
    Determina si debemos continuar procesando basándose en el tiempo restante
    """
    if not execution_timer:
        return True
    
    remaining = execution_timer.remaining_time()
    if remaining < 5:
        print(f"⏰ Early exit: Only {remaining:.1f}s remaining")
        return False
    
    return True

# ---------- Main Handler - OPTIMIZADO ----------
def handler(event, context):
    """
    Bitget Worker Lambda optimizado para máximo rendimiento
    """
    # INICIALIZAR TIMER DE EJECUCIÓN
    init_timer(MAX_EXECUTION_TIME)
    
    try:
        start_time = time.time()
        print(f"Starting optimized Lambda execution (max {MAX_EXECUTION_TIME}s)")
        print(f"DEBUG: Received event type: {type(event)}, value: {event}")

        if isinstance(event, str):
            symbol = event
            evt = {}
        elif isinstance(event, dict):
            evt = event
            symbol = evt.get("symbol") or evt.get("Symbol")
        else:
            evt = {}
            symbol = str(event) if event is not None else None

        if not symbol:
            return {"symbol": None, "count": 0, "s3_key": None, "s3_uri": None, "error": "Missing symbol"}

        # Ventanas de tiempo (acepta s o ms)
        start_ms = _coerce_ms(evt.get("start_ms"))
        end_ms = _coerce_ms(evt.get("end_ms"))

        # Flags
        include_spot = evt.get("includeSpot", True)
        include_fut = evt.get("includeFutures", True)

        orders: List[Dict[str, Any]] = []

        # PROCESAMIENTO CONDICIONAL BASADO EN TIEMPO
        
        # SPOT: Prioridad alta si hay tiempo suficiente
        if include_spot and should_continue_processing():
            print(f"⏱️ Starting SPOT processing ({execution_timer.remaining_time():.1f}s remaining)")
            try:
                # PARALELIZACIÓN: Usar ThreadPoolExecutor para procesar spot
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future_spot = executor.submit(spot_history_orders, symbol, start_ms=start_ms, end_ms=end_ms)
                    
                    # Timeout dinámico basado en tiempo restante
                    timeout = min(25, execution_timer.remaining_time() - 10) if execution_timer else 25
                    spot_hist = future_spot.result(timeout=timeout)
                    
                for o in spot_hist:
                    if isinstance(o, dict):
                        o["_symbol"] = symbol
                        o["_market"] = "spot_history"
                        o["_endpoint"] = "/api/v2/spot/trade/history-orders"
                        orders.append(o)
                print(f"SPOT completed: {len(spot_hist)} orders")
            except TimeoutException:
                print(f"SPOT processing timed out, continuing with partial results")
            except Exception as e:
                print(f"SPOT error: {_parse_bitget_error(str(e), symbol)}")

        # FUTURES: Solo si hay tiempo suficiente restante
        if include_fut and should_continue_processing():
            print(f"Starting FUTURES processing ({execution_timer.remaining_time():.1f}s remaining)")
            try:
                # PARALELIZACIÓN: Usar ThreadPoolExecutor para procesar futures
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future_futures = executor.submit(
                        futures_get_orders_for_symbol,
                        symbol=symbol,
                        start_ms=start_ms,
                        end_ms=end_ms
                    )
                    
                    # Timeout dinámico basado en tiempo restante
                    timeout = min(30, execution_timer.remaining_time() - 5) if execution_timer else 30
                    fut_orders = future_futures.result(timeout=timeout)
                    
                orders.extend(fut_orders)
                print(f"FUTURES completed: {len(fut_orders)} orders")
            except TimeoutException:
                print(f"FUTURES processing timed out, continuing with partial results")
            except Exception as e:
                print(f"FUTURES error: {_parse_bitget_error(str(e), symbol)}")

        total_orders = len(orders)
        elapsed_time = time.time() - start_time
        print(f"Processing completed in {elapsed_time:.1f}s: {total_orders} total orders")

        # ALMACENAMIENTO Y RESPUESTA
        if RESULTS_BUCKET and total_orders > 0:
            try:
                print(f"Storing {total_orders} orders in S3...")
                s3_metadata = _store_orders_in_s3(symbol, orders)

                if OPTIMIZER_AVAILABLE:
                    return optimize_orders_response(symbol, orders, s3_metadata)
                else:
                    return {
                        "symbol": symbol,
                        "count": total_orders,
                        "s3_key": s3_metadata["s3_key"],
                        "s3_uri": s3_metadata["s3_uri"],
                        "public_url": s3_metadata["public_url"],
                        "error": None,
                        "data_location": "s3",
                        "execution_time_seconds": elapsed_time,
                        "lambda_optimized": True
                    }
            except Exception as s3_error:
                error_msg = f"S3 storage failed: {str(s3_error)}"
                print(f"CRITICAL: {error_msg}")
                return {
                    "symbol": symbol,
                    "count": total_orders,
                    "orders": [],
                    "truncated": True,
                    "error": error_msg,
                    "data_location": "failed",
                    "execution_time_seconds": elapsed_time
                }

        elif total_orders == 0:
            return {
                "symbol": symbol,
                "orders": [],
                "count": 0,
                "error": None,
                "data_location": "none",
                "execution_time_seconds": elapsed_time,
                "lambda_optimized": True
            }
        else:
            return {
                "symbol": symbol,
                "count": total_orders,
                "orders": [],
                "truncated": True,
                "error": f"RESULTS_BUCKET not configured. {total_orders} orders found but not returned to avoid size limits.",
                "data_location": "none",
                "recommendation": "Configure RESULTS_BUCKET to access full datasets",
                "execution_time_seconds": elapsed_time
            }

    except TimeoutException as timeout_err:
        elapsed = time.time() - start_time
        print(f"⏰ Lambda execution timed out after {elapsed:.1f}s: {str(timeout_err)}")
        
        # Respuesta de timeout con datos parciales si los hay
        partial_count = len(orders) if 'orders' in locals() else 0
        symbol_name = symbol if 'symbol' in locals() else "unknown"
        
        if OPTIMIZER_AVAILABLE:
            return create_minimal_response(
                symbol_name, 
                partial_count, 
                f"Execution timed out after {elapsed:.1f}s. Partial data: {partial_count} orders."
            )
        else:
            return {
                "symbol": symbol_name,
                "count": partial_count,
                "orders": [],
                "s3_key": None,
                "s3_uri": None,
                "error": f"Execution timed out after {elapsed:.1f}s. Consider reducing time window or enabling S3 storage.",
                "partial_data": partial_count > 0,
                "execution_time_seconds": elapsed
            }
    
    except Exception as e:
        elapsed = time.time() - start_time if 'start_time' in locals() else 0
        
        if isinstance(event, dict):
            symbol = event.get("symbol") or event.get("Symbol") or str(event)
        else:
            symbol = str(event)

        error_msg = _parse_bitget_error(str(e), symbol)
        print(f"ERROR in optimized worker for {symbol}: {error_msg}")

        if OPTIMIZER_AVAILABLE:
            return create_minimal_response(symbol, 0, error_msg)
        else:
            return {
                "symbol": symbol,
                "count": 0,
                "orders": [],
                "s3_key": None,
                "s3_uri": None,
                "error": error_msg,
                "execution_time_seconds": elapsed,
                "lambda_optimized": True
            }