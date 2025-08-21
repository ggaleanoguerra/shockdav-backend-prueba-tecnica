"""
Endpoints relacionados con órdenes de Bitget.
"""

from fastapi import APIRouter, HTTPException, Query
from typing import Dict, Any
from app.services.orders_service import OrdersService, OrderRequest

# Crear router para órdenes
router = APIRouter(prefix="/orders", tags=["orders"])

# Inicializar servicio de órdenes
orders_service = OrdersService()

@router.post("", response_model=Dict[str, Any])
def start_orders(req: OrderRequest):
    """Inicia la ejecución orquestada invocando la Lambda coordinadora."""
    return orders_service.start_order_execution(req)

@router.get("/list", response_model=Dict[str, Any])
def list_executions():
    """Lista todas las ejecuciones guardadas en la base de datos."""
    return orders_service.list_all_executions()

@router.get("/{execution_arn}", response_model=Dict[str, Any])
def get_orders_status_path(execution_arn: str):
    """Consulta estado por path param. Automáticamente guarda los datos en base de datos."""
    return orders_service.get_execution_status(execution_arn)


@router.post("/{execution_arn}/save-from-url", response_model=Dict[str, Any])
def save_data_from_public_url(
    execution_arn: str, 
    public_url: str = Query(..., description="URL pública del JSON")
):
    """Descarga datos del JSON público y los guarda en la base de datos."""
    return orders_service.save_data_from_public_url(execution_arn, public_url)

@router.get("/{execution_arn}/database", response_model=Dict[str, Any])
def get_database_data(
    execution_arn: str,
    skip: int = Query(0, ge=0, description="Número de registros a saltar"),
    limit: int = Query(10, ge=1, le=100, description="Número máximo de registros a retornar")
):
    """Obtiene los datos guardados en la base de datos para una ejecución específica con paginación de órdenes."""
    return orders_service.get_database_data(execution_arn, skip, limit)
