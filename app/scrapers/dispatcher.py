"""
dispatcher.py — Roteador de Scrapers

Decide qual scraper usar baseado na loja detectada.
É aqui que o price_checker.py vai chamar na Fase 2.

Hierarquia:
  1. Scraper específico (Amazon, ML, etc.)
  2. Scraper genérico (tenta extrair de qualquer página)
  3. Mock (se tudo falhar e USAR_MOCK=True)
"""

import logging
from typing import Optional

from app.scrapers.base import ScrapedProduct
from app.scrapers.amazon import scrape_amazon
from app.scrapers.mercadolivre import scrape_mercadolivre

logger = logging.getLogger(__name__)

# Mapeamento loja → função de scraping
SCRAPERS = {
    "amazon": scrape_amazon,
    "mercadolivre": scrape_mercadolivre,
    # Fase 2 (adicionar aqui conforme implementar):
    # "shopee": scrape_shopee,
    # "magalu": scrape_magalu,
    # "americanas": scrape_americanas,
}


async def scrape_product(url: str, store: Optional[str] = None) -> ScrapedProduct:
    """
    Escolhe e executa o scraper correto para a URL.

    Args:
        url: URL do produto
        store: nome da loja (detectado pelo affiliate.py)

    Returns:
        ScrapedProduct com resultado
    """
    if store and store in SCRAPERS:
        scraper_fn = SCRAPERS[store]
        logger.info(f"Usando scraper específico: {store}")
        result = await scraper_fn(url)
    else:
        logger.info(f"Loja '{store}' sem scraper específico — usando genérico")
        result = await _generic_scraper(url, store)

    return result


async def _generic_scraper(url: str, store: Optional[str] = None) -> ScrapedProduct:
    """
    Scraper genérico: tenta extrair preço de qualquer loja.
    Funciona para Americanas, Magalu, Shopee, etc.
    Menos confiável que scrapers específicos.
    """
    from app.scrapers.base import fetch_page, parse_price, get_random_headers
    from bs4 import BeautifulSoup
    import re

    logger.info(f"🔍 Scraper genérico: {url[:80]}...")
    html = await fetch_page(url, headers=get_random_headers())

    if not html:
        return ScrapedProduct(
            success=False,
            store=store,
            error="Página não carregada",
        )

    soup = BeautifulSoup(html, "lxml")

    # Seletores genéricos que funcionam em muitas lojas
    generic_price_selectors = [
        "[class*='price']",
        "[class*='valor']",
        "[class*='preco']",
        "[itemprop='price']",
        "[data-testid*='price']",
        ".price",
        ".preco",
        ".valor",
    ]

    price = None
    for selector in generic_price_selectors:
        elements = soup.select(selector)
        for el in elements:
            # Ignora elementos que são containers (têm filhos com preço)
            if len(el.find_all()) > 3:
                continue
            text = el.get_text(strip=True)
            if not text:
                continue
            p = parse_price(text)
            if p and p > 0:
                price = p
                logger.debug(f"Preço genérico '{selector}': R${p}")
                break
        if price:
            break

    # Fallback: regex no HTML inteiro
    if not price:
        patterns = [
            r'"price":\s*"?([\d.,]+)"?',
            r'"preco":\s*"?([\d.,]+)"?',
            r'"valor":\s*"?([\d.,]+)"?',
            r'R\$\s*([\d.,]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                price = parse_price(match.group(1))
                if price:
                    break

    # Título genérico
    title = None
    for sel in ["h1", "[class*='product-name']", "[class*='title']"]:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if t and len(t) > 5:
                title = " ".join(t.split())[:200]
                break

    if not price:
        return ScrapedProduct(
            success=False,
            store=store,
            name=title,
            error="Preço não encontrado (scraper genérico)",
        )

    logger.info(f"✅ Genérico: '{(title or 'N/A')[:40]}' → R${price}")

    return ScrapedProduct(
        success=True,
        store=store,
        price=price,
        name=title,
    )
