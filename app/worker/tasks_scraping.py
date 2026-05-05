"""
tasks_scraping.py — Tasks de Verificação de Preços

FLUXO DE UMA VERIFICAÇÃO:
  1. run_monitoring_cycle() dispara check_product_price() para cada produto
     → Premium vai para high_priority queue
     → Free vai para default queue

  2. check_product_price():
     a. Verifica cache Redis → retorna imediatamente se fresco
     b. acquire_rate_limit() → aguarda slot disponível para o domínio
     c. Faz scraping real
     d. Guarda no cache
     e. Compara com alvo → se atingiu, dispara send_price_alert()
     f. Atualiza banco

RETRY AUTOMÁTICO:
  Se scraping falhar (timeout, 5xx, CAPTCHA):
    - Tenta novamente em 30s, 2min, 10min
    - Após 3 falhas → marca produto como ERROR no banco
    - Próximo ciclo tenta de novo automaticamente
"""

import logging
from datetime import datetime
from typing import Optional

from celery import shared_task
from celery.utils.log import get_task_logger

from app.worker.celery_app import celery_app

logger = get_task_logger(__name__)


# ─────────────────────────────────────────────────────────────
# TASK: verificar preço de um produto
# ─────────────────────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="app.worker.tasks_scraping.check_product_price",
    max_retries=3,
    default_retry_delay=30,         # 1ª retry após 30s
    autoretry_for=(Exception,),
    retry_backoff=True,             # 30s → 60s → 120s
    retry_backoff_max=600,          # máx 10min entre retries
    retry_jitter=True,              # variação aleatória (evita thundering herd)
    time_limit=60,                  # task é cancelada após 60s
    soft_time_limit=50,             # aviso antes de cancelar
    acks_late=True,
)
def check_product_price(self, product_id: int, user_plan: str = "free"):
    """
    Verifica o preço atual de um produto.

    Args:
        product_id: ID do produto no banco
        user_plan: "free" ou "premium" (para logging/métricas)

    Esta é a task central do sistema.
    Chamada pelo run_monitoring_cycle() para cada produto.
    """
    import asyncio

    async def _run():
        from app.db.database import SessionLocal
        from app.models.models import Product, PriceHistory, ProductStatus, AlertType
        from app.worker.cache import get_cached_price, set_cached_price, acquire_rate_limit
        from app.services.price_checker import check_price

        db = SessionLocal()
        try:
            # Carrega produto
            product = db.query(Product).filter(Product.id == product_id).first()
            if not product or product.status != ProductStatus.ACTIVE:
                logger.info(f"[{product_id}] Produto inativo ou não encontrado — pulando")
                return {"skipped": True}

            logger.info(f"[{product_id}] Verificando: {product.name[:40]} ({user_plan})")

            # ── 1. Verifica cache ──────────────────────────
            cached = await get_cached_price(product.url)
            if cached:
                price = cached["price"]
                from_cache = True
                logger.info(f"[{product_id}] 💾 Cache: R${price}")
            else:
                # ── 2. Rate limit ──────────────────────────
                allowed = await acquire_rate_limit(product.url, wait=True, max_wait=30.0)
                if not allowed:
                    raise Exception(f"Rate limit timeout para {product.store}")

                # ── 3. Scraping real ───────────────────────
                result = await check_price(product.url, product.store)

                if not result.success:
                    product.status = ProductStatus.ERROR
                    product.last_checked_at = datetime.utcnow()
                    db.commit()
                    raise Exception(f"Scraping falhou: {result.error}")

                price = result.price
                from_cache = False

                # ── 4. Salva no cache ──────────────────────
                await set_cached_price(product.url, {
                    "price": price,
                    "name": result.product_name,
                    "store": product.store,
                })

                # Atualiza nome se o scraper retornou um melhor
                if result.product_name and result.product_name != product.name:
                    product.name = result.product_name

            # ── 5. Registra histórico ──────────────────────
            history = PriceHistory(
                product_id=product.id,
                price=price,
                is_available=True,
            )
            db.add(history)

            # ── 6. Atualiza preços no produto ──────────────
            previous_price = product.current_price
            product.current_price = price
            product.last_checked_at = datetime.utcnow()

            if product.initial_price is None:
                product.initial_price = price

            if product.lowest_price is None or price < product.lowest_price:
                product.lowest_price = price

            if product.status == ProductStatus.ERROR:
                product.status = ProductStatus.ACTIVE

            db.commit()

            # ── 7. Verifica condições de alerta ────────────
            should_alert = False
            alert_type = None

            if (product.target_price and price <= product.target_price
                    and product.status == ProductStatus.ACTIVE):
                should_alert = True
                alert_type = AlertType.PRICE_DROP

            elif (product.alert_percentage and previous_price and previous_price > 0):
                drop_pct = (1 - price / previous_price) * 100
                if drop_pct >= product.alert_percentage:
                    should_alert = True
                    alert_type = AlertType.PERCENTAGE_DROP

            # ── 8. Dispara alerta (task separada) ─────────
            if should_alert:
                product.status = ProductStatus.ALERTED
                db.commit()

                # Enfileira task de alerta na fila dedicada
                from app.worker.tasks_alerts import send_price_alert
                send_price_alert.apply_async(
                    args=[product.id, price, previous_price, alert_type.value],
                    queue="alerts",
                    priority=9 if user_plan == "premium" else 5,
                )

            return {
                "product_id": product_id,
                "price": price,
                "from_cache": from_cache,
                "alerted": should_alert,
                "plan": user_plan,
            }

        finally:
            db.close()

    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()


# ─────────────────────────────────────────────────────────────
# TASK: ciclo completo de monitoramento
# ─────────────────────────────────────────────────────────────

@celery_app.task(
    name="app.worker.tasks_scraping.run_monitoring_cycle",
    time_limit=300,   # 5 minutos para o ciclo inteiro
)
def run_monitoring_cycle():
    """
    Enfileira verificação de preços para todos os produtos ativos.

    PRIORIDADE:
      Premium → high_priority queue (verificados primeiro)
      Free    → default queue (verificados depois)

    Esta task apenas ENFILEIRA — não executa scraping diretamente.
    O scraping é feito pelos workers que consomem as filas.

    Chamada pelo Celery Beat a cada CHECK_INTERVAL_MINUTES.
    """
    from app.db.database import SessionLocal
    from app.models.models import Product, ProductStatus, User
    from app.core.plans import get_user_plan

    db = SessionLocal()
    try:
        products = (
            db.query(Product)
            .filter(Product.status == ProductStatus.ACTIVE)
            .all()
        )

        total = len(products)
        premium_count = 0
        free_count = 0

        for product in products:
            # Descobre o plano do usuário
            user = db.query(User).filter(User.id == product.user_id).first()
            user_plan = get_user_plan(user).id if user else "free"
            is_premium = user_plan == "premium"

            # Enfileira na fila correta com prioridade
            check_product_price.apply_async(
                args=[product.id, user_plan],
                queue="high_priority" if is_premium else "default",
                priority=9 if is_premium else 5,
            )

            if is_premium:
                premium_count += 1
            else:
                free_count += 1

        logger.info(
            f"📦 Ciclo iniciado: {total} produtos enfileirados "
            f"({premium_count} premium, {free_count} free)"
        )

        return {
            "total": total,
            "premium": premium_count,
            "free": free_count,
            "timestamp": datetime.utcnow().isoformat(),
        }

    finally:
        db.close()
