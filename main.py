"""
Punto de entrada principal para compatibilidad con la estructura anterior.
Redirige a app.main para mantener compatibilidad.
"""

from app.main import app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
