import time
import requests
import json
import boto3
from datetime import datetime
from typing import Dict, Any, List
from sqlalchemy.orm import Session
from app.models.database import ExecutionResult, Order, ProcessingLog, get_db_session, create_tables
import logging

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DatabaseService:
    """Servicio para manejar operaciones de base de datos"""
    
    def __init__(self):
        # Crear tablas si no existen
        self.db_available = create_tables()
        if not self.db_available:
            logger.warning("Base de datos no disponible - funcionando en modo sin BD")
    
    def save_execution_data(self, execution_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Guarda los datos de ejecución en la base de datos y mide el tiempo de procesamiento
        
        Args:
            execution_data: Datos de la ejecución que incluyen status, result, executionArn
            
        Returns:
            Dict con información del procesamiento incluyendo tiempo transcurrido
        """
        start_time = time.time()
        
        # Si la base de datos no está disponible, devolver respuesta simulada
        if not self.db_available:
            logger.warning("Base de datos no disponible - simulando guardado de datos")
            return {
                "message": "Datos recibidos pero no guardados (BD no disponible)",
                "execution_arn": execution_data.get("executionArn"),
                "status": execution_data.get("status"),
                "processing_time": time.time() - start_time,
                "records_saved": 0,
                "database_available": False
            }
        
        session = get_db_session()
        if not session:
            logger.error("No se pudo obtener sesión de base de datos")
            return {
                "error": "No se pudo conectar a la base de datos",
                "execution_arn": execution_data.get("executionArn"),
                "processing_time": time.time() - start_time,
                "database_available": False
            }
        
        try:
            execution_arn = execution_data.get("executionArn")
            status = execution_data.get("status")
            result = execution_data.get("result", {})
            
            # Log del inicio del procesamiento
            processing_log = ProcessingLog(
                execution_arn=execution_arn,
                level="INFO",
                message="Iniciando guardado de datos de ejecución",
                details=json.dumps({
                    "action": "data_save",
                    "status": status,
                    "total_symbols": result.get("total_symbols"),
                    "records_to_process": len(result.get("orders", []))
                })
            )
            session.add(processing_log)
            session.commit()
            
            # Guardar o actualizar el resultado de ejecución
            execution_result = session.query(ExecutionResult).filter_by(
                execution_arn=execution_arn
            ).first()
            
            if execution_result:
                # Actualizar registro existente
                execution_result.status = status
                execution_result.total_symbols = result.get("total_symbols")
                execution_result.total_orders = result.get("total_orders")
                execution_result.s3_uri = result.get("s3_uri")
                execution_result.public_url = result.get("public_url")
                execution_result.updated_at = datetime.utcnow()
            else:
                # Crear nuevo registro
                execution_result = ExecutionResult(
                    execution_arn=execution_arn,
                    status=status,
                    total_symbols=result.get("total_symbols"),
                    total_orders=result.get("total_orders"),
                    s3_uri=result.get("s3_uri"),
                    public_url=result.get("public_url")
                )
                session.add(execution_result)
            
            orders_saved = 0
            
            # Guardar órdenes si están disponibles
            if status == "SUCCEEDED":
                orders_to_save = []
                
                # Primero intentar obtener órdenes desde la respuesta (para compatibilidad)
                if "orders" in result and result["orders"]:
                    orders_to_save = result["orders"]
                    logger.info(f"Usando órdenes de la respuesta: {len(orders_to_save)} órdenes")
                
                # Si no hay órdenes en la respuesta, intentar obtener desde S3
                elif "s3_uri" in result and result["s3_uri"]:
                    logger.info("No se encontraron órdenes en la respuesta, obteniendo desde S3...")
                    orders_to_save = self._get_orders_from_s3(result["s3_uri"])
                
                # Si tenemos órdenes, guardarlas
                if orders_to_save:
                    orders_saved = self._save_orders(session, execution_arn, orders_to_save)
                    logger.info(f"Guardadas {orders_saved} órdenes en la base de datos")
                else:
                    logger.warning("No se encontraron órdenes para guardar (ni en respuesta ni en S3)")
            
            # Calcular tiempo de procesamiento
            end_time = time.time()
            processing_time = end_time - start_time
            
            # Actualizar tiempo de procesamiento
            execution_result.processing_time_seconds = processing_time
            
            # Log de éxito
            success_log = ProcessingLog(
                execution_arn=execution_arn,
                level="INFO",
                message=f"Datos guardados exitosamente - {orders_saved} órdenes en {processing_time:.2f}s",
                details=json.dumps({
                    "action": "data_save_completed",
                    "orders_saved": orders_saved,
                    "processing_time_seconds": processing_time,
                    "success": True
                })
            )
            session.add(success_log)
            
            session.commit()
            
            logger.info(f"Datos guardados exitosamente para {execution_arn}. "
                       f"Órdenes: {orders_saved}, Tiempo: {processing_time:.2f}s")
            
            return {
                "success": True,
                "execution_arn": execution_arn,
                "orders_saved": orders_saved,
                "processing_time_seconds": processing_time,
                "total_symbols": result.get("total_symbols"),
                "total_orders": result.get("total_orders")
            }
            
        except Exception as e:
            session.rollback()
            
            # Log del error
            error_log = ProcessingLog(
                execution_arn=execution_arn,
                level="ERROR",
                message=f"Error guardando datos de ejecución: {str(e)}",
                details=json.dumps({
                    "action": "data_save_error",
                    "error": str(e),
                    "processing_time_seconds": time.time() - start_time
                })
            )
            session.add(error_log)
            session.commit()
            
            logger.error(f"Error guardando datos para {execution_arn}: {str(e)}")
            
            return {
                "success": False,
                "execution_arn": execution_arn,
                "error": str(e),
                "processing_time_seconds": time.time() - start_time
            }
            
        finally:
            session.close()
    
    def _get_orders_from_s3(self, s3_uri: str) -> List[Dict[str, Any]]:
        """
        Obtiene órdenes desde S3 usando la URI completa
        
        Args:
            s3_uri: URI de S3 en formato s3://bucket/key
            
        Returns:
            Lista de órdenes o lista vacía si hay error
        """
        try:
            # Parsear la URI de S3
            if not s3_uri or not s3_uri.startswith("s3://"):
                logger.warning(f"URI de S3 inválida: {s3_uri}")
                return []
            
            # Extraer bucket y key de la URI
            parts = s3_uri[5:].split("/", 1)  # Remover "s3://" y dividir en bucket/key
            if len(parts) != 2:
                logger.warning(f"Formato de URI de S3 inválido: {s3_uri}")
                return []
            
            bucket, key = parts
            
            # Crear cliente S3
            s3_client = boto3.client('s3')
            
            # Obtener objeto desde S3
            logger.info(f"Obteniendo órdenes desde S3: {bucket}/{key}")
            response = s3_client.get_object(Bucket=bucket, Key=key)
            content = response['Body'].read().decode('utf-8')
            data = json.loads(content)
            
            # Las órdenes deberían estar en data["orders"]
            orders = data.get("orders", [])
            if not isinstance(orders, list):
                logger.warning(f"Formato inesperado de órdenes en S3: {type(orders)}")
                return []
            
            logger.info(f"Se obtuvieron {len(orders)} órdenes desde S3")
            return orders
            
        except Exception as e:
            logger.error(f"Error obteniendo órdenes desde S3 ({s3_uri}): {str(e)}")
            return []
    
    def _save_orders(self, session: Session, execution_arn: str, orders: List[Dict[str, Any]]) -> int:
        """
        Guarda las órdenes en la base de datos
        
        Args:
            session: Sesión de SQLAlchemy
            execution_arn: ARN de la ejecución
            orders: Lista de órdenes
            
        Returns:
            Número de órdenes guardadas
        """
        # Primero eliminar órdenes existentes para esta ejecución (en caso de re-procesamiento)
        session.query(Order).filter_by(execution_arn=execution_arn).delete()
        
        orders_saved = 0
        
        for order_data in orders:
            try:
                order = Order(
                    execution_arn=execution_arn,
                    symbol=order_data.get("symbol"),
                    size=order_data.get("size"),
                    order_id=order_data.get("orderId"),
                    client_oid=order_data.get("clientOid"),
                    base_volume=order_data.get("baseVolume"),
                    fee=order_data.get("fee"),
                    price=order_data.get("price"),
                    price_avg=order_data.get("priceAvg"),
                    status=order_data.get("status"),
                    side=order_data.get("side"),
                    force=order_data.get("force"),
                    total_profits=order_data.get("totalProfits"),
                    pos_side=order_data.get("posSide"),
                    margin_coin=order_data.get("marginCoin"),
                    quote_volume=order_data.get("quoteVolume"),
                    leverage=order_data.get("leverage"),
                    margin_mode=order_data.get("marginMode"),
                    enter_point_source=order_data.get("enterPointSource"),
                    trade_side=order_data.get("tradeSide"),
                    pos_mode=order_data.get("posMode"),
                    order_type=order_data.get("orderType"),
                    order_source=order_data.get("orderSource"),
                    preset_stop_surplus_price=order_data.get("presetStopSurplusPrice"),
                    preset_stop_loss_price=order_data.get("presetStopLossPrice"),
                    pos_avg=order_data.get("posAvg"),
                    reduce_only=order_data.get("reduceOnly"),
                    c_time=order_data.get("cTime"),
                    u_time=order_data.get("uTime")
                )
                session.add(order)
                orders_saved += 1
                
            except Exception as e:
                logger.warning(f"Error guardando orden {order_data.get('orderId')}: {str(e)}")
                continue
        
        return orders_saved
    
    def get_execution_data(self, execution_arn: str) -> Dict[str, Any]:
        """
        Obtiene datos de ejecución de la base de datos
        
        Args:
            execution_arn: ARN de la ejecución
            
        Returns:
            Diccionario con los datos de la ejecución
        """
        if not self.db_available:
            logger.warning("Base de datos no disponible - no se pueden obtener datos de ejecución")
            return {
                "found": False,
                "error": "Base de datos no disponible",
                "database_available": False
            }
        
        session = get_db_session()
        if not session:
            return {
                "found": False,
                "error": "No se pudo conectar a la base de datos",
                "database_available": False
            }
        
        try:
            execution_result = session.query(ExecutionResult).filter_by(
                execution_arn=execution_arn
            ).first()
            
            if not execution_result:
                return {"found": False}
            
            orders_count = session.query(Order).filter_by(
                execution_arn=execution_arn
            ).count()
            
            return {
                "found": True,
                "execution_arn": execution_result.execution_arn,
                "status": execution_result.status,
                "total_symbols": execution_result.total_symbols,
                "total_orders": execution_result.total_orders,
                "orders_in_db": orders_count,
                "s3_uri": execution_result.s3_uri,
                "public_url": execution_result.public_url,
                "processing_time_seconds": execution_result.processing_time_seconds,
                "created_at": execution_result.created_at.isoformat() if execution_result.created_at else None,
                "updated_at": execution_result.updated_at.isoformat() if execution_result.updated_at else None
            }
            
        finally:
            session.close()
    
    def get_execution_orders_paginated(self, execution_arn: str, skip: int = 0, limit: int = 10) -> Dict[str, Any]:
        """
        Obtiene las órdenes de una ejecución con paginación
        
        Args:
            execution_arn: ARN de la ejecución
            skip: Número de registros a saltar
            limit: Número máximo de registros a retornar
            
        Returns:
            Diccionario con las órdenes paginadas y metadatos
        """
        if not self.db_available:
            logger.warning("Base de datos no disponible - no se pueden obtener órdenes")
            return {
                "found": False,
                "error": "Base de datos no disponible",
                "database_available": False
            }
        
        session = get_db_session()
        if not session:
            return {
                "found": False,
                "error": "No se pudo conectar a la base de datos",
                "database_available": False
            }
        
        try:
            # Verificar que existe la ejecución
            execution_result = session.query(ExecutionResult).filter_by(
                execution_arn=execution_arn
            ).first()
            
            if not execution_result:
                return {"found": False}
            
            # Obtener total de órdenes
            total_orders = session.query(Order).filter_by(
                execution_arn=execution_arn
            ).count()
            
            # Obtener órdenes con paginación
            orders = session.query(Order).filter_by(
                execution_arn=execution_arn
            ).offset(skip).limit(limit).all()
            
            # Convertir órdenes a diccionarios
            orders_data = []
            for order in orders:
                order_dict = {
                    "id": order.id,
                    "symbol": order.symbol,
                    "order_id": order.order_id,
                    "size": order.size,
                    "price": order.price,
                    "price_avg": order.price_avg,
                    "base_volume": order.base_volume,
                    "quote_volume": order.quote_volume,
                    "status": order.status,
                    "side": order.side,
                    "order_type": order.order_type,
                    "force": order.force,
                    "leverage": order.leverage,
                    "margin_mode": order.margin_mode,
                    "margin_coin": order.margin_coin,
                    "pos_side": order.pos_side,
                    "pos_mode": order.pos_mode,
                    "trade_side": order.trade_side,
                    "reduce_only": order.reduce_only,
                    "pos_avg": order.pos_avg,
                    "fee": order.fee,
                    "total_profits": order.total_profits,
                    "client_oid": order.client_oid,
                    "order_source": order.order_source,
                    "enter_point_source": order.enter_point_source,
                    "preset_stop_surplus_price": order.preset_stop_surplus_price,
                    "preset_stop_loss_price": order.preset_stop_loss_price,
                    "c_time": order.c_time,
                    "u_time": order.u_time,
                    "created_at": order.created_at.isoformat() if order.created_at else None
                }
                orders_data.append(order_dict)
            
            return {
                "found": True,
                "execution_info": {
                    "execution_arn": execution_result.execution_arn,
                    "status": execution_result.status,
                    "total_symbols": execution_result.total_symbols,
                    "total_orders": execution_result.total_orders,
                    "s3_uri": execution_result.s3_uri,
                    "public_url": execution_result.public_url,
                    "processing_time_seconds": execution_result.processing_time_seconds,
                    "created_at": execution_result.created_at.isoformat() if execution_result.created_at else None,
                    "updated_at": execution_result.updated_at.isoformat() if execution_result.updated_at else None
                },
                "orders": orders_data,
                "pagination": {
                    "total_orders": total_orders,
                    "skip": skip,
                    "limit": limit,
                    "current_count": len(orders_data),
                    "has_more": (skip + limit) < total_orders,
                    "next_skip": skip + limit if (skip + limit) < total_orders else None,
                    "prev_skip": max(0, skip - limit) if skip > 0 else None
                }
            }
            
        finally:
            session.close()
    
    def fetch_and_save_from_public_url(self, execution_arn: str, public_url: str) -> Dict[str, Any]:
        """
        Obtiene datos del JSON público y los guarda en la base de datos
        
        Args:
            execution_arn: ARN de la ejecución
            public_url: URL pública del JSON
            
        Returns:
            Resultado del procesamiento
        """
        start_time = time.time()
        
        try:
            # Descargar datos del JSON
            logger.info(f"Descargando datos de {public_url}")
            response = requests.get(public_url, timeout=30)
            response.raise_for_status()
            
            json_data = response.json()
            
            # Crear estructura de datos compatible
            execution_data = {
                "status": "SUCCEEDED",
                "executionArn": execution_arn,
                "result": json_data
            }
            
            # Guardar en base de datos
            result = self.save_execution_data(execution_data)
            
            download_time = time.time() - start_time
            result["download_time_seconds"] = download_time
            result["total_processing_time_seconds"] = download_time + result.get("processing_time_seconds", 0)
            
            return result
            
        except Exception as e:
            logger.error(f"Error procesando URL pública {public_url}: {str(e)}")
            return {
                "success": False,
                "execution_arn": execution_arn,
                "error": str(e),
                "processing_time_seconds": time.time() - start_time
            }

    def list_all_executions(self) -> List[Dict[str, Any]]:
        """
        Lista todas las ejecuciones guardadas en la base de datos.
        
        Returns:
            Lista de diccionarios con información de todas las ejecuciones
        """
        if not self.db_available:
            logger.warning("Base de datos no disponible - no se pueden listar ejecuciones")
            return []
        
        session = get_db_session()
        if not session:
            logger.error("No se pudo obtener sesión de base de datos")
            return []
        
        try:
            executions = session.query(ExecutionResult).order_by(ExecutionResult.created_at.desc()).all()
            
            result = []
            for execution in executions:
                # Contar órdenes asociadas
                orders_count = session.query(Order).filter_by(execution_arn=execution.execution_arn).count()
                
                result.append({
                    "execution_arn": execution.execution_arn,
                    "status": execution.status,
                    "total_symbols": execution.total_symbols,
                    "total_orders": execution.total_orders,
                    "orders_in_db": orders_count,
                    "s3_uri": execution.s3_uri,
                    "public_url": execution.public_url,
                    "processing_time_seconds": execution.processing_time_seconds,
                    "created_at": execution.created_at.isoformat() if execution.created_at else None,
                    "updated_at": execution.updated_at.isoformat() if execution.updated_at else None
                })
            
            return result
            
        except Exception as e:
            logger.error(f"Error listando ejecuciones: {str(e)}")
            raise
        finally:
            session.close()
