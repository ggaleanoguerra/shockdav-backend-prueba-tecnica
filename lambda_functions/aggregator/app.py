import os
import json
import boto3
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

# --- Config ---
S3 = boto3.client("s3")
RESULTS_BUCKET = os.environ.get("RESULTS_BUCKET")                              # obligatorio para guardar en S3
RESULTS_PREFIX = os.environ.get("RESULTS_PREFIX", "bitget-results/").lstrip("/")
RESPONSE_MAX_ORDERS = int(os.environ.get("RESPONSE_MAX_ORDERS", "0"))
AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-2"
CLEANUP_PER_SYMBOL_FILES = os.environ.get("CLEANUP_PER_SYMBOL_FILES", "true").lower() == "true"

MAX_RESPONSE_SIZE_KB = int(os.environ.get("MAX_RESPONSE_SIZE_KB", "220"))      # Límite en KB para respuestas

def _as_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default

def _order_time_safe(o: Any) -> int:
    """
    Extrae timestamp de una orden para ordenamiento.
    Si no se puede parsear, retorna 0 para que quede al final.
    """
    if not isinstance(o, dict):
        return 0
    ts = o.get("orderTime") or o.get("timestamp") or o.get("time")
    return _as_int(ts, 0)

def _categorize_error(error_msg: str) -> Dict[str, str]:
    """
    Categoriza errores para conteo sin incluir listas extensas.
    """
    error_lower = error_msg.lower()
    
    # Errores de API inválida
    if "invalid" in error_lower or "not found" in error_lower or "bad request" in error_lower:
        return {"category": "invalid_request", "message": "Invalid API request or symbol not found", "original": error_msg}
    
    # Errores de permisos
    if "access forbidden" in error_lower or "permissions" in error_lower:
        return {"category": "permission_error", "message": "Access forbidden - check API permissions", "original": error_msg}
    
    # Errores de rate limit
    if "rate limit" in error_lower or "too many requests" in error_lower:
        return {"category": "rate_limit", "message": "Rate limit exceeded - too many requests", "original": error_msg}
    
    # Errores de servidor
    if "server error" in error_lower or "try again later" in error_lower:
        return {"category": "server_error", "message": "Bitget server error - please try again later", "original": error_msg}
    
    # Errores de timeout
    if "timeout" in error_lower:
        return {"category": "timeout", "message": "Request timeout - Bitget API did not respond in time", "original": error_msg}
    
    # Errores de red
    if "network" in error_lower or "connection" in error_lower:
        return {"category": "network_error", "message": "Network connection error to Bitget API", "original": error_msg}
    
    # Errores de S3
    if "s3" in error_lower or "storage failed" in error_lower:
        return {"category": "storage_error", "message": error_msg, "original": error_msg}
    
    # Error genérico - capturar mensaje completo
    return {"category": "api_error", "message": error_msg, "original": error_msg}

def _delete_per_symbol_files(per_symbol_keys: List[str]) -> Dict[str, Any]:
    """
    Elimina los archivos per-symbol de S3 después de crear el archivo agregado.
    
    Args:
        per_symbol_keys: Lista de keys de S3 a eliminar
        
    Returns:
        Diccionario con estadísticas de la eliminación
    """
    if not RESULTS_BUCKET or not CLEANUP_PER_SYMBOL_FILES or not per_symbol_keys:
        return {"cleaned": False, "reason": "cleanup disabled or no files to clean"}
    
    deleted_count = 0
    failed_deletions = []
    
    try:
        # Eliminar archivos en lotes para eficiencia
        for key in per_symbol_keys:
            if not key:  # Skip empty keys
                continue
                
            try:
                print(f"Deleting per-symbol file: s3://{RESULTS_BUCKET}/{key}")
                S3.delete_object(Bucket=RESULTS_BUCKET, Key=key)
                deleted_count += 1
            except Exception as e:
                failed_deletions.append({"key": key, "error": str(e)})
                print(f"Failed to delete {key}: {str(e)}")
    
    except Exception as e:
        return {
            "cleaned": False, 
            "error": f"Cleanup process failed: {str(e)}",
            "deleted_count": deleted_count,
            "failed_deletions": failed_deletions
        }
    
    cleanup_result = {
        "cleaned": True,
        "deleted_count": deleted_count,
        "total_requested": len(per_symbol_keys),
        "success_rate": f"{(deleted_count/len(per_symbol_keys)*100):.1f}%" if per_symbol_keys else "0%"
    }
    
    if failed_deletions:
        cleanup_result["failed_deletions"] = failed_deletions
        cleanup_result["some_failures"] = True
    
    print(f"✅ Cleanup completed: {deleted_count}/{len(per_symbol_keys)} files deleted")
    return cleanup_result

def _results_key(now: datetime, suffix: str = "json") -> str:
    # key como YYYY/MM/DD/HH-mm-ssZ.json bajo RESULTS_PREFIX
    prefix = RESULTS_PREFIX if RESULTS_PREFIX.endswith("/") else RESULTS_PREFIX + "/"
    return f"{prefix}{now.strftime('%Y/%m/%d/%H-%M-%SZ')}.{suffix}"

def _get_json_from_s3(bucket: str, key: str) -> Dict[str, Any]:
    obj = S3.get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read())

def handler(event, context):
    """
    event: lista con la salida de cada worker (ligero o legacy).
      Ligero (recomendado):
        { "symbol":"BTCUSDT", "count":123, "s3_key":"per-symbol/BTCUSDT/....json", "s3_uri":"s3://...", "error":null }
      Legacy (no recomendado por límite 256KB):
        { "symbol":"BTCUSDT", "orders":[{...}, ...], "count": 50, "error":null }

    Este reducer:
      - Lee órdenes desde S3 si viene s3_key; si no, usa 'orders' inline.
      - Ordena DESC (más reciente primero).
      - Guarda el resultado completo en S3.
      - Devuelve un resumen + (opcional) un subconjunto de órdenes para inspección rápida.
    """
    # Capturar tiempo de inicio del agregador
    aggregator_start_time = datetime.now(timezone.utc)
    print(f"Aggregator started at: {aggregator_start_time.isoformat()}")
    
    items: List[Any] = event if isinstance(event, list) else [event]
    all_orders: List[Dict[str, Any]] = []
    errors: List[Dict[str, Optional[str]]] = []
    total_symbols_processed = 0
    total_symbols_with_data = 0
    per_symbol_keys_to_delete: List[str] = []  # Track per-symbol files for cleanup

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append({"symbol": None, "error": f"Unexpected item at {idx}: {type(item).__name__}={repr(item)[:200]}"})
            continue

        sym = item.get("symbol")
        total_symbols_processed += 1

        # Error del worker si vino
        if item.get("error"):
            error_info = _categorize_error(str(item.get("error")))
            errors.append({
                "symbol": sym, 
                "error": error_info.get("original", error_info["message"]),
                "category": error_info["category"]
            })
            # Si hay error pero también datos, continúa procesando
            if not item.get("orders") and not item.get("s3_key"):
                continue

        # Preferir leer desde S3 si hay puntero
        orders: Any = None
        key = item.get("s3_key")
        if key and RESULTS_BUCKET:
            try:
                print(f"Reading orders for {sym} from S3: {key}")
                data = _get_json_from_s3(RESULTS_BUCKET, key)
                orders = data.get("orders")
                if orders:
                    print(f"Loaded {len(orders)} orders for {sym} from S3")
                    # Track this S3 key for potential cleanup
                    per_symbol_keys_to_delete.append(key)
            except Exception as e:
                error_info = _categorize_error(f"Failed to read from S3: {str(e)}")
                errors.append({
                    "symbol": sym, 
                    "error": error_info.get("original", error_info["message"]),
                    "category": error_info["category"]
                })

        # Fallback a orders inline (legacy)
        if not orders and item.get("orders"):
            orders = item.get("orders")
            print(f"Using inline orders for {sym}: {len(orders) if orders else 0} orders")

        # Si no hay orders de ninguna forma, skip
        if not orders:
            if sym:  # Solo contar símbolos conocidos
                error_info = _categorize_error("No orders data available")
                errors.append({
                    "symbol": sym, 
                    "error": error_info.get("original", error_info["message"]),
                    "category": error_info["category"]
                })
            continue

        # Si llegamos aquí, hay datos válidos
        orders_count = 0
        for jdx, o in enumerate(orders):
            if isinstance(o, dict):
                # Agregar metadatos si no existen
                o.setdefault("_symbol", sym)
                all_orders.append(o)
                orders_count += 1
            else:
                error_info = _categorize_error(f"Invalid order data at index {jdx}: {type(o).__name__}")
                errors.append({
                    "symbol": sym, 
                    "error": error_info.get("original", error_info["message"]),
                    "category": error_info["category"]
                })

        if orders_count > 0:
            total_symbols_with_data += 1

    # Orden cronológico DESC (más reciente primero)
    print(f"Sorting {len(all_orders)} total orders by timestamp...")
    all_orders.sort(key=_order_time_safe, reverse=True)

    # Crear resumen de errores por categoría con detalles
    error_summary = {}
    error_details = {}
    
    for error in errors:
        category = error.get("category", "unknown")
        if category not in error_summary:
            error_summary[category] = 0
            error_details[category] = {"count": 0, "examples": []}
        
        error_summary[category] += 1
        error_details[category]["count"] += 1
        
        # Solo agregar algunos ejemplos para evitar respuestas enormes
        if len(error_details[category]["examples"]) < 3:
            error_details[category]["examples"].append({
                "symbol": error.get("symbol"),
                "message": error.get("error", "Unknown error")[:200]  # Truncar mensajes largos
            })

    # Timing del agregador
    aggregator_end_time = datetime.now(timezone.utc)
    aggregator_duration_seconds = (aggregator_end_time - aggregator_start_time).total_seconds()

    # Crear resumen final (sin órdenes para optimización)
    final_summary = {
        "total_orders": len(all_orders),
        "symbols_processed": total_symbols_processed,
        "symbols_with_data": total_symbols_with_data,
        "error_count": len(errors),
        "error_summary": error_summary,
        "error_details": error_details,
        "processing_timestamp": aggregator_end_time.isoformat(),
        "aggregator_duration_seconds": round(aggregator_duration_seconds, 3),
        "timing": {
            "aggregator_start": aggregator_start_time.isoformat(),
            "aggregator_end": aggregator_end_time.isoformat(),
            "aggregator_duration_seconds": round(aggregator_duration_seconds, 3)
        }
    }

    # Guardar el resultado completo en S3 (si está configurado)
    if RESULTS_BUCKET:
        now = datetime.now(timezone.utc)
        key = _results_key(now)
        try:
            # payload completo para S3
            full_payload = {
                **final_summary,
                "orders": all_orders
            }
            print(f"Storing complete results in S3: s3://{RESULTS_BUCKET}/{key}")
            S3.put_object(
                Bucket=RESULTS_BUCKET,
                Key=key,
                Body=json.dumps(full_payload, ensure_ascii=False, indent=2).encode("utf-8"),
                ContentType="application/json; charset=utf-8",
            )
            # En la respuesta: punteros al archivo
            final_summary["s3_uri"] = f"s3://{RESULTS_BUCKET}/{key}"
            final_summary["public_url"] = f"https://{RESULTS_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{key}"
            print(f"Results stored successfully in S3")
            
            if per_symbol_keys_to_delete:
                print(f"�� Starting cleanup of {len(per_symbol_keys_to_delete)} per-symbol files...")
                cleanup_result = _delete_per_symbol_files(per_symbol_keys_to_delete)
                final_summary["cleanup"] = cleanup_result
                
                if cleanup_result.get("cleaned"):
                    print(f"✅ Cleanup successful: {cleanup_result.get('deleted_count', 0)} files deleted")
                else:
                    print(f"⚠️ Cleanup failed or skipped: {cleanup_result.get('reason', 'unknown')}")
            else:
                final_summary["cleanup"] = {"cleaned": False, "reason": "no per-symbol files to clean"}
                
        except Exception as e:
            error_msg = f"S3 put_object failed: {str(e)}"
            errors.append({"symbol": None, "error": error_msg})
            print(f"ERROR: {error_msg}")
    else:
        warning_msg = "RESULTS_BUCKET env var is not set; skipping S3 upload"
        errors.append({"symbol": None, "error": warning_msg})
        print(f"WARNING: {warning_msg}")
        final_summary["cleanup"] = {"cleaned": False, "reason": "S3 not configured"}

    print(f"Aggregator completed: {len(all_orders)} orders from {total_symbols_with_data}/{total_symbols_processed} symbols")
    return final_summary
