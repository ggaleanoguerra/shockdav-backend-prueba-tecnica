"""
Servicio para manejar la lógica de negocio relacionada con las órdenes de Bitget.
Separa la lógica de los endpoints para mantener el código organizado.
"""

import boto3
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, Any
from fastapi import HTTPException
from pydantic import BaseModel, Field
from app.services.database_service import DatabaseService
from app.services.symbols_service import SymbolsService
from app.core.config import config
import logging

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Zona horaria de Bogotá: UTC-5
BOGOTA_TIMEZONE = timezone(timedelta(hours=-5))

def _get_bogota_time(utc_time: datetime) -> datetime:
    """Convierte tiempo UTC a hora de Bogotá (UTC-5)"""
    if utc_time.tzinfo is None:
        utc_time = utc_time.replace(tzinfo=timezone.utc)
    return utc_time.astimezone(BOGOTA_TIMEZONE)

class OrderRequest(BaseModel):
    symbols: list[str] | None = Field(None, description="Lista de símbolos específicos. Si no se proporciona, se usarán todos los símbolos activos de Bitget")
    start_ms: int | None = None
    end_ms: int | None = None

class OrdersService:
    """Servicio para manejar operaciones relacionadas con órdenes"""
    
    def __init__(self):
        # Validar configuración requerida
        self.lambda_name = config.COORD_LAMBDA_NAME
        if not self.lambda_name:
            raise RuntimeError("Config faltante: define COORD_LAMBDA_NAME en el entorno.")
        
        # Inicializar clientes AWS con timeout
        aws_config = config.get_aws_config()
        self.lambda_client = boto3.client(
            "lambda", 
            region_name=aws_config["region"],
            config=boto3.session.Config(
                read_timeout=10,
                connect_timeout=5,
                retries={'max_attempts': 3}
            )
        )
        self.sf_client = boto3.client(
            "stepfunctions", 
            region_name=aws_config["region"],
            config=boto3.session.Config(
                read_timeout=10,
                connect_timeout=5,
                retries={'max_attempts': 3}
            )
        )
        
        # Inicializar servicio de base de datos y símbolos
        self.db_service = DatabaseService()
        self.symbols_service = SymbolsService()
        
        logger.info(f"OrdersService inicializado con Lambda: {self.lambda_name}")
    
    def _get_symbols_source(self, order_request: OrderRequest) -> str:
        """
        Determina el origen de los símbolos para incluir en la respuesta.
        
        Args:
            order_request: Solicitud original
            
        Returns:
            String indicando el origen: "bitget_api" o "user_provided"
        """
        if not order_request.symbols or (order_request.symbols and "ALL" in order_request.symbols):
            return "bitget_api"
        return "user_provided"
    
    def _get_symbols_for_execution(self, order_request: OrderRequest) -> list[str]:
        """
        Obtiene la lista de símbolos para la ejecución.
        Si no se proporcionan símbolos o se envía ["ALL"], obtiene todos los activos desde Bitget.
        
        Args:
            order_request: Solicitud con símbolos opcionales
            
        Returns:
            Lista de símbolos para procesar
            
        Raises:
            HTTPException: Si no se pueden obtener símbolos
        """
        # Si no hay símbolos o contiene "ALL", obtener todos desde Bitget
        if not order_request.symbols or (order_request.symbols and "ALL" in order_request.symbols):
            try:
                logger.info("Obteniendo todos los símbolos activos desde Bitget...")
                symbols_response = self.symbols_service.get_bitget_symbols()
                
                symbols_list = [symbol["symbol"] for symbol in symbols_response["symbols"]]
                
                if not symbols_list:
                    raise HTTPException(
                        status_code=500,
                        detail="No se encontraron símbolos activos en la API de Bitget"
                    )
                
                logger.info(f"Obtenidos {len(symbols_list)} símbolos activos desde Bitget")
                return symbols_list
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error obteniendo símbolos desde Bitget: {str(e)}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Error obteniendo símbolos desde Bitget: {str(e)}"
                )
        
        # Si se proporcionaron símbolos específicos (que no sean "ALL")
        logger.info(f"Usando símbolos específicos proporcionados: {len(order_request.symbols)} símbolos")
        return order_request.symbols
    
    def start_order_execution(self, order_request: OrderRequest) -> Dict[str, Any]:
        """
        Inicia la ejecución de órdenes invocando la Lambda coordinadora.
        
        Args:
            order_request: Solicitud con símbolos opcionales y parámetros
            
        Returns:
            Diccionario con el estado y executionArn
            
        Raises:
            HTTPException: Si hay error en la invocación
        """
        try:
            # Obtener símbolos (específicos o todos los activos)
            symbols = self._get_symbols_for_execution(order_request)
            
            # Crear payload con símbolos resueltos
            payload = order_request.model_dump()
            payload['symbols'] = symbols
            
            logger.info(f"Iniciando ejecución para {len(symbols)} símbolos")
            
            # Intentar invocar Lambda coordinadora con timeout
            try:
                resp = self.lambda_client.invoke(
                    FunctionName=self.lambda_name,
                    InvocationType="RequestResponse",
                    Payload=json.dumps(payload)
                )
            except Exception as aws_error:
                logger.error(f"Error conectando con AWS Lambda: {str(aws_error)}")
                # En caso de error con AWS, devolver una respuesta de fallback
                return {
                    "status": "error",
                    "message": f"Error conectando con AWS Lambda: {str(aws_error)}",
                    "symbols": symbols,
                    "total_symbols": len(symbols),
                    "symbols_source": self._get_symbols_source(order_request),
                    "fallback": True
                }
            
            data = json.loads(resp["Payload"].read().decode())
            
            # Procesar respuesta de la coordinadora
            if isinstance(data, dict) and "statusCode" in data:
                body = json.loads(data.get("body") or "{}")
                if "executionArn" in body:
                    execution_arn = body["executionArn"]
                    
                    logger.info(f"Ejecución iniciada exitosamente: {execution_arn}")
                    
                    # Guardar automáticamente el executionArn en la base de datos
                    database_saved = False
                    try:
                        utc_now = datetime.now(timezone.utc)
                        bogota_now = _get_bogota_time(utc_now)
                        initial_data = {
                            "executionArn": execution_arn,
                            "status": "RUNNING",
                            "result": {
                                "symbols": symbols,
                                "total_symbols": len(symbols),
                                "symbols_source": self._get_symbols_source(order_request),
                                "start_ms": payload.get('start_ms'),
                                "end_ms": payload.get('end_ms'),
                                "initiated_at": utc_now.isoformat(),
                                "initiated_at_bogota": bogota_now.isoformat()
                            }
                        }
                        save_result = self.db_service.save_execution_data(initial_data)
                        if save_result.get("success", False):
                            database_saved = True
                            logger.info(f"ExecutionArn guardado automáticamente en BD: {execution_arn}")
                        else:
                            logger.warning(f"Error guardando executionArn inicial: {save_result.get('error', 'Error desconocido')}")
                    except Exception as db_error:
                        logger.warning(f"Error guardando executionArn inicial: {str(db_error)}")
                        # No fallar la respuesta por error de BD, solo logear
                    
                    response = {
                        "status": "started", 
                        "executionArn": execution_arn,
                        "symbols": symbols,
                        "total_symbols": len(symbols),
                        "symbols_source": self._get_symbols_source(order_request),
                        "message": f"Procesamiento iniciado para {len(symbols)} símbolos"
                    }
                    
                    if database_saved:
                        response["database_saved"] = True
                        response["database_message"] = "ExecutionArn guardado automáticamente en base de datos"
                    else:
                        response["database_saved"] = False
                        response["database_message"] = "Error guardando en base de datos - revisar configuración MySQL"
                    
                    return response
                
                raise RuntimeError(f"Respuesta inesperada de coordinadora: {data}")
            
            # Si devolvió otra cosa, la reenviamos (útil para errores 4xx de validación)
            return data
            
        except Exception as e:
            logger.error(f"Error iniciando ejecución: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))
    
    def start_order_execution_local(self, order_request: OrderRequest) -> Dict[str, Any]:
        """
        Versión local que simula el procesamiento sin AWS para testing.
        
        Args:
            order_request: Solicitud con símbolos opcionales y parámetros
            
        Returns:
            Diccionario simulando una respuesta exitosa
        """
        try:
            # Obtener símbolos (específicos o todos los activos)
            symbols = self._get_symbols_for_execution(order_request)
            
            # Crear payload con símbolos resueltos
            payload = order_request.model_dump()
            payload['symbols'] = symbols
            
            logger.info(f"Simulando ejecución local para {len(symbols)} símbolos")
            
            # Generar un execution_arn simulado
            import uuid
            execution_arn = f"arn:aws:states:us-east-2:123456789:execution:test-{uuid.uuid4()}"
            
            logger.info(f"Ejecución local simulada: {execution_arn}")
            
            # Guardar automáticamente el executionArn en la base de datos (versión local)
            database_saved = False
            try:
                utc_now = datetime.now(timezone.utc)
                bogota_now = _get_bogota_time(utc_now)
                initial_data = {
                    "executionArn": execution_arn,
                    "status": "RUNNING",
                    "result": {
                        "symbols": symbols,
                        "total_symbols": len(symbols),
                        "symbols_source": self._get_symbols_source(order_request),
                        "start_ms": payload.get('start_ms'),
                        "end_ms": payload.get('end_ms'),
                        "initiated_at": utc_now.isoformat(),
                        "initiated_at_bogota": bogota_now.isoformat(),
                        "local_execution": True
                    }
                }
                save_result = self.db_service.save_execution_data(initial_data)
                if save_result.get("success", False):
                    database_saved = True
                    logger.info(f"ExecutionArn local guardado automáticamente en BD: {execution_arn}")
                else:
                    logger.warning(f"Error guardando executionArn local inicial: {save_result.get('error', 'Error desconocido')}")
            except Exception as db_error:
                logger.warning(f"Error guardando executionArn local inicial: {str(db_error)}")
                # No fallar la respuesta por error de BD, solo logear
            
            response = {
                "status": "started_local", 
                "executionArn": execution_arn,
                "symbols": symbols,
                "total_symbols": len(symbols),
                "symbols_source": self._get_symbols_source(order_request),
                "message": f"Procesamiento local simulado para {len(symbols)} símbolos",
                "local": True
            }
            
            if database_saved:
                response["database_saved"] = True
                response["database_message"] = "ExecutionArn guardado automáticamente en base de datos"
            else:
                response["database_saved"] = False
                response["database_message"] = "Error guardando en base de datos - revisar configuración MySQL"
            
            return response
            
        except Exception as e:
            logger.error(f"Error en ejecución local: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))
    
    def get_execution_status(self, execution_arn: str) -> Dict[str, Any]:
        """
        Consulta el estado de una ejecución de Step Functions.
        
        Args:
            execution_arn: ARN de la ejecución a consultar
            
        Returns:
            Diccionario con el estado, resultado y información de base de datos
            
        Raises:
            HTTPException: Si hay error consultando el estado
        """
        try:
            logger.info(f"Consultando estado de ejecución: {execution_arn}")
            
            # Describir ejecución en Step Functions
            desc = self.sf_client.describe_execution(executionArn=execution_arn)
            status = desc["status"]  # RUNNING | SUCCEEDED | FAILED | TIMED_OUT | ABORTED
            result = None
            
            # Si la ejecución está completa, procesar resultado
            if status == "SUCCEEDED":
                result = json.loads(desc.get("output") or "{}")
                
                # Agregar información de tiempo de AWS al resultado
                if desc.get("startDate") and desc.get("stopDate"):
                    start_date = desc.get("startDate")
                    stop_date = desc.get("stopDate")
                    aws_duration_seconds = (stop_date - start_date).total_seconds()
                    
                    # Formatear duración en minutos y segundos
                    minutes = int(aws_duration_seconds // 60)
                    seconds = aws_duration_seconds % 60
                    duration_formatted = f"{minutes}m {seconds:.3f}s"
                    
                    # Agregar timing info al resultado principal
                    if "timing" not in result:
                        result["timing"] = {}
                    
                    result["timing"].update({
                        "aws_start_date": start_date.isoformat(),
                        "aws_stop_date": stop_date.isoformat(),
                        "aws_total_duration_seconds": round(aws_duration_seconds, 3),
                        "aws_total_duration_formatted": duration_formatted
                    })
                    
                    result["aws_total_duration_seconds"] = round(aws_duration_seconds, 3)
                    result["aws_total_duration_formatted"] = duration_formatted
                
                # Guardar datos en base de datos automáticamente
                database_save_result = self._save_execution_to_database(
                    execution_arn, status, result
                )
                
                # Agregar información del guardado a la respuesta
                result["database_save"] = database_save_result
                
                # Agregar información adicional de la base de datos
                db_data = self.db_service.get_execution_data(execution_arn)
                if db_data["found"]:
                    result["database_info"] = {
                        "orders_in_db": db_data["orders_in_db"],
                        "processing_time_seconds": db_data["processing_time_seconds"],
                        "created_at": db_data["created_at"],
                        "updated_at": db_data["updated_at"]
                    }
            
            elif status in ["FAILED", "TIMED_OUT", "ABORTED"]:
                # También guardar ejecuciones fallidas para tracking
                result = {"error": desc.get("error", "Execution failed")}
                self._save_execution_to_database(execution_arn, status, result)
            
            # Calcular duración de la ejecución si está disponible
            aws_execution_details = {
                "startDate": desc.get("startDate").isoformat() if desc.get("startDate") else None,
                "startDate_bogota": _get_bogota_time(desc.get("startDate")).isoformat() if desc.get("startDate") else None,
                "stopDate": desc.get("stopDate").isoformat() if desc.get("stopDate") else None,
                "stopDate_bogota": _get_bogota_time(desc.get("stopDate")).isoformat() if desc.get("stopDate") else None,
                "stateMachineArn": desc.get("stateMachineArn")
            }
            
            # Calcular duración total si hay fechas de inicio y fin
            if desc.get("startDate") and desc.get("stopDate"):
                start_date = desc.get("startDate")
                stop_date = desc.get("stopDate")
                duration_seconds = (stop_date - start_date).total_seconds()
                aws_execution_details["duration_seconds"] = round(duration_seconds, 3)
                
                # Formatear duración en minutos y segundos
                minutes = int(duration_seconds // 60)
                seconds = duration_seconds % 60
                aws_execution_details["duration_formatted"] = f"{minutes}m {seconds:.3f}s"
                
                logger.info(f"Ejecución completada en {duration_seconds:.3f} segundos ({minutes}m {seconds:.3f}s)")
            
            response = {
                "status": status, 
                "result": result, 
                "executionArn": execution_arn,
                "aws_execution_details": aws_execution_details
            }
            
            logger.info(f"Estado consultado: {status} para {execution_arn}")
            return response
            
        except Exception as e:
            logger.error(f"Error consultando estado de {execution_arn}: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))
    
    def _save_execution_to_database(self, execution_arn: str, status: str, result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Guarda los datos de ejecución en la base de datos.
        
        Args:
            execution_arn: ARN de la ejecución
            status: Estado de la ejecución
            result: Resultado de la ejecución
            
        Returns:
            Resultado del guardado en base de datos
        """
        try:
            execution_data = {
                "status": status,
                "result": result,
                "executionArn": execution_arn
            }
            
            save_result = self.db_service.save_execution_data(execution_data)
            
            if save_result["success"]:
                logger.info(f"Datos guardados en BD para {execution_arn}: "
                           f"{save_result.get('orders_saved', 0)} órdenes en "
                           f"{save_result.get('processing_time_seconds', 0):.2f}s")
            else:
                logger.warning(f"Error guardando en BD para {execution_arn}: "
                              f"{save_result.get('error', 'Error desconocido')}")
            
            return save_result
            
        except Exception as e:
            logger.error(f"Error en _save_execution_to_database para {execution_arn}: {str(e)}")
            return {
                "success": False, 
                "error": str(e),
                "execution_arn": execution_arn
            }
    
    def save_data_from_public_url(self, execution_arn: str, public_url: str) -> Dict[str, Any]:
        """
        Descarga y guarda datos desde una URL pública.
        
        Args:
            execution_arn: ARN de la ejecución
            public_url: URL pública del JSON
            
        Returns:
            Resultado del procesamiento
            
        Raises:
            HTTPException: Si hay error en el procesamiento
        """
        try:
            logger.info(f"Procesando URL pública para {execution_arn}: {public_url}")
            
            result = self.db_service.fetch_and_save_from_public_url(execution_arn, public_url)
            
            if result["success"]:
                logger.info(f"Datos de URL procesados exitosamente para {execution_arn}: "
                           f"{result.get('orders_saved', 0)} órdenes")
                
                return {
                    "message": "Datos guardados exitosamente desde URL pública",
                    "execution_arn": execution_arn,
                    "public_url": public_url,
                    "orders_saved": result.get("orders_saved", 0),
                    "processing_time_seconds": result.get("processing_time_seconds", 0),
                    "download_time_seconds": result.get("download_time_seconds", 0),
                    "total_processing_time_seconds": result.get("total_processing_time_seconds", 0)
                }
            else:
                error_msg = result.get("error", "Error desconocido procesando URL")
                logger.error(f"Error procesando URL para {execution_arn}: {error_msg}")
                raise HTTPException(status_code=500, detail=error_msg)
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error inesperado procesando URL para {execution_arn}: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))
    
    def get_database_data(self, execution_arn: str, skip: int = 0, limit: int = 10) -> Dict[str, Any]:
        """
        Obtiene datos de la base de datos para una ejecución con órdenes paginadas.
        
        Args:
            execution_arn: ARN de la ejecución
            skip: Número de registros a saltar (default: 0)
            limit: Número máximo de registros a retornar (default: 10)
            
        Returns:
            Datos de la base de datos con órdenes paginadas
            
        Raises:
            HTTPException: Si no se encuentran datos
        """
        try:
            logger.info(f"Consultando datos paginados de BD para {execution_arn} (skip={skip}, limit={limit})")
            
            result = self.db_service.get_execution_orders_paginated(execution_arn, skip, limit)
            
            if result["found"]:
                logger.info(f"Datos encontrados en BD para {execution_arn}: "
                           f"{result['pagination']['current_count']} órdenes de {result['pagination']['total_orders']} total")
                return result
            else:
                logger.warning(f"No se encontraron datos en BD para {execution_arn}")
                raise HTTPException(
                    status_code=404, 
                    detail=f"Datos no encontrados en la base de datos para {execution_arn}"
                )
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error consultando BD para {execution_arn}: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))
    
    def save_execution_data_manual(self, execution_arn: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Guarda manualmente datos de ejecución en la base de datos.
        
        Args:
            execution_arn: ARN de la ejecución
            data: Datos a guardar
            
        Returns:
            Resultado del guardado
            
        Raises:
            HTTPException: Si hay error en el guardado
        """
        try:
            logger.info(f"Guardado manual iniciado para {execution_arn}")
            
            execution_data = {
                "status": data.get("status", "SUCCEEDED"),
                "result": data.get("result", {}),
                "executionArn": execution_arn
            }
            
            result = self.db_service.save_execution_data(execution_data)
            
            if result["success"]:
                logger.info(f"Guardado manual exitoso para {execution_arn}: "
                           f"{result.get('orders_saved', 0)} órdenes")
                
                return {
                    "message": "Datos guardados exitosamente (guardado manual)",
                    "execution_arn": execution_arn,
                    "orders_saved": result.get("orders_saved", 0),
                    "processing_time_seconds": result.get("processing_time_seconds", 0),
                    "total_symbols": result.get("total_symbols"),
                    "total_orders": result.get("total_orders")
                }
            else:
                error_msg = result.get("error", "Error desconocido en guardado manual")
                logger.error(f"Error en guardado manual para {execution_arn}: {error_msg}")
                raise HTTPException(status_code=500, detail=error_msg)
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error inesperado en guardado manual para {execution_arn}: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    def list_all_executions(self) -> Dict[str, Any]:
        """
        Lista todas las ejecuciones guardadas en la base de datos.
        
        Returns:
            Diccionario con todas las ejecuciones disponibles
        """
        try:
            logger.info("Consultando todas las ejecuciones de la base de datos")
            
            executions = self.db_service.list_all_executions()
            
            logger.info(f"Se encontraron {len(executions)} ejecuciones en la base de datos")
            
            return {
                "message": f"Se encontraron {len(executions)} ejecuciones",
                "total_executions": len(executions),
                "executions": executions
            }
            
        except Exception as e:
            logger.error(f"Error listando ejecuciones: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))
