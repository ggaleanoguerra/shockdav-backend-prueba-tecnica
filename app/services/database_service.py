import time
import requests
import json
import boto3
from datetime import datetime
from typing import Dict, Any, List
from sqlalchemy.orm import Session
from app.models.database import ExecutionResult, Order, ProcessingLog, get_db_session, create_tables, get_bogota_now
import logging

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DatabaseService:
    """Servicio para manejar operaciones de base de datos"""
    
    def __init__(self):
        # Crear tablas si no existen
        logger.info("🔗 Inicializando DatabaseService...")
        try:
            self.db_available = create_tables()
            if self.db_available:
                logger.info("✅ DatabaseService inicializado - Base de datos disponible")
            else:
                logger.warning("⚠️ DatabaseService inicializado - Base de datos NO disponible")
        except Exception as e:
            logger.error(f"❌ Error inicializando DatabaseService: {str(e)}")
            self.db_available = False
    
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
            error_msg = "No se pudo obtener sesión de base de datos"
            logger.error(f"❌ {error_msg}")
            return {
                "error": error_msg,
                "execution_arn": execution_data.get("executionArn"),
                "processing_time": time.time() - start_time,
                "database_available": False,
                "debug_info": {
                    "db_available": self.db_available,
                    "session_created": False,
                    "stage": "session_creation"
                }
            }
        
        logger.info(f"🔄 Iniciando guardado para execution: {execution_data.get('executionArn')}")
        
        try:
            execution_arn = execution_data.get("executionArn")
            status = execution_data.get("status")
            result = execution_data.get("result", {})
            
            logger.info(f"📊 Datos recibidos - ARN: {execution_arn}, Status: {status}, "
                       f"Orders en result: {len(result.get('orders', []))}, "
                       f"Total symbols: {result.get('total_symbols') or result.get('symbols_processed', 'N/A')}, "
                       f"Symbols with data: {result.get('symbols_with_data', 'N/A')}")
            
            # Log del inicio del procesamiento
            processing_log = ProcessingLog(
                execution_arn=execution_arn,
                level="INFO",
                message="Iniciando guardado de datos de ejecución",
                details=json.dumps({
                    "action": "data_save",
                    "status": status,
                    "total_symbols": result.get("total_symbols") or result.get("symbols_processed"),
                    "symbols_with_data": result.get("symbols_with_data"),
                    "records_to_process": len(result.get("orders", []))
                })
            )
            session.add(processing_log)
            session.commit()
            logger.info("✅ Log de inicio guardado correctamente")
            
            # Guardar o actualizar el resultado de ejecución
            logger.info(f"🔍 Buscando execution_result existente para: {execution_arn}")
            execution_result = session.query(ExecutionResult).filter_by(
                execution_arn=execution_arn
            ).first()
            
            if execution_result:
                # Actualizar registro existente
                logger.info("📝 Actualizando execution_result existente")
                execution_result.status = status
                execution_result.total_symbols = result.get("total_symbols") or result.get("symbols_processed")
                execution_result.total_orders = result.get("total_orders")
                execution_result.s3_uri = result.get("s3_uri")
                execution_result.public_url = result.get("public_url")
                execution_result.updated_at = get_bogota_now()
                
                logger.info(f"📝 Valores actualizados - Symbols: {execution_result.total_symbols}, Orders: {execution_result.total_orders}")
            else:
                # Crear nuevo registro
                logger.info("➕ Creando nuevo execution_result")
                total_symbols_value = result.get("total_symbols") or result.get("symbols_processed")
                
                execution_result = ExecutionResult(
                    execution_arn=execution_arn,
                    status=status,
                    total_symbols=total_symbols_value,
                    total_orders=result.get("total_orders"),
                    s3_uri=result.get("s3_uri"),
                    public_url=result.get("public_url")
                )
                session.add(execution_result)
                
                logger.info(f"➕ Nuevo registro creado - Symbols: {total_symbols_value}, Orders: {result.get('total_orders')}")
                
            # Agregar información adicional al result_data si está disponible
            result_data = {
                "symbols_processed": result.get("symbols_processed"),
                "symbols_with_data": result.get("symbols_with_data"),
                "error_count": result.get("error_count", 0),
                "processing_timestamp": result.get("processing_timestamp"),
                "aggregator_duration_seconds": result.get("aggregator_duration_seconds"),
                "cleanup": result.get("cleanup", {}),
                "timing": result.get("timing", {})
            }
            execution_result.result_data = json.dumps(result_data)
            logger.info(f"📋 Result_data guardado con información completa de procesamiento")
            
            logger.info("💾 Commiteando execution_result...")
            session.commit()
            logger.info("✅ Execution_result guardado correctamente")
            
            orders_saved = 0
            
            # Guardar órdenes si están disponibles
            if status == "SUCCEEDED":
                logger.info(f"📦 Status SUCCEEDED - Iniciando proceso de guardado de órdenes")
                orders_to_save = []
                
                # Primero intentar obtener órdenes desde la respuesta (para compatibilidad)
                if "orders" in result and result["orders"]:
                    orders_to_save = result["orders"]
                    logger.info(f"📋 Usando órdenes de la respuesta: {len(orders_to_save)} órdenes")
                
                # Si no hay órdenes en la respuesta, intentar obtener desde S3
                elif "s3_uri" in result and result["s3_uri"]:
                    logger.info(f"🔄 No hay órdenes en respuesta, obteniendo desde S3: {result['s3_uri']}")
                    orders_to_save = self._get_orders_from_s3(result["s3_uri"])
                    logger.info(f"📥 Obtenidas {len(orders_to_save)} órdenes desde S3")
                
                # Si tenemos órdenes, guardarlas
                if orders_to_save:
                    logger.info(f"💾 Iniciando guardado de {len(orders_to_save)} órdenes...")
                    try:
                        orders_saved = self._save_orders(session, execution_arn, orders_to_save)
                        logger.info(f"✅ Guardadas {orders_saved} órdenes en la base de datos")
                    except Exception as orders_error:
                        logger.error(f"❌ Error guardando órdenes: {str(orders_error)}")
                        raise orders_error
                else:
                    logger.warning("⚠️ No se encontraron órdenes para guardar (ni en respuesta ni en S3)")
            else:
                logger.info(f"🔄 Status '{status}' - No guardando órdenes (solo para SUCCEEDED)")
            
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
                       f"Órdenes: {orders_saved}, Símbolos: {execution_result.total_symbols}, Tiempo: {processing_time:.2f}s")
            
            return {
                "success": True,
                "execution_arn": execution_arn,
                "orders_saved": orders_saved,
                "processing_time_seconds": processing_time,
                "total_symbols": result.get("total_symbols") or result.get("symbols_processed"),
                "symbols_processed": result.get("symbols_processed"),
                "symbols_with_data": result.get("symbols_with_data"),
                "total_orders": result.get("total_orders"),
                "database_record": {
                    "total_symbols_in_db": execution_result.total_symbols,
                    "total_orders_in_db": execution_result.total_orders,
                    "status_in_db": execution_result.status,
                    "has_result_data": bool(execution_result.result_data),
                    "s3_uri_saved": bool(execution_result.s3_uri)
                }
            }
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"❌ ERROR CRÍTICO guardando datos para {execution_arn}: {error_msg}")
            logger.error(f"🔍 Tipo de error: {type(e).__name__}")
            
            # Capturar información específica del error de base de datos
            if "Data too long for column" in error_msg:
                logger.error("🚨 ERROR DE TAMAÑO DE COLUMNA DETECTADO:")
                logger.error(f"   Mensaje completo: {error_msg}")
                # Extraer información del campo problemático
                import re
                column_match = re.search(r"'(\w+)' at row (\d+)", error_msg)
                if column_match:
                    column_name, row_number = column_match.groups()
                    logger.error(f"   🎯 Campo problemático: '{column_name}' en fila {row_number}")
            
            session.rollback()
            logger.info("🔄 Rollback de sesión completado")
            
            # Log del error con más detalles
            try:
                error_log = ProcessingLog(
                    execution_arn=execution_arn,
                    level="ERROR",
                    message=f"Error guardando datos de ejecución: {error_msg}",
                    details=json.dumps({
                        "action": "data_save_error",
                        "error": error_msg,
                        "error_type": type(e).__name__,
                        "processing_time_seconds": time.time() - start_time,
                        "execution_data_keys": list(execution_data.keys()) if execution_data else [],
                        "result_keys": list(result.keys()) if result else []
                    })
                )
                session.add(error_log)
                session.commit()
                logger.info("📝 Log de error guardado en base de datos")
            except Exception as log_error:
                logger.error(f"⚠️ No se pudo guardar el log de error: {str(log_error)}")
            
            return {
                "success": False,
                "execution_arn": execution_arn,
                "error": error_msg,
                "error_type": type(e).__name__,
                "processing_time_seconds": time.time() - start_time,
                "debug_info": {
                    "stage": "data_save_main",
                    "has_orders": "orders" in result if result else False,
                    "has_s3_uri": "s3_uri" in result if result else False,
                    "orders_count": len(result.get("orders", [])) if result else 0
                }
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
        logger.info(f"🗑️ Eliminando órdenes existentes para execution: {execution_arn}")
        # Primero eliminar órdenes existentes para esta ejecución (en caso de re-procesamiento)
        deleted_count = session.query(Order).filter_by(execution_arn=execution_arn).delete()
        logger.info(f"🗑️ Eliminadas {deleted_count} órdenes existentes")
        
        orders_saved = 0
        
        for idx, order_data in enumerate(orders):
            try:
                # Log detallado de valores problemáticos antes de crear la orden
                side_value = order_data.get("side")
                trade_side_value = order_data.get("tradeSide")
                order_source_value = order_data.get("orderSource")
                
                logger.info(f"📋 Procesando orden {idx+1}/{len(orders)} - "
                           f"OrderID: {order_data.get('orderId')}, "
                           f"Side: '{side_value}' (len={len(str(side_value)) if side_value else 0}), "
                           f"TradeSide: '{trade_side_value}' (len={len(str(trade_side_value)) if trade_side_value else 0}), "
                           f"OrderSource: '{order_source_value}' (len={len(str(order_source_value)) if order_source_value else 0})")
                
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
                    side=side_value,
                    force=order_data.get("force"),
                    total_profits=order_data.get("totalProfits"),
                    pos_side=order_data.get("posSide"),
                    margin_coin=order_data.get("marginCoin"),
                    quote_volume=order_data.get("quoteVolume"),
                    leverage=order_data.get("leverage"),
                    margin_mode=order_data.get("marginMode"),
                    enter_point_source=order_data.get("enterPointSource"),
                    trade_side=trade_side_value,
                    pos_mode=order_data.get("posMode"),
                    order_type=order_data.get("orderType"),
                    order_source=order_source_value,
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
                error_msg = str(e)
                logger.error(f"❌ Error guardando orden {order_data.get('orderId')}: {error_msg}")
                logger.error(f"🔍 Datos problemáticos de la orden:")
                logger.error(f"   Side: '{side_value}' (longitud: {len(str(side_value)) if side_value else 0})")
                logger.error(f"   TradeSide: '{trade_side_value}' (longitud: {len(str(trade_side_value)) if trade_side_value else 0})")
                logger.error(f"   OrderSource: '{order_source_value}' (longitud: {len(str(order_source_value)) if order_source_value else 0})")
                logger.error(f"   Status: '{order_data.get('status')}' (longitud: {len(str(order_data.get('status'))) if order_data.get('status') else 0})")
                logger.error(f"   OrderType: '{order_data.get('orderType')}' (longitud: {len(str(order_data.get('orderType'))) if order_data.get('orderType') else 0})")
                logger.error(f"   MarginMode: '{order_data.get('marginMode')}' (longitud: {len(str(order_data.get('marginMode'))) if order_data.get('marginMode') else 0})")
                logger.error(f"📄 Orden completa: {json.dumps(order_data, indent=2)}")
                
                # Re-lanzar el error para que se capture en el nivel superior
                raise e
        
        logger.info(f"✅ Procesamiento de órdenes completado: {orders_saved}/{len(orders)} guardadas exitosamente")
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
            
            # Parsear result_data si existe
            result_data = {}
            if execution_result.result_data:
                try:
                    result_data = json.loads(execution_result.result_data)
                except json.JSONDecodeError:
                    logger.warning(f"No se pudo parsear result_data para {execution_arn}")
            
            return {
                "found": True,
                "execution_arn": execution_result.execution_arn,
                "status": execution_result.status,
                "total_symbols": execution_result.total_symbols,
                "symbols_processed": result_data.get("symbols_processed", execution_result.total_symbols),
                "symbols_with_data": result_data.get("symbols_with_data"),
                "total_orders": execution_result.total_orders,
                "orders_in_db": orders_count,
                "s3_uri": execution_result.s3_uri,
                "public_url": execution_result.public_url,
                "processing_time_seconds": execution_result.processing_time_seconds,
                "aggregator_duration_seconds": result_data.get("aggregator_duration_seconds"),
                "error_count": result_data.get("error_count", 0),
                "cleanup_info": result_data.get("cleanup", {}),
                "timing_info": result_data.get("timing", {}),
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
