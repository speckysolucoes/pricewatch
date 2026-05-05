"""
price_checker.py — Verificação de Preços (Fase 2)

Agora usa scrapers reais por padrão.
Mock disponível via USE_MOCK=true no .env (para testes).
"""

import random
import asyncio
import os
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

USE_MOCK = os.getenv("USE_MOCK", "false").lower() == "true"


@dataclass
class PriceResult:
    success: bool
    price: Optional[float] = None
    product_name: Optional[str] = None
    image_url: Optional[str] = None
    original_price: Optional[float] = None
    availability: bool = True
    error: Optional[str] = None
    checked_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def discount_percentage(self) -> Optional[float]:
        if self.original_price and self.price and self.original_price > self.price:
            return round((1 - self.price / self.original_price) * 100, 1)
        return None


async def _real_check_price(url: str, store: Optional[str]) -> PriceResult:
    from app.scrapers.dispatcher import scrape_product
    result = await scrape_product(url=url, store=store)
    return PriceResult(
        success=result.success,
        price=result.price,
        product_name=result.name,
        image_url=result.image_url,
        original_price=result.original_price,
        availability=result.availability,
        error=result.error,
    )


MOCK_PRODUCTS = {
    "amazon":       {"default_price": 299.90, "variation": 0.15},
    "mercadolivre": {"default_price": 189.99, "variation": 0.20},
    "default":      {"default_price": 150.00, "variation": 0.10},
}


async def _mock_check_price(url: str, store: Optional[str]) -> PriceResult:
    await asyncio.sleep(0.3)
    if random.random() < 0.05:
        return PriceResult(success=False, error="Produto indisponível (mock)")
    config = MOCK_PRODUCTS.get(store or "default", MOCK_PRODUCTS["default"])
    base = config["default_price"]
    var = config["variation"]
    price = round(random.uniform(base * (1 - var), base * (1 + var)), 2)
    original = round(price * random.uniform(1.05, 1.30), 2) if random.random() > 0.5 else None
    return PriceResult(
        success=True,
        price=price,
        original_price=original,
        product_name=f"Produto Mock ({store or 'desconhecido'})",
    )


async def check_price(url: str, store: Optional[str] = None) -> PriceResult:
    """
    Verifica o preço atual de um produto.
    Fase 2: scraping real por padrão. USE_MOCK=true para testes.
    """
    try:
        if USE_MOCK:
            return await _mock_check_price(url, store)
        else:
            return await _real_check_price(url, store)
    except Exception as e:
        logger.error(f"Erro ao verificar preço de {url}: {e}", exc_info=True)
        return PriceResult(success=False, error=str(e))
