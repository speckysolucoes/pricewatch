"""config.py — Configurações centrais (Fase 5 — completo)"""

from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    APP_NAME: str = "Price Monitor Bot"
    APP_VERSION: str = "5.0.0"
    DEBUG: bool = True
    BASE_URL: str = "http://localhost:8000"

    DATABASE_URL: str = "sqlite:///./price_monitor.db"
    CHECK_INTERVAL_MINUTES: int = 30

    AMAZON_AFFILIATE_TAG: Optional[str] = "seutag-20"
    MERCADOLIVRE_AFFILIATE_ID: Optional[str] = None

    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHANNEL_ID: Optional[str] = None

    JWT_SECRET_KEY: str = "TROQUE_ISSO_EM_PRODUCAO_use_secrets_token_hex_32"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    CORS_ORIGINS: str = "*"
    USE_MOCK: bool = False

    # Pagamentos (Fase 4)
    MERCADOPAGO_ACCESS_TOKEN: Optional[str] = None
    MERCADOPAGO_WEBHOOK_SECRET: Optional[str] = None
    MERCADOPAGO_PUBLIC_KEY: Optional[str] = None
    STRIPE_SECRET_KEY: Optional[str] = None
    STRIPE_WEBHOOK_SECRET: Optional[str] = None
    STRIPE_PUBLIC_KEY: Optional[str] = None
    PLAN_PREMIUM_PRICE_BRL: int = 1900
    PLAN_PREMIUM_PRICE_USD: int = 499

    # Redis / Celery (Fase 5)
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_CACHE_URL: str = "redis://localhost:6379/1"
    REDIS_RATE_LIMIT_URL: str = "redis://localhost:6379/2"
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/0"
    CELERY_HIGH_PRIORITY_QUEUE: str = "high_priority"
    CELERY_DEFAULT_QUEUE: str = "default"

    # Rate limiting scraping (requests/minuto por domínio)
    SCRAPE_RATE_AMAZON: int = 10
    SCRAPE_RATE_ML: int = 20
    SCRAPE_RATE_DEFAULT: int = 15

    # Cache TTL (segundos)
    PRICE_CACHE_TTL: int = 300   # 5 minutos

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
