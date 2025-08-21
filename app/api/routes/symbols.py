"""
Endpoints para obtener símbolos desde la API de Bitget.
"""

from fastapi import APIRouter, HTTPException
from typing import Dict, Any, List
from app.services.symbols_service import SymbolsService

# Crear router para símbolos
router = APIRouter(prefix="/symbols", tags=["symbols"])

# Inicializar servicio de símbolos
symbols_service = SymbolsService()

@router.get("", response_model=Dict[str, Any])
def get_symbols():
    """
    Obtener símbolos consolidados solo de endpoints v2 (sin guión bajo) desde la API de Bitget.
    
    Consulta y consolida símbolos únicos de:
    
    FUTUROS V2 (product_types sin guión bajo):
    - usdt-futures: USDT Futures
    - usdc-futures: USDC Futures
    - coin-futures: Coin Futures
    
    SPOT PÚBLICOS V2:
    - Todos los pares de trading spot disponibles
    
    NOTA: 
    - Ya NO incluye símbolos con guión bajo del endpoint v1
    - Incluye TODOS los símbolos independientemente de su status (online, offline, etc.)
    
    Returns:
        Lista consolidada y única de símbolos v2 con TODOS los status.
        Mantiene la estructura original con 'symbol' y 'status' (status original de la API).
        Incluye resumen detallado por tipo en product_types_summary.
    """
    return symbols_service.get_bitget_symbols()
