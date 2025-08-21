"""
Script de ejecución rápida para la aplicación.
Ejecuta la aplicación usando la nueva estructura organizacional.
También exporta el objeto 'app' para uso con uvicorn.
"""

import uvicorn
from app.main import app  # Importar el objeto app para exportarlo

# Exportar app para uso directo con uvicorn
__all__ = ["app"]

if __name__ == "__main__":
    print("🚀 Iniciando Bitget Orders API...")
    print("📂 Usando estructura organizacional en carpetas")
    print("📡 Servidor: http://localhost:8000")
    print("📚 Docs: http://localhost:8000/docs")
    print("\n")
    
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    )
