"""
affiliate.py — Sistema de Links Afiliados

Converte qualquer URL de produto em link com tracking de afiliado.

COMO FUNCIONA:
  1. Detecta a loja (Amazon, Mercado Livre, etc.)
  2. Aplica o parâmetro correto para aquela loja
  3. Retorna a URL modificada

MONETIZAÇÃO:
  - Amazon: tag de Associates (comissão por venda)
  - Mercado Livre: afiliados da plataforma
  - Outros: UTM params para rastrear cliques
"""

from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
from typing import Optional, Tuple
import re

from app.core.config import settings


# ─────────────────────────────────────────────────────────────
# DETECÇÃO DE LOJA
# ─────────────────────────────────────────────────────────────

STORE_PATTERNS = {
    "magalu": [
        r"magazineluiza\.com\.br",
        r"magalu\.com\.br",
    ],
    "amazon": [
        r"amazon\.com\.br",
        r"amazon\.com",
        r"amzn\.to",
        r"a\.co",
    ],
    "mercadolivre": [
        r"mercadolivre\.com\.br",
        r"mercadolibre\.com",
        r"mlb\.com\.br",
        r"meli\.com\.br",
    ],
    "americanas": [
        r"americanas\.com\.br",
    ],
    "shopee": [
        r"shopee\.com\.br",
    ],
    "magalu": [
        r"magazineluiza\.com\.br",
        r"(?<!a)magalu\.com\.br",
    ],
    "casasbahia": [
        r"casasbahia\.com\.br",
    ],
    "aliexpress": [
        r"aliexpress\.com",
        r"s\.click\.aliexpress\.com",
    ],
}


def detect_store(url: str) -> Optional[str]:
    """
    Detecta qual loja é a URL.

    Returns:
        Nome da loja ("amazon", "mercadolivre", etc.) ou None.

    >>> detect_store("https://www.amazon.com.br/produto/dp/B08N5WRWNW")
    'amazon'
    >>> detect_store("https://www.mercadolivre.com.br/produto/123")
    'mercadolivre'
    """
    url_lower = url.lower()

    for store, patterns in STORE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, url_lower):
                return store

    return None  # loja desconhecida


# ─────────────────────────────────────────────────────────────
# CONVERSORES POR LOJA
# ─────────────────────────────────────────────────────────────

def _add_amazon_affiliate(url: str) -> str:
    """
    Adiciona tag de afiliado Amazon.

    A Amazon usa o parâmetro 'tag' para rastrear vendas.
    Formato: ?tag=seutag-20

    Exemplo:
      Entrada:  https://www.amazon.com.br/dp/B08N5WRWNW
      Saída:    https://www.amazon.com.br/dp/B08N5WRWNW?tag=seutag-20
    """
    if not settings.AMAZON_AFFILIATE_TAG:
        return url  # sem tag configurada, retorna original

    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)

    # Sobrescreve ou adiciona a tag
    params["tag"] = [settings.AMAZON_AFFILIATE_TAG]

    # Reconstrói a URL com os novos parâmetros
    new_query = urlencode(params, doseq=True)
    new_url = urlunparse(parsed._replace(query=new_query))

    return new_url


def _add_mercadolivre_affiliate(url: str) -> str:
    """
    Adiciona parâmetros de afiliado do Mercado Livre.

    O ML usa UTM params para rastrear origens:
      utm_source, utm_medium, utm_campaign

    Nota: Para afiliados pagos, usar a plataforma oficial do ML.
    Aqui usamos UTM simples para rastreamento básico.
    """
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)

    # UTM básico — troque pelos valores do seu programa de afiliados
    params["utm_source"] = ["price_monitor_bot"]
    params["utm_medium"] = ["affiliate"]
    params["utm_campaign"] = ["price_alert"]

    if settings.MERCADOLIVRE_AFFILIATE_ID:
        params["partner_id"] = [settings.MERCADOLIVRE_AFFILIATE_ID]

    new_query = urlencode(params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def _add_generic_utm(url: str, source: str = "price_monitor") -> str:
    """
    Adiciona UTM genérico para lojas sem afiliado configurado.

    Serve para rastrear cliques mesmo sem programa de afiliados.
    """
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)

    params["utm_source"] = [source]
    params["utm_medium"] = ["bot"]
    params["utm_campaign"] = ["price_alert"]

    new_query = urlencode(params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


# ─────────────────────────────────────────────────────────────
# FUNÇÃO PRINCIPAL
# ─────────────────────────────────────────────────────────────

def generate_affiliate_url(url: str) -> Tuple[str, Optional[str]]:
    """
    Converte uma URL em link com tracking de afiliado.

    Returns:
        Tuple[str, Optional[str]]: (url_afiliada, nome_da_loja)

    Uso:
        url_afiliada, loja = generate_affiliate_url(url_original)
    """
    store = detect_store(url)

    # Seleciona o conversor correto para cada loja
    converters = {
        "amazon": _add_amazon_affiliate,
        "mercadolivre": _add_mercadolivre_affiliate,
    }

    if store and store in converters:
        affiliate_url = converters[store](url)
    else:
        # Loja desconhecida: aplica UTM genérico
        affiliate_url = _add_generic_utm(url, source=store or "unknown")

    return affiliate_url, store


# ─────────────────────────────────────────────────────────────
# UTILITÁRIOS
# ─────────────────────────────────────────────────────────────

def clean_url(url: str) -> str:
    """
    Remove parâmetros de rastreamento de terceiros da URL.

    Antes de salvar a URL original, limpa parâmetros como:
    fbclid, gclid, ref, source, etc.

    Isso garante que o scraping funcione corretamente.
    """
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)

    # Parâmetros que podem atrapalhar (rastreamento de outros)
    junk_params = {
        "fbclid", "gclid", "utm_source", "utm_medium",
        "utm_campaign", "utm_term", "utm_content",
        "ref", "source", "ref_", "ref_src",
        "_encoding", "pf_rd_p", "pf_rd_r",  # Amazon internos
        "smid", "spLa",  # Amazon internos
    }

    clean_params = {k: v for k, v in params.items() if k not in junk_params}
    new_query = urlencode(clean_params, doseq=True)

    return urlunparse(parsed._replace(query=new_query))
