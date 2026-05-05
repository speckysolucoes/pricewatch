"""
tasks_notifications.py — Tasks de Notificações

Separar notificações em fila própria garante que:
  - Telegram offline não para o scraping
  - Retry de notificação não interfere com verificação de preços
  - Workers de notificação podem escalar independentemente
"""

import logging
from datetime import datetime
from typing import Optional

from celery.utils.log import get_task_logger
from app.worker.celery_app import celery_app

logger = get_task_logger(__name__)


@celery_app.task(
    bind=True,
    name="app.worker.tasks_notifications.send_price_alert",
    queue="high_priority",    # alertas são prioritários
    max_retries=5,
    default_retry_delay=10,
    autoretry_for=(Exception,),
    retry_backoff=True,
    time_limit=30,
    acks_late=True,           # só confirma após entrega
)
def send_price_alert(
    self,
    product_id: int,
    current_price: float,
    previous_price: Optional[float],
    alert_type_value: str,
):
    """
    Envia alerta de queda de preço ao usuário via Telegram.

    Retry agressivo (5x) — notificação não pode ser perdida.
    """
    from app.db.database import SessionLocal
    from app.models.models import Product, AlertType
    import asyncio

    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == product_id).first()
        if not product:
            logger.warning(f"Produto {product_id} não encontrado para alerta")
            return {"skipped": "product_not_found"}

        alert_type = AlertType(alert_type_value)

        # Usa o notifier existente (suporte a Telegram + console)
        from app.services.notifier import send_alert

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            alert = loop.run_until_complete(
                send_alert(
                    db=db,
                    product=product,
                    current_price=current_price,
                    alert_type=alert_type,
                    previous_price=previous_price,
                )
            )
        finally:
            loop.close()

        logger.info(
            f"✅ Alerta enviado: produto={product_id} "
            f"preço=R${current_price} type={alert_type_value}"
        )
        return {
            "alert_id": alert.id,
            "delivered": alert.delivered,
            "product_id": product_id,
        }

    except Exception as exc:
        logger.error(f"Erro ao enviar alerta produto={product_id}: {exc}")
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(
    name="app.worker.tasks_notifications.publish_promotions",
    queue="notifications",
    time_limit=300,
    max_retries=2,
)
def publish_promotions():
    """
    Detecta e publica promoções boas no canal Telegram.
    Roda a cada hora após o ciclo de verificação de preços.
    """
    from app.db.database import SessionLocal
    import asyncio

    db = SessionLocal()
    try:
        from app.telegram.promotions import detect_and_publish_promotions
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            stats = loop.run_until_complete(detect_and_publish_promotions(db))
        finally:
            loop.close()

        logger.info(f"[Canal] Promoções: {stats}")
        return stats
    finally:
        db.close()


@celery_app.task(
    bind=True,
    name="app.worker.tasks_notifications.activate_premium_notification",
    queue="high_priority",
    max_retries=3,
    autoretry_for=(Exception,),
)
def activate_premium_notification(self, user_id: int, expires_at_iso: str):
    """
    Notifica usuário que o plano Premium foi ativado.
    Disparado pelo webhook de pagamento após activate_premium().
    """
    from app.db.database import SessionLocal
    from app.models.models import User
    from app.core.config import settings

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.telegram_id:
            return {"skipped": "no_telegram"}

        if not settings.TELEGRAM_BOT_TOKEN:
            return {"skipped": "no_telegram_token"}

        import asyncio
        from telegram import Bot
        from telegram.constants import ParseMode
        from app.telegram.messages import escape_md

        expires = datetime.fromisoformat(expires_at_iso)
        msg = (
            "🎉 *Premium Ativado\\!*\n\n"
            "Você agora tem acesso a:\n"
            "• 📦 Produtos ilimitados\n"
            "• ⚡ Verificação a cada 15 minutos\n"
            "• 📅 Histórico de 1 ano\n"
            "• 📢 Canal de promoções\n"
            "• 🚀 Scraping prioritário\n\n"
            f"Válido até: *{escape_md(expires.strftime('%d/%m/%Y'))}*\n\n"
            "Obrigado por assinar o PriceWatch\\! 🙏"
        )

        async def _send():
            bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
            await bot.send_message(
                chat_id=user.telegram_id,
                text=msg,
                parse_mode=ParseMode.MARKDOWN_V2,
            )

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_send())
        finally:
            loop.close()

        logger.info(f"✅ Notificação Premium enviada para user {user_id}")
        return {"sent": True, "user_id": user_id}

    finally:
        db.close()
