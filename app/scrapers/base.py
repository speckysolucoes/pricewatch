"""
base.py — Utilitários compartilhados entre todos os scrapers

Contém:
  - Pool de User-Agents rotativos (evita bloqueio)
  - Cliente HTTP assíncrono com configurações seguras
  - Parser de preço robusto (lida com R$, pontos, vírgulas)
  - Retry automático com backoff exponencial
"""

import re
import random
import asyncio
import logging
from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# USER-AGENTS ROTATIVOS
# Simula diferentes navegadores para evitar bloqueios simples
# ─────────────────────────────────────────────────────────────

USER_AGENTS = [
    # Chrome Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Firefox Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Firefox Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Safari Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

# Headers comuns que um browser real enviaria
BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Cache-Control": "max-age=0",
}


def get_random_headers(extra: dict = None) -> dict:
    """Retorna headers com User-Agent aleatório."""
    headers = {**BASE_HEADERS, "User-Agent": random.choice(USER_AGENTS)}
    if extra:
        headers.update(extra)
    return headers


# ─────────────────────────────────────────────────────────────
# RESULTADO PADRONIZADO
# ─────────────────────────────────────────────────────────────

@dataclass
class ScrapedProduct:
    """
    Resultado de um scraping bem-sucedido.
    Todos os scrapers retornam esse formato.
    """
    success: bool
    price: Optional[float] = None
    name: Optional[str] = None
    image_url: Optional[str] = None
    original_price: Optional[float] = None  # preço "de" (antes do desconto)
    availability: bool = True               # produto disponível para compra?
    error: Optional[str] = None
    store: Optional[str] = None
    scraped_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def discount_percentage(self) -> Optional[float]:
        """Desconto em % baseado nos preços original vs atual."""
        if self.original_price and self.price and self.original_price > self.price:
            return round((1 - self.price / self.original_price) * 100, 1)
        return None


# ─────────────────────────────────────────────────────────────
# PARSER DE PREÇO
# Lida com formatos brasileiros: R$1.299,90 / 1299,90 / 1.299
# ─────────────────────────────────────────────────────────────

def parse_price(text: str) -> Optional[float]:
    """
    Converte string de preço para float.

    Suporta formatos:
      "R$ 1.299,90"  → 1299.90
      "1.299,90"     → 1299.90
      "299,00"       → 299.00
      "R$299"        → 299.00
      "1299.90"      → 1299.90  (formato americano também)
    """
    if not text:
        return None

    # Remove espaços e símbolo de moeda
    cleaned = re.sub(r"[R$\s\xa0]", "", text.strip())

    if not cleaned:
        return None

    # Formato BR: 1.299,90 (ponto = milhar, vírgula = decimal)
    # Detecta se tem vírgula como separador decimal
    if "," in cleaned:
        # Remove pontos de milhar e troca vírgula por ponto
        cleaned = cleaned.replace(".", "").replace(",", ".")

    # Remove qualquer caractere que não seja dígito ou ponto
    cleaned = re.sub(r"[^\d.]", "", cleaned)

    try:
        price = float(cleaned)
        # Sanidade: preços válidos entre R$0,01 e R$1.000.000
        if 0.01 <= price <= 1_000_000:
            return round(price, 2)
        return None
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────
# CLIENTE HTTP COM RETRY
# ─────────────────────────────────────────────────────────────

async def fetch_page(
    url: str,
    headers: dict = None,
    max_retries: int = 3,
    timeout: int = 15,
) -> Optional[str]:
    """
    Faz GET na URL com retry automático e backoff exponencial.

    Args:
        url: URL para buscar
        headers: headers HTTP (usa aleatórios se None)
        max_retries: número máximo de tentativas
        timeout: timeout em segundos

    Returns:
        HTML da página ou None se falhar todas as tentativas

    Estratégia de retry:
        Tentativa 1: imediata
        Tentativa 2: espera 2s
        Tentativa 3: espera 4s
        (backoff exponencial)
    """
    if headers is None:
        headers = get_random_headers()

    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers=headers,
            ) as client:
                response = await client.get(url)

                # 429 = Too Many Requests → espera mais
                if response.status_code == 429:
                    wait = 10 * attempt
                    logger.warning(f"Rate limited (429). Aguardando {wait}s...")
                    await asyncio.sleep(wait)
                    continue

                # 503 = serviço indisponível → retry
                if response.status_code == 503:
                    wait = 5 * attempt
                    logger.warning(f"Serviço indisponível (503). Aguardando {wait}s...")
                    await asyncio.sleep(wait)
                    continue

                response.raise_for_status()
                return response.text

        except httpx.TimeoutException:
            last_error = f"Timeout após {timeout}s"
            logger.warning(f"Tentativa {attempt}/{max_retries}: {last_error}")

        except httpx.HTTPStatusError as e:
            last_error = f"HTTP {e.response.status_code}"
            logger.warning(f"Tentativa {attempt}/{max_retries}: {last_error}")

        except Exception as e:
            last_error = str(e)
            logger.warning(f"Tentativa {attempt}/{max_retries}: {last_error}")

        # Backoff exponencial: 2s, 4s, 8s...
        if attempt < max_retries:
            wait = 2 ** attempt
            logger.info(f"Aguardando {wait}s antes de tentar novamente...")
            await asyncio.sleep(wait)

    logger.error(f"Todas as {max_retries} tentativas falharam para {url}: {last_error}")
    return None
