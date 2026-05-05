"""
tasks_alerts.py — Tasks de Notificação e Promoções

Separar alertas em fila própria garante que:
  - Um scraping lento não atrasa notificações
  - Retry de alerta não interfere com scraping
  - Workers de alertas podem ter concorrência diferente
"""

import logging
from datetime import datetime
from typing import Optional

from celery.utils.log import get_task_logger
from app.worker.celery_app import celery_app

logger = get_task_logger(__name__)


@celery_app.task(
    bind=True,
    name="app.worker.tasks_alerts.send_price_alert",
    max_retries=5,
    default_retry_delay=10,
    autoretry_for=(Exception,),
    retry_backoff=True,
    time_limit=30,
    acks_late=True,
)
def send_price_alert(
    self,
    product_id: int,
    current_price: float,
    previous_price: Optional[float],
    alert_type_value: str,
):
    """
    Envia alerta de queda de preço ao usuário.

    Retry agressivo (5x) porque notificação não pode ser perdida.
    Retry com backoff: 10s → 20s → 40s → 80s → 160s
    """
    import asyncio
    from app.db.database import SessionLocal
    from app.models.models import Product, Alert, AlertType
    from app.services.notifier import send_alert

    async def _run():
        db = SessionLocal()
        try:
            product = db.query(Product).filter(Product.id == product_id).first()
            if not product:
                logger.error(f"Produto {product_id} não encontrado para alerta")
                return

            alert_type = AlertType(alert_type_value)
            alert = await send_alert(
                db=db,
                product=product,
                current_price=current_price,
                alert_type=alert_type,
                previous_price=previous_price,
            )

            logger.info(
                f"✅ Alerta enviado: produto={product_id} "
                f"preço=R${current_price} tipo={alert_type_value} "
                f"entregue={alert.delivered}"
            )

            return {"alert_id": alert.id, "delivered": alert.delivered}

        finally:
            db.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()


@celery_app.task(
    name="app.worker.tasks_alerts.publish_promotion",
    max_retries=3,
    time_limit=30,
)
def publish_promotion(product_id: int, current_price: float, previous_price: float):
    """Publica promoção detectada no canal Telegram."""
    import asyncio
    from app.db.database import SessionLocal
    from app.models.models import Product
    from app.telegram.promotions import publish_to_channel

    async def _run():
        db = SessionLocal()
        try:
            product = db.query(Product).filter(Product.id == product_id).first()
            if not product:
                return
            discount = round((1 - current_price / previous_price) * 100, 1)
            published = await publish_to_channel(product, current_price, previous_price, discount)
            logger.info(f"📢 Promoção publicada: produto={product_id} desc={discount}%")
            return {"published": published}
        finally:
            db.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()


@celery_app.task(
    name="app.worker.tasks_alerts.run_promotion_detection",
    time_limit=120,
)
def run_promotion_detection():
    """
    Analisa todos os produtos em busca de promoções boas.
    Chamado pelo Beat a cada hora.
    """
    import asyncio
    from app.db.database import SessionLocal
    from app.telegram.promotions import detect_and_publish_promotions

    async def _run():
        db = SessionLocal()
        try:
            stats = await detect_and_publish_promotions(db)
            logger.info(f"[Canal] Detecção concluída: {stats}")
            return stats
        finally:
            db.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()
