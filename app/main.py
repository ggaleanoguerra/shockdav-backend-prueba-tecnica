"""
Punto de entrada principal de la aplicación Bitget Orders API.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from app.core.config import config
from app.api.routes import orders, health, symbols

# Cargar variables de entorno
load_dotenv()

# Crear aplicación FastAPI
app = FastAPI(
    title=config.API_TITLE,
    version=config.API_VERSION,
    description=config.API_DESCRIPTION,
    docs_url="/docs",
    redoc_url="/redoc"
)

# Configurar CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8001",
        "http://localhost:8001",
        "http://127.0.0.1:3000",
        "http://localhost:3000"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Incluir routers
app.include_router(orders.router)
app.include_router(health.router)
app.include_router(symbols.router)

# Evento de inicio
@app.on_event("startup")
async def startup_event():
    """Evento que se ejecuta al iniciar la aplicación"""
    print(f"🚀 Iniciando {config.API_TITLE} v{config.API_VERSION}")
    
    # Validar configuración
    validation = config.validate_required_config()
    if not validation["valid"]:
        print("⚠️  Advertencias de configuración encontradas:")
        for error in validation["errors"]:
            print(f"   ❌ {error}")
        for warning in validation["warnings"]:
            print(f"   ⚠️  {warning}")
    else:
        print("✅ Configuración validada correctamente")

# Evento de cierre
@app.on_event("shutdown")
async def shutdown_event():
    """Evento que se ejecuta al cerrar la aplicación"""
    print("🛑 Cerrando aplicación")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level=config.LOG_LEVEL.lower()
    )
