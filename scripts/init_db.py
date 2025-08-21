"""
Script para inicializar la base de datos MySQL.
Ejecuta este script para crear las tablas necesarias.
"""

import sys
import os

# Agregar el directorio padre al path para poder importar los módulos
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models.database import create_tables, engine
from sqlalchemy import text
from app.core.config import config

def init_database():
    """Inicializa la base de datos creando las tablas necesarias"""
    
    print("🚀 Inicializando base de datos...")
    
    try:
        # Verificar conexión
        with engine.connect() as connection:
            result = connection.execute(text("SELECT 1"))
            print("✅ Conexión a MySQL establecida correctamente")
        
        # Crear tablas
        create_tables()
        print("✅ Tablas creadas exitosamente:")
        print("   - execution_results")
        print("   - orders") 
        print("   - processing_logs")
        
        # Mostrar información de configuración
        db_config = config.get_database_config()
        print(f"\n📊 Configuración de base de datos:")
        print(f"   Host: {db_config['host']}")
        print(f"   Puerto: {db_config['port']}")
        print(f"   Base de datos: {db_config['database']}")
        print(f"   Usuario: {db_config['user']}")
        
        print("\n🎉 ¡Base de datos inicializada correctamente!")
        print("\n📝 Próximos pasos:")
        print("   1. Asegúrate de que tu servidor MySQL esté ejecutándose")
        print("   2. Actualiza las credenciales en el archivo .env")
        print("   3. Ejecuta tu aplicación FastAPI con: uvicorn main:app --reload")
        
    except Exception as e:
        print(f"❌ Error inicializando base de datos: {str(e)}")
        print("\n🔧 Sugerencias:")
        print("   1. Verifica que MySQL esté ejecutándose")
        print("   2. Verifica las credenciales en el archivo .env")
        print("   3. Asegúrate de que la base de datos exista:")
        print(f"      CREATE DATABASE {config.MYSQL_DATABASE};")
        raise

if __name__ == "__main__":
    init_database()
