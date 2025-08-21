"""
Servicio para obtener símbolos SPOT ONLINE desde la API de Bitget (endpoints v2).
"""

from typing import Dict, Any, List
from fastapi import HTTPException
import requests
import logging

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class SymbolsService:
    """Servicio para obtener símbolos SPOT ONLINE desde la API de Bitget"""

    BITGET_SPOT_SYMBOLS_URL = "https://api.bitget.com/api/v2/spot/public/symbols"

    def get_bitget_symbols(self) -> Dict[str, Any]:
        """
        Obtener símbolos SPOT (v2) con status 'online'.

        Returns:
            Diccionario con lista de símbolos únicos 'online' de:
            - Símbolos spot públicos v2 (status=online)

        Raises:
            HTTPException: Si hay error al consultar la API
        """
        try:
            logger.info("=== Consultando SÍMBOLOS SPOT públicos v2 SOLO ONLINE ===")

            all_symbols: List[Dict[str, str]] = []
            seen_symbols = set()
            spot_symbols_count = 0
            spot_status = "success"

            try:
                response = requests.get(self.BITGET_SPOT_SYMBOLS_URL, timeout=30)
                response.raise_for_status()
                data = response.json()

                if data.get("code") != "00000":
                    msg = data.get("msg", "Error desconocido")
                    logger.warning(f"Error en API spot v2 de Bitget: {msg}")
                    spot_status = "error"
                else:
                    for spot_data in data.get("data", []):
                        symbol = spot_data.get("symbol")
                        status = spot_data.get("status", "unknown")

                        # Filtrar SOLO online
                        if symbol and status == "online" and symbol not in seen_symbols:
                            seen_symbols.add(symbol)
                            all_symbols.append({"symbol": symbol, "status": status})
                            spot_symbols_count += 1

                    logger.info(f"Obtenidos {spot_symbols_count} símbolos SPOT v2 ONLINE únicos")

            except requests.exceptions.RequestException as e:
                logger.error(f"Error conectando con API spot v2 de Bitget: {str(e)}")
                spot_status = "connection_error"
            except Exception as e:
                logger.error(f"Error procesando símbolos spot v2: {str(e)}")
                spot_status = "processing_error"

            total_symbols = len(all_symbols)
            logger.info("=== CONSOLIDACIÓN FINAL SPOT ONLINE ===")
            logger.info(f"Total símbolos SPOT v2 únicos ONLINE: {total_symbols}")

            return {
                "symbols": all_symbols,  # [{"symbol": "...", "status": "online"}, ...]
                "total": total_symbols,
                "source": "bitget_v2_api_only",
                "spot_summary": {
                    "count": spot_symbols_count,
                    "status": spot_status,
                    "description": "Spot trading pairs (public v2) - ONLY ONLINE",
                },
                "api_response_code": "00000" if spot_status == "success" else "error",
                "api_message": "success - only spot v2 online symbols" if spot_status == "success" else "error retrieving spot symbols",
            }

        except requests.exceptions.RequestException as e:
            logger.error(f"Error conectando con API spot v2 de Bitget: {str(e)}")
            raise HTTPException(
                status_code=502,
                detail=f"Error conectando con API spot v2 de Bitget: {str(e)}",
            )
        except Exception as e:
            logger.error(f"Error procesando símbolos spot v2 de Bitget: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"Error procesando símbolos spot v2: {str(e)}",
            )
