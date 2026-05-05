"""
amazon.py — Scraper da Amazon Brasil

Estratégia de extração em camadas:
  1. Seletores CSS principais (estrutura atual da Amazon)
  2. Seletores alternativos (Amazon muda o HTML frequentemente)
  3. Regex como último recurso

IMPORTANTE:
  A Amazon detecta bots e pode retornar CAPTCHA.
  Mitigações aplicadas:
    - User-Agent rotativo
    - Headers realistas
    - Delay entre requests
    - Cookies de sessão respeitados

  Para produção com volume alto → considerar:
    - Proxy rotativo
    - ScraperAPI / Zyte / Bright Data
    - API oficial (Amazon PA API 5.0)
"""

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from app.scrapers.base import ScrapedProduct, fetch_page, get_random_headers, parse_price

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# SELETORES CSS (em ordem de prioridade)
# Amazon muda a estrutura regularmente — múltiplos fallbacks
# ─────────────────────────────────────────────────────────────

# Seletores de preço — tenta cada um até encontrar
PRICE_SELECTORS = [
    # Preço principal (produto simples)
    ".a-price.aok-align-center.reinventPricePriceToPayMargin .a-offscreen",
    ".a-price .a-offscreen",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    "#priceblock_saleprice",
    ".a-price-whole",  # só o número inteiro (precisa pegar fração separado)
    "#price_inside_buybox",
    "#newBuyBoxPrice",
    ".a-color-price",
    "[data-feature-name='priceInsideBuyBox'] .a-price .a-offscreen",
]

# Seletores de preço original ("De:")
ORIGINAL_PRICE_SELECTORS = [
    ".a-price.a-text-price .a-offscreen",
    "#priceblock_ourprice + .a-color-secondary .a-offscreen",
    ".basisPrice .a-offscreen",
    ".a-text-strike",
]

# Seletores do título do produto
TITLE_SELECTORS = [
    "#productTitle",
    "#title",
    "h1.a-size-large",
    "h1[data-cel-widget='title'] span",
]

# Seletores de imagem principal
IMAGE_SELECTORS = [
    "#landingImage",
    "#imgBlkFront",
    "#ebooksImgBlkFront",
    ".a-dynamic-image.a-stretch-vertical",
]

# Seletores para verificar disponibilidade
UNAVAILABILITY_KEYWORDS = [
    "atualmente indisponível",
    "currently unavailable",
    "não disponível",
    "esgotado",
    "out of stock",
    "fora de estoque",
]


# ─────────────────────────────────────────────────────────────
# EXTRATORES
# ─────────────────────────────────────────────────────────────

def _extract_price(soup: BeautifulSoup) -> Optional[float]:
    """Tenta extrair preço usando múltiplos seletores."""
    for selector in PRICE_SELECTORS:
        elements = soup.select(selector)
        for el in elements:
            text = el.get_text(strip=True)
            if text:
                price = parse_price(text)
                if price:
                    logger.debug(f"Preço encontrado com seletor '{selector}': R${price}")
                    return price

    # Fallback: regex direto no HTML
    # Procura padrões como "R$ 1.299,90" ou "1299,90"
    html = str(soup)
    patterns = [
        r'"priceAmount":\s*([\d.]+)',           # JSON embutido
        r'R\$\s*([\d.,]+)',                     # texto visível
        r'"buyingPrice":\s*([\d.]+)',            # outro JSON
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            price = parse_price(match.group(1))
            if price:
                logger.debug(f"Preço via regex: R${price}")
                return price

    return None


def _extract_original_price(soup: BeautifulSoup) -> Optional[float]:
    """Extrai preço original (antes do desconto)."""
    for selector in ORIGINAL_PRICE_SELECTORS:
        elements = soup.select(selector)
        for el in elements:
            text = el.get_text(strip=True)
            if text:
                price = parse_price(text)
                if price:
                    return price
    return None


def _extract_title(soup: BeautifulSoup) -> Optional[str]:
    """Extrai título do produto."""
    for selector in TITLE_SELECTORS:
        el = soup.select_one(selector)
        if el:
            title = el.get_text(strip=True)
            if title and len(title) > 3:
                # Limpa espaços extras internos
                return " ".join(title.split())
    return None


def _extract_image(soup: BeautifulSoup) -> Optional[str]:
    """Extrai URL da imagem principal."""
    for selector in IMAGE_SELECTORS:
        el = soup.select_one(selector)
        if el:
            # Tenta src, data-src, data-old-hires (maior resolução)
            for attr in ["data-old-hires", "data-a-dynamic-image", "src"]:
                url = el.get(attr, "")
                if url and url.startswith("http"):
                    # data-a-dynamic-image é um JSON com URLs
                    if attr == "data-a-dynamic-image":
                        import json
                        try:
                            images = json.loads(url)
                            if images:
                                # Pega a de maior resolução
                                return max(images.keys(), key=lambda u: images[u][0])
                        except Exception:
                            pass
                    else:
                        return url
    return None


def _check_availability(soup: BeautifulSoup) -> bool:
    """Verifica se o produto está disponível para compra."""
    # Procura mensagens de indisponibilidade
    availability_div = soup.select_one("#availability")
    if availability_div:
        text = availability_div.get_text(strip=True).lower()
        for keyword in UNAVAILABILITY_KEYWORDS:
            if keyword in text:
                return False

    # Se não encontrou "buy box" (botão de comprar), provavelmente indisponível
    buy_box = soup.select_one("#buybox") or soup.select_one("#add-to-cart-button")
    if not buy_box:
        # Mas não marca como indisponível só por isso (pode ser outro layout)
        logger.debug("Buy box não encontrado — verificando outros indicadores")

    return True


def _is_captcha(html: str) -> bool:
    """Detecta se a Amazon retornou página de CAPTCHA."""
    captcha_indicators = [
        "Type the characters you see in this image",
        "Digite os caracteres que você vê",
        "robot_check",
        "api-services-support",
        "/errors/validateCaptcha",
    ]
    for indicator in captcha_indicators:
        if indicator in html:
            return True
    return False


# ─────────────────────────────────────────────────────────────
# FUNÇÃO PRINCIPAL
# ─────────────────────────────────────────────────────────────

async def scrape_amazon(url: str) -> ScrapedProduct:
    """
    Faz scraping de produto Amazon Brasil.

    Args:
        url: URL do produto Amazon (aceita /dp/, /gp/product/, amzn.to, etc.)

    Returns:
        ScrapedProduct com preço e metadados

    Uso:
        result = await scrape_amazon("https://amazon.com.br/dp/B08N5WRWNW")
        if result.success:
            print(f"Preço: R$ {result.price}")
    """
    logger.info(f"🔍 Amazon scraping: {url[:80]}...")

    # Headers específicos para Amazon
    headers = get_random_headers(extra={
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Referer": "https://www.amazon.com.br/",
    })

    html = await fetch_page(url, headers=headers)

    if not html:
        return ScrapedProduct(
            success=False,
            store="amazon",
            error="Não foi possível carregar a página",
        )

    # Detecta CAPTCHA
    if _is_captcha(html):
        logger.warning(f"⚠️ CAPTCHA detectado para {url[:50]}")
        return ScrapedProduct(
            success=False,
            store="amazon",
            error="CAPTCHA detectado — tente novamente mais tarde",
        )

    # Parse HTML
    soup = BeautifulSoup(html, "lxml")

    # Extrai dados
    price = _extract_price(soup)
    original_price = _extract_original_price(soup)
    title = _extract_title(soup)
    image_url = _extract_image(soup)
    available = _check_availability(soup)

    if price is None:
        logger.warning(f"Preço não encontrado para {url[:50]}")
        return ScrapedProduct(
            success=False,
            store="amazon",
            availability=available,
            name=title,
            error="Preço não encontrado na página",
        )

    logger.info(
        f"✅ Amazon: '{(title or 'N/A')[:40]}' "
        f"→ R${price}"
        + (f" (de R${original_price})" if original_price else "")
    )

    return ScrapedProduct(
        success=True,
        store="amazon",
        price=price,
        original_price=original_price,
        name=title,
        image_url=image_url,
        availability=available,
    )
