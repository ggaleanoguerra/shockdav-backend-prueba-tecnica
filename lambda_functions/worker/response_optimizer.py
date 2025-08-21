import json
import sys
from typing import Dict, List, Any, Optional

MAX_RESPONSE_SIZE = 180 * 1024  # Reducido a 180KB para máxima seguridad

ESTIMATED_ORDER_SIZE = 500

def estimate_response_size(data: Any) -> int:
    """
    Estima el tamaño en bytes de un objeto al serializarlo a JSON.
    """
    try:
        json_str = json.dumps(data, ensure_ascii=False)
        return len(json_str.encode('utf-8'))
    except Exception:
        # Si no se puede serializar, asumir tamaño grande
        return MAX_RESPONSE_SIZE + 1

def optimize_orders_response(
    symbol: str, 
    orders: List[Dict[str, Any]], 
    s3_metadata: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """
    Optimiza la respuesta del worker para GARANTIZAR que no se supere 256KB.
    
    Args:
        symbol: Símbolo procesado
        orders: Lista de órdenes
        s3_metadata: Metadatos de S3 si se guardó ahí
    
    Returns:
        Respuesta optimizada que NUNCA supera el límite
    """
    total_orders = len(orders)
    
    if s3_metadata:
        response = {
            "symbol": symbol,
            "count": total_orders,
            "s3_key": s3_metadata["s3_key"],
            "s3_uri": s3_metadata["s3_uri"],
            "public_url": s3_metadata["public_url"],
            "error": None,
            "data_location": "s3"
        }
        return response
    
    # Calcular número máximo de órdenes que caben SEGURAMENTE
    base_response_size = estimate_response_size({
        "symbol": symbol,
        "count": total_orders,
        "truncated": True,
        "error": "truncated",
        "data_location": "inline"
    })
    
    available_space = MAX_RESPONSE_SIZE - base_response_size
    max_safe_orders = max(1, available_space // ESTIMATED_ORDER_SIZE)
    
    max_safe_orders = min(max_safe_orders, 20)
    
    if total_orders <= max_safe_orders:
        response = {
            "symbol": symbol,
            "orders": orders,
            "count": total_orders,
            "error": None,
            "data_location": "inline"
        }
        
        # Verificación final de seguridad
        if estimate_response_size(response) < MAX_RESPONSE_SIZE:
            return response
    
    return {
        "symbol": symbol,
        "count": total_orders,
        "orders": [],  # NO incluir órdenes para garantizar tamaño mínimo
        "truncated": True,
        "error": f"Response contains {total_orders} orders. Configure RESULTS_BUCKET for full data access.",
        "data_location": "none",
        "recommendation": "Configure S3 storage to access complete datasets"
    }

def create_minimal_response(symbol: str, total_orders: int, error_msg: str) -> Dict[str, Any]:
    """
    Crea una respuesta mínima cuando hay errores o datos muy grandes.
    """
    return {
        "symbol": symbol,
        "count": total_orders,
        "orders": [],
        "truncated": total_orders > 0,
        "s3_key": None,
        "s3_uri": None,
        "error": error_msg
    }

def create_summary_only_response(
    symbol: str,
    total_orders: int,
    s3_metadata: Optional[Dict[str, str]] = None,
    error_msg: Optional[str] = None
) -> Dict[str, Any]:
    """
    Crea una respuesta que solo incluye metadatos de resumen, sin órdenes.
    Útil para reducir el tamaño de la respuesta y evitar duplicar datos ya guardados en S3.
    
    Args:
        symbol: Símbolo procesado
        total_orders: Número total de órdenes
        s3_metadata: Metadatos de S3 si se guardó ahí
        error_msg: Mensaje de error si lo hay
    
    Returns:
        Respuesta optimizada solo con metadatos
    """
    response = {
        "symbol": symbol,
        "count": total_orders,
        "error": error_msg
    }
    
    if s3_metadata:
        response.update({
            "s3_key": s3_metadata["s3_key"],
            "s3_uri": s3_metadata["s3_uri"],
            "public_url": s3_metadata["public_url"]
        })
    
    return response