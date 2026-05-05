"""
main.py — Ponto de entrada (Fase 5 — Completo)

Modo de execução:
  DESENVOLVIMENTO (sem Redis):
    USE_MOCK=true uvicorn main:app --reload
    → Usa APScheduler integrado (como fases 1-4)

  PRODUÇÃO (com Redis/Celery):
    docker compose up -d
    → Celery Beat gerencia o scheduler
    → Workers independentes por fila
    → Nginx como proxy reverso
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.core.config import settings
from app.db.database import init_db
from app.api.routes import users, products
from app.api.routes import monitor as monitor_routes
from app.api.routes import auth as auth_routes
from app.api.routes import dashboard as dashboard_routes
from app.api.routes import billing as billing_routes
from app.api.routes import admin as admin_routes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend")

# Detecta se Redis está disponível
def _redis_available() -> bool:
    try:
        import redis as redis_lib
        r = redis_lib.from_url(settings.REDIS_URL, socket_connect_timeout=2)
        r.ping()
        return True
    except Exception:
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"🚀 Iniciando {settings.APP_NAME} v{settings.APP_VERSION}")
    init_db()

    redis_ok = _redis_available()

    if redis_ok:
        # MODO PRODUÇÃO: Celery gerencia scheduling
        logger.info("✅ Redis conectado — modo Celery (workers externos gerenciam tasks)")
        logger.info("   Execute: docker compose up -d worker-scraping beat flower")
    else:
        # MODO DESENVOLVIMENTO: APScheduler embutido
        logger.warning("⚠️  Redis não disponível — usando APScheduler (modo desenvolvimento)")
        from app.services.scheduler import start_scheduler
        start_scheduler()

    # Bot Telegram (independente do Redis)
    if settings.TELEGRAM_BOT_TOKEN:
        try:
            from app.telegram.bot import start_bot
            await start_bot()
            logger.info("🤖 Telegram bot iniciado")
        except Exception as e:
            logger.warning(f"Telegram bot falhou: {e}")
    else:
        logger.info("💬 Telegram não configurado (opcional)")

    # Detalhes do ambiente
    gw = "mock"
    if settings.MERCADOPAGO_ACCESS_TOKEN: gw = "mercadopago"
    elif settings.STRIPE_SECRET_KEY: gw = "stripe"
    logger.info(f"💳 Gateway de pagamento: {gw.upper()}")
    logger.info(f"🌐 Frontend:  http://localhost:{os.environ.get('PORT','8000')}/app")
    logger.info(f"📖 API Docs:  http://localhost:{os.environ.get('PORT','8000')}/docs")

    yield

    if not redis_ok:
        from app.services.scheduler import stop_scheduler
        stop_scheduler()

    if settings.TELEGRAM_BOT_TOKEN:
        try:
            from app.telegram.bot import stop_bot
            await stop_bot()
        except Exception:
            pass

    logger.info("👋 Sistema encerrado")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="""
## 🤖 Price Monitor Bot — Sistema Completo

Monitor de preços com afiliados, Telegram, planos pagos e escala com Celery.

### Fases implementadas:
- **Fase 1** — MVP: CRUD, monitoramento, alertas
- **Fase 2** — Scraping real + bot Telegram + canal de promoções
- **Fase 3** — Interface web + autenticação JWT + dashboard
- **Fase 4** — Pagamentos (Mercado Pago / Stripe) + planos Free/Premium
- **Fase 5** — Escala com Celery + Redis + Docker + Nginx
    """,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Todas as rotas registradas
app.include_router(auth_routes.router)
app.include_router(users.router)
app.include_router(products.router)
app.include_router(monitor_routes.router)
app.include_router(dashboard_routes.router)
app.include_router(billing_routes.router)
app.include_router(admin_routes.router)


@app.get("/app", include_in_schema=False)
@app.get("/app/{full_path:path}", include_in_schema=False)
async def serve_frontend(full_path: str = ""):
    index = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"error": "Frontend não encontrado."}


@app.get("/", tags=["Root"])
async def root():
    redis_ok = _redis_available()
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "frontend": "/app",
        "docs": "/docs",
        "mode": "celery" if redis_ok else "apscheduler",
        "redis": "connected" if redis_ok else "unavailable",
        "phases": [1, 2, 3, 4, 5],
    }
