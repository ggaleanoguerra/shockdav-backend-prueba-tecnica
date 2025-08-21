"""
Endpoints de salud e información de la aplicación.
"""

from fastapi import APIRouter
from app.core.config import config

# Crear router para endpoints de salud
router = APIRouter(tags=["health"])

@router.get("/health")
def health_check():
    """Endpoint de salud de la aplicación"""
    return {
        "status": "healthy",
        "service": config.API_TITLE,
        "version": config.API_VERSION
    }

@router.get("/")
def root():
    """Endpoint raíz con información básica"""
    return {
        "message": config.API_TITLE,
        "version": config.API_VERSION,
        "description": config.API_DESCRIPTION,
        "docs": "/docs",
        "redoc": "/redoc",
        "health": "/health"
    }

@router.get("/info")
def app_info():
    """Información detallada de la aplicación"""
    
    # Obtener resumen de configuración (sin datos sensibles)
    validation = config.validate_required_config()
    
    return {
        "application": {
            "name": config.API_TITLE,
            "version": config.API_VERSION,
            "description": config.API_DESCRIPTION
        },
        "configuration": {
            "valid": validation["valid"],
            "aws_region": config.AWS_DEFAULT_REGION,
            "mysql_host": config.MYSQL_HOST,
            "mysql_port": config.MYSQL_PORT,
            "mysql_database": config.MYSQL_DATABASE,
            "log_level": config.LOG_LEVEL
        },
        "endpoints": {
            "orders": "/orders",
            "health": "/health",
            "docs": "/docs",
            "redoc": "/redoc"
        }
    }
