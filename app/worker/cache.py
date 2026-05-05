"""
cache.py — Cache Redis + Rate Limiter de Scraping

DOIS PROBLEMAS QUE ISSO RESOLVE:

1. CACHE DE PREÇOS:
   Problema: usuário A e usuário B monitoram o mesmo produto.
   Sem cache: fazemos 2 requests ao site (duplicado, lento, bandido).
   Com cache:  1 request real → guarda 5min no Redis → segundo usuário
               recebe do cache na hora.

   Resultado:
     - Sites não nos banem (menos requests)
     - Verificação muito mais rápida
     - Menor custo de servidor

2. RATE LIMITER POR DOMÍNIO:
   Problema: Amazon detecta bots se fazemos muitos requests seguidos.
   Sem limite: 100 produtos Amazon → 100 requests simultâneos → banimento.
   Com limite: máx 10 req/min para Amazon → nunca levamos ban.

   Tokens bucket algorithm:
     Cada domínio tem um "balde" de tokens.
     Cada request consome 1 token.
     Tokens reabastecidos a cada segundo.
     Se balde vazio → espera.

FALLBACK GRACIOSO:
   Se Redis estiver offline → sistema funciona normalmente (sem cache/rate limit).
   Nunca bloqueia o scraping por causa do Redis.
"""

import json
import time
import logging
import asyncio
from typing import Optional, Any
from datetime import datetime

from app.core.config import settings

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# CONEXÃO REDIS (com fallback)
# ─────────────────────────────────────────────────────────────

_redis_cache = None
_redis_rate = None
_redis_available = None   # None = não testado ainda


async def _get_redis_cache():
    """Retorna cliente Redis para cache. None se indisponível."""
    global _redis_cache, _redis_available
    if _redis_available is False:
        return None
    try:
        if _redis_cache is None:
            import redis.asyncio as aioredis
            _redis_cache = aioredis.from_url(
                settings.REDIS_CACHE_URL,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            await _redis_cache.ping()
            _redis_available = True
            logger.info("✅ Redis cache conectado")
        return _redis_cache
    except Exception as e:
        _redis_available = False
        logger.warning(f"⚠️ Redis indisponível — cache desativado: {e}")
        return None


async def _get_redis_rate():
    """Retorna cliente Redis para rate limiting."""
    global _redis_rate
    try:
        if _redis_rate is None:
            import redis.asyncio as aioredis
            _redis_rate = aioredis.from_url(
                settings.REDIS_RATE_LIMIT_URL,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        return _redis_rate
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# CACHE DE PREÇOS
# ─────────────────────────────────────────────────────────────

def _price_cache_key(url: str) -> str:
    """Gera chave de cache a partir da URL."""
    import hashlib
    url_hash = hashlib.md5(url.encode()).hexdigest()[:16]
    return f"price:v1:{url_hash}"


async def get_cached_price(url: str) -> Optional[dict]:
    """
    Busca preço no cache Redis.

    Returns:
        dict com {price, name, store, cached_at} ou None se não tem cache
    """
    r = await _get_redis_cache()
    if not r:
        return None

    try:
        key = _price_cache_key(url)
        data = await r.get(key)
        if data:
            result = json.loads(data)
            cached_at = result.get("cached_at", "")
            logger.debug(f"💾 Cache HIT: R${result.get('price')} (cached às {cached_at})")
            return result
        return None
    except Exception as e:
        logger.debug(f"Cache get error: {e}")
        return None


async def set_cached_price(url: str, price_data: dict, ttl: int = None) -> bool:
    """
    Salva preço no cache Redis com TTL.

    Args:
        url: URL do produto
        price_data: dict com dados do preço
        ttl: segundos de vida (default: PRICE_CACHE_TTL do config)
    """
    r = await _get_redis_cache()
    if not r:
        return False

    try:
        ttl = ttl or settings.PRICE_CACHE_TTL
        key = _price_cache_key(url)

        data = {
            **price_data,
            "cached_at": datetime.utcnow().isoformat(),
            "ttl": ttl,
        }

        await r.setex(key, ttl, json.dumps(data))
        logger.debug(f"💾 Cache SET: R${price_data.get('price')} TTL={ttl}s")
        return True
    except Exception as e:
        logger.debug(f"Cache set error: {e}")
        return False


async def invalidate_cache(url: str) -> bool:
    """Remove item do cache (força nova verificação)."""
    r = await _get_redis_cache()
    if not r:
        return False
    try:
        await r.delete(_price_cache_key(url))
        return True
    except Exception:
        return False


async def get_cache_stats() -> dict:
    """Retorna estatísticas do cache para o health check."""
    r = await _get_redis_cache()
    if not r:
        return {"available": False}
    try:
        info = await r.info("stats")
        keys = await r.dbsize()
        return {
            "available": True,
            "total_keys": keys,
            "hits": info.get("keyspace_hits", 0),
            "misses": info.get("keyspace_misses", 0),
            "hit_rate": round(
                info.get("keyspace_hits", 0) /
                max(info.get("keyspace_hits", 0) + info.get("keyspace_misses", 1), 1) * 100,
                1
            ),
        }
    except Exception as e:
        return {"available": True, "error": str(e)}


# ─────────────────────────────────────────────────────────────
# RATE LIMITER (Token Bucket)
# ─────────────────────────────────────────────────────────────

# Limite de requests por domínio (requests/minuto)
DOMAIN_RATE_LIMITS = {
    "amazon.com.br": settings.SCRAPE_RATE_AMAZON,
    "amazon.com": settings.SCRAPE_RATE_AMAZON,
    "mercadolivre.com.br": settings.SCRAPE_RATE_ML,
    "mercadolibre.com": settings.SCRAPE_RATE_ML,
    "shopee.com.br": 15,
    "magazineluiza.com.br": 12,
    "americanas.com.br": 12,
}

# Script Lua para token bucket atômico no Redis
# Garante que verificação e decremento são atômicos (thread-safe)
RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local current = redis.call('GET', key)
if current == false then
    redis.call('SET', key, limit - 1)
    redis.call('EXPIRE', key, window)
    return 1
end

current = tonumber(current)
if current > 0 then
    redis.call('DECR', key)
    return 1
else
    return 0
end
"""


def _extract_domain(url: str) -> str:
    """Extrai domínio da URL."""
    from urllib.parse import urlparse
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return "unknown"


async def acquire_rate_limit(url: str, wait: bool = True, max_wait: float = 30.0) -> bool:
    """
    Tenta adquirir token de rate limit para fazer scraping.

    Args:
        url: URL que será scraped
        wait: se True, aguarda até ter token disponível
        max_wait: máximo de segundos para aguardar

    Returns:
        True se pode prosseguir, False se timeout

    Uso:
        if await acquire_rate_limit(url):
            result = await scrape_product(url)
        else:
            # rate limited, tentar mais tarde
    """
    domain = _extract_domain(url)
    limit_per_minute = DOMAIN_RATE_LIMITS.get(domain, settings.SCRAPE_RATE_DEFAULT)

    r = await _get_redis_rate()
    if not r:
        # Redis indisponível: permite mas adiciona delay mínimo
        await asyncio.sleep(60 / limit_per_minute)
        return True

    key = f"rate:{domain}"
    window = 60   # janela de 1 minuto
    start_wait = time.time()

    while True:
        try:
            # Executa script Lua atômico
            script = r.register_script(RATE_LIMIT_SCRIPT)
            allowed = await script(
                keys=[key],
                args=[limit_per_minute, window, int(time.time())]
            )

            if allowed:
                logger.debug(f"🟢 Rate OK: {domain} ({limit_per_minute}/min)")
                return True

            if not wait:
                logger.debug(f"🔴 Rate LIMITED: {domain} — não aguardando")
                return False

            # Aguarda antes de tentar novamente
            elapsed = time.time() - start_wait
            if elapsed >= max_wait:
                logger.warning(f"⏱️ Rate limit timeout após {elapsed:.1f}s: {domain}")
                return False

            wait_time = 60 / limit_per_minute   # intervalo entre tokens
            logger.debug(f"⏳ Rate aguardando {wait_time:.1f}s: {domain}")
            await asyncio.sleep(wait_time)

        except Exception as e:
            logger.warning(f"Rate limit error: {e} — prosseguindo sem limite")
            return True


async def get_rate_limit_status() -> dict:
    """Retorna status atual dos rate limits para monitoramento."""
    r = await _get_redis_rate()
    if not r:
        return {"available": False}

    try:
        status = {}
        for domain, limit in DOMAIN_RATE_LIMITS.items():
            key = f"rate:{domain}"
            remaining = await r.get(key)
            status[domain] = {
                "limit_per_minute": limit,
                "tokens_remaining": int(remaining) if remaining else limit,
            }
        return {"available": True, "domains": status}
    except Exception as e:
        return {"available": True, "error": str(e)}


# ─────────────────────────────────────────────────────────────
# CACHE DE SESSÕES JWT (blacklist para logout real)
# ─────────────────────────────────────────────────────────────

async def blacklist_token(token: str, expires_in: int) -> bool:
    """
    Adiciona token JWT à blacklist (logout seguro).
    Token fica bloqueado até expirar naturalmente.

    Fase 3 usava logout client-side (simplesmente descarta o token).
    Fase 5 usa blacklist real no Redis — mais seguro.
    """
    r = await _get_redis_cache()
    if not r:
        return False
    try:
        import hashlib
        token_hash = hashlib.sha256(token.encode()).hexdigest()[:32]
        await r.setex(f"blacklist:{token_hash}", expires_in, "1")
        return True
    except Exception:
        return False


async def is_token_blacklisted(token: str) -> bool:
    """Verifica se token está na blacklist."""
    r = await _get_redis_cache()
    if not r:
        return False
    try:
        import hashlib
        token_hash = hashlib.sha256(token.encode()).hexdigest()[:32]
        return bool(await r.get(f"blacklist:{token_hash}"))
    except Exception:
        return False
