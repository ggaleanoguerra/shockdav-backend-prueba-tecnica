from sqlalchemy import Column, Integer, String, DateTime, Text, BigInteger, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
from datetime import datetime, timezone, timedelta
from app.core.config import config

# Zona horaria de Bogotá: UTC-5
BOGOTA_TIMEZONE = timezone(timedelta(hours=-5))

def get_bogota_now():
    """Obtiene la fecha y hora actual en zona horaria de Bogotá (UTC-5)"""
    utc_now = datetime.now(timezone.utc)
    return utc_now.astimezone(BOGOTA_TIMEZONE).replace(tzinfo=None)  # Sin tzinfo para compatibilidad con MySQL

Base = declarative_base()

class ExecutionResult(Base):
    """Tabla para almacenar los resultados de las ejecuciones"""
    __tablename__ = "execution_results"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    execution_arn = Column(String(255), unique=True, nullable=False, index=True)
    status = Column(String(50), nullable=False)
    total_symbols = Column(Integer)
    total_orders = Column(Integer)
    s3_uri = Column(Text)
    public_url = Column(Text)
    result_data = Column(Text)  # JSON string
    processing_time_seconds = Column(Float)  # Tiempo de procesamiento
    created_at = Column(DateTime, default=get_bogota_now)
    updated_at = Column(DateTime, default=get_bogota_now, onupdate=get_bogota_now)

class Order(Base):
    """Tabla para almacenar las órdenes de trading"""
    __tablename__ = "orders"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # Relación solo por execution_arn (sin FK)
    execution_arn = Column(String(255), nullable=False, index=True)
    
    # Campos básicos de la orden
    symbol = Column(String(20), nullable=False, index=True)
    order_id = Column(String(50), nullable=False, index=True)
    
    # Campos de volumen y precio
    size = Column(String(50))
    price = Column(String(50))
    price_avg = Column(String(50))
    base_volume = Column(String(50))
    quote_volume = Column(String(50))
    
    # Campos de estado y configuración
    status = Column(String(20))
    side = Column(String(10))  # buy/sell
    order_type = Column(String(20))  # market/limit
    force = Column(String(10))  # gtc, ioc, etc.
    
    # Campos específicos de futuros
    leverage = Column(String(10))
    margin_mode = Column(String(20))  # isolated/crossed
    margin_coin = Column(String(20))
    pos_side = Column(String(10))  # long/short
    pos_mode = Column(String(20))  # hedge_mode/one_way_mode
    trade_side = Column(String(20))  # open/close/sell_single/buy_single
    reduce_only = Column(String(10))  # YES/NO
    pos_avg = Column(String(50))
    
    # Campos de costos y ganancias
    fee = Column(String(50))
    total_profits = Column(String(50))
    
    # Campos de origen y configuración
    client_oid = Column(String(50))
    order_source = Column(String(20))
    enter_point_source = Column(String(20))
    preset_stop_surplus_price = Column(String(50))
    preset_stop_loss_price = Column(String(50))
    
    # Timestamps (en milliseconds)
    c_time = Column(BigInteger)  # Creation time
    u_time = Column(BigInteger)  # Update time
    
    # Timestamp de registro en nuestra BD
    created_at = Column(DateTime, default=get_bogota_now)

class ProcessingLog(Base):
    """Tabla para logs de procesamiento"""
    __tablename__ = "processing_logs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    execution_arn = Column(String(255), nullable=False, index=True)
    level = Column(String(20), nullable=False)  # INFO, ERROR, WARNING
    message = Column(Text, nullable=False)
    details = Column(Text)  # JSON string con detalles adicionales
    created_at = Column(DateTime, default=get_bogota_now)

# Configuración de la base de datos
DATABASE_URL = config.DATABASE_URL
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL no está configurada en el archivo .env")

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def create_tables():
    """Crear todas las tablas en la base de datos"""
    try:
        Base.metadata.create_all(bind=engine)
        print("✅ Tablas de base de datos creadas/verificadas correctamente")
        return True
    except Exception as e:
        print(f"⚠️  No se pudo conectar a la base de datos: {str(e)}")
        print("⚠️  La aplicación continuará funcionando en modo sin base de datos")
        return False

def get_db_session():
    """Obtener una sesión de base de datos"""
    try:
        session = SessionLocal()
        return session
    except Exception as e:
        print(f"⚠️  Error al crear sesión de base de datos: {str(e)}")
        return None