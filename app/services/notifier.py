"""
notifier.py — Sistema de Alertas (Fase 2)

Fase 1: apenas console/log
Fase 2: Telegram real + console como fallback
"""

import logging
from typing import Optional
from datetime import datetime
from sqlalchemy.orm import Session

from app.models.models import Product, Alert, AlertType, User
from app.core.config import settings

logger = logging.getLogger(__name__)


def format_alert_message(product, current_price, previous_price, alert_type) -> str:
    """Formata mensagem de alerta em texto simples (log/console)."""
    discount_pct = None
    if previous_price and previous_price > 0:
        discount_pct = round((1 - current_price / previous_price) * 100, 1)

    if alert_type == AlertType.PRICE_DROP:
        header = "🎯 PREÇO ALVO ATINGIDO!"
    elif alert_type == AlertType.PERCENTAGE_DROP:
        header = f"📉 QUEDA DE {discount_pct}%!"
    else:
        header = "🔥 PROMOÇÃO DETECTADA!"

    lines = [header, "", f"📦 {product.name}", f"🏪 Loja: {(product.store or 'N/A').upper()}", ""]
    if previous_price:
        lines.append(f"💰 De: R$ {previous_price:.2f}")
    lines.append(f"✅ Por: R$ {current_price:.2f}")
    if discount_pct:
        lines.append(f"📊 Desconto: {discount_pct}%")
    url = product.url_affiliate or product.url
    lines.extend(["", f"🔗 {url}", "", "─" * 30, "⚡ via Price Monitor Bot"])
    return "\n".join(lines)


async def _send_console(message: str, user=None):
    user_info = f"[User: {user.email}]" if user else "[Canal]"
    print("\n" + "=" * 50)
    print(f"🔔 ALERTA {user_info}")
    print("=" * 50)
    print(message)
    print("=" * 50 + "\n")
    return True


async def _send_telegram_dm(message: str, telegram_id: str) -> bool:
    """Envia mensagem direta ao usuário no Telegram."""
    if not settings.TELEGRAM_BOT_TOKEN:
        return False

    try:
        from app.telegram.bot import get_bot_application
        from telegram.constants import ParseMode
        from app.telegram.messages import msg_alert as fmt_tg_alert

        app = get_bot_application()
        if not app:
            return False

        await app.bot.send_message(
            chat_id=telegram_id,
            text=message,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=False,
        )
        return True

    except Exception as e:
        logger.error(f"Erro ao enviar Telegram DM para {telegram_id}: {e}")
        return False


async def send_alert(
    db: Session,
    product: Product,
    current_price: float,
    alert_type: AlertType,
    previous_price: Optional[float] = None,
) -> Alert:
    """
    Envia alerta de preço ao usuário.
    Fase 2: tenta Telegram primeiro, console como fallback.
    """
    discount_pct = None
    if previous_price and previous_price > 0:
        discount_pct = round((1 - current_price / previous_price) * 100, 2)

    # Formata mensagem de log
    plain_message = format_alert_message(product, current_price, previous_price, alert_type)

    delivered = False

    # Tenta Telegram se usuário tem ID configurado
    if product.user and product.user.telegram_id and settings.TELEGRAM_BOT_TOKEN:
        try:
            from app.telegram.messages import msg_alert as fmt_tg
            tg_message = fmt_tg(
                product=product,
                current_price=current_price,
                previous_price=previous_price,
                alert_type=alert_type,
            )
            delivered = await _send_telegram_dm(tg_message, product.user.telegram_id)
            if delivered:
                logger.info(f"✅ Alerta enviado via Telegram para {product.user.telegram_id}")
        except Exception as e:
            logger.error(f"Falha no Telegram: {e}")

    # Sempre loga no console (útil para debug)
    await _send_console(plain_message, user=product.user)

    # Registra no banco
    alert = Alert(
        product_id=product.id,
        alert_type=alert_type,
        price_at_alert=current_price,
        previous_price=previous_price,
        discount_percentage=discount_pct,
        message_sent=plain_message,
        delivered=delivered,
        sent_at=datetime.utcnow(),
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    logger.info(
        f"Alerta [{alert_type}]: produto={product.id} "
        f"preço=R${current_price} desconto={discount_pct}% entregue={delivered}"
    )
    return alert
