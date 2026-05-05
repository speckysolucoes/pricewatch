"""
mercadolivre.py — Scraper do Mercado Livre Brasil

O ML tem duas fontes de dados:
  1. HTML da página (scraping tradicional)
  2. JSON embutido na página (__NEXT_DATA__ / state JSON)
     → Mais confiável que seletores CSS (menos sujeito a mudanças)

Estratégia:
  Tenta extrair do JSON embutido primeiro,
  cai para scraping CSS se não encontrar.
"""

import json
import logging
import re
from typing import Optional, Dict, Any

from bs4 import BeautifulSoup

from app.scrapers.base import ScrapedProduct, fetch_page, get_random_headers, parse_price

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# SELETORES CSS — fallback
# ─────────────────────────────────────────────────────────────

PRICE_SELECTORS = [
    ".andes-money-amount__fraction",         # preço principal (parte inteira)
    ".ui-pdp-price__second-line .andes-money-amount__fraction",
    ".price-tag-fraction",
    "[class*='price__fraction']",
    ".andes-money-amount.ui-pdp-price__part .andes-money-amount__fraction",
]

PRICE_CENTS_SELECTORS = [
    ".andes-money-amount__cents",            # centavos (ex: ",99")
    ".price-tag-cents",
]

ORIGINAL_PRICE_SELECTORS = [
    ".ui-pdp-price__original-value .andes-money-amount__fraction",
    ".price-tag-amount-app[class*='line-through'] .price-tag-fraction",
]

TITLE_SELECTORS = [
    ".ui-pdp-title",
    "h1.ui-pdp-title",
    "[class*='item-title']",
    ".item-title",
]

IMAGE_SELECTORS = [
    ".ui-pdp-gallery__figure img",
    ".ui-pdp-image.ui-pdp-gallery__figure--image",
    ".gallery-image",
]


# ─────────────────────────────────────────────────────────────
# EXTRAÇÃO DO JSON EMBUTIDO
# ─────────────────────────────────────────────────────────────

def _extract_json_data(html: str) -> Optional[Dict[str, Any]]:
    """
    Tenta extrair dados do JSON embutido na página do ML.

    O ML injeta dados do produto como JSON no HTML.
    Mais estável que seletores CSS.
    """
    patterns = [
        # Next.js data
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        # State JSON do ML
        r'window\.__PRELOADED_STATE__\s*=\s*({.*?});',
        # Dados do item
        r'"item"\s*:\s*({[^{}]*"price"[^{}]*})',
    ]

    for pattern in patterns:
        match = re.search(pattern, html, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                return data
            except json.JSONDecodeError:
                continue

    return None


def _find_price_in_json(data: dict, path: list = None, depth: int = 0) -> Optional[float]:
    """
    Busca recursivamente por campos de preço no JSON.
    Procura chaves como: price, sale_price, standard_price, amount
    """
    if depth > 10 or not isinstance(data, dict):
        return None

    price_keys = {"price", "sale_price", "standard_price", "amount", "value"}

    for key, value in data.items():
        if key.lower() in price_keys and isinstance(value, (int, float)):
            if 0.01 <= float(value) <= 1_000_000:
                return float(value)

        if isinstance(value, dict):
            result = _find_price_in_json(value, depth=depth + 1)
            if result:
                return result

        if isinstance(value, list):
            for item in value[:5]:  # limita iteração
                if isinstance(item, dict):
                    result = _find_price_in_json(item, depth=depth + 1)
                    if result:
                        return result

    return None


# ─────────────────────────────────────────────────────────────
# EXTRAÇÃO CSS — fallback
# ─────────────────────────────────────────────────────────────

def _extract_price_css(soup: BeautifulSoup) -> Optional[float]:
    """Extrai preço via seletores CSS."""
    for selector in PRICE_SELECTORS:
        el = soup.select_one(selector)
        if not el:
            continue

        fraction = el.get_text(strip=True)

        # Tenta pegar centavos separados
        cents_el = None
        for cents_selector in PRICE_CENTS_SELECTORS:
            cents_el = soup.select_one(cents_selector)
            if cents_el:
                break

        if cents_el:
            cents = cents_el.get_text(strip=True)
            price_str = f"{fraction},{cents}"
        else:
            price_str = fraction

        price = parse_price(price_str)
        if price:
            logger.debug(f"Preço ML via CSS '{selector}': R${price}")
            return price

    return None


def _extract_original_price(soup: BeautifulSoup) -> Optional[float]:
    for selector in ORIGINAL_PRICE_SELECTORS:
        el = soup.select_one(selector)
        if el:
            price = parse_price(el.get_text(strip=True))
            if price:
                return price
    return None


def _extract_title(soup: BeautifulSoup) -> Optional[str]:
    for selector in TITLE_SELECTORS:
        el = soup.select_one(selector)
        if el:
            title = el.get_text(strip=True)
            if title and len(title) > 3:
                return " ".join(title.split())
    return None


def _extract_image(soup: BeautifulSoup) -> Optional[str]:
    for selector in IMAGE_SELECTORS:
        el = soup.select_one(selector)
        if el:
            for attr in ["data-zoom", "src"]:
                url = el.get(attr, "")
                if url and url.startswith("http"):
                    # Remove parâmetros de resize do ML
                    url = re.sub(r"_\d+x\d+\.", "_0x0.", url)
                    return url
    return None


def _check_availability(soup: BeautifulSoup, html: str) -> bool:
    """Verifica disponibilidade no ML."""
    unavail_indicators = [
        "Produto indisponível",
        "Este produto não está disponível",
        "Produto pausado",
        "ui-pdp-buybox--unavailable",
    ]
    page_text = soup.get_text()
    for indicator in unavail_indicators:
        if indicator.lower() in page_text.lower():
            return False
    return True


# ─────────────────────────────────────────────────────────────
# FUNÇÃO PRINCIPAL
# ─────────────────────────────────────────────────────────────

async def scrape_mercadolivre(url: str) -> ScrapedProduct:
    """
    Faz scraping de produto do Mercado Livre Brasil.

    Args:
        url: URL do produto ML (formato: /MLB-XXXXXXXXX-...)

    Returns:
        ScrapedProduct com preço e metadados
    """
    logger.info(f"🔍 Mercado Livre scraping: {url[:80]}...")

    headers = get_random_headers(extra={
        "Referer": "https://www.mercadolivre.com.br/",
        "Accept-Language": "pt-BR,pt;q=0.9",
    })

    html = await fetch_page(url, headers=headers)

    if not html:
        return ScrapedProduct(
            success=False,
            store="mercadolivre",
            error="Não foi possível carregar a página",
        )

    soup = BeautifulSoup(html, "lxml")

    # ── Tenta JSON embutido primeiro ───────────────────────
    price = None
    json_data = _extract_json_data(html)
    if json_data:
        price = _find_price_in_json(json_data)
        if price:
            logger.debug(f"Preço ML via JSON: R${price}")

    # ── Fallback para CSS ───────────────────────────────────
    if not price:
        price = _extract_price_css(soup)

    original_price = _extract_original_price(soup)
    title = _extract_title(soup)
    image_url = _extract_image(soup)
    available = _check_availability(soup, html)

    if price is None:
        return ScrapedProduct(
            success=False,
            store="mercadolivre",
            name=title,
            availability=available,
            error="Preço não encontrado na página",
        )

    logger.info(
        f"✅ ML: '{(title or 'N/A')[:40]}' → R${price}"
        + (f" (de R${original_price})" if original_price else "")
    )

    return ScrapedProduct(
        success=True,
        store="mercadolivre",
        price=price,
        original_price=original_price,
        name=title,
        image_url=image_url,
        availability=available,
    )
