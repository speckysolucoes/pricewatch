"""
subscriptions.py — Serviço de Assinaturas e Planos

Responsável por toda a lógica de negócio de planos:
  - Ativar premium após pagamento confirmado
  - Verificar se usuário pode adicionar mais produtos
  - Expirar planos vencidos
  - Histórico de pagamentos

É o único lugar que modifica o plano do usuário.
O endpoint de webhook chama activate_premium() — mais nada.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.orm import Session

from app.models.models import User, Product
from app.core.plans import get_plan, PLANS

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# ATIVAÇÃO DE PREMIUM
# ─────────────────────────────────────────────────────────────

def activate_premium(db: Session, user_id: int, months: int = 1) -> User:
    """
    Ativa (ou renova) o plano Premium para um usuário.

    Chamado após pagamento confirmado via webhook.

    Lógica:
      - Se já tem premium ativo: adiciona os meses ao tempo restante
      - Se não tem ou expirou: começa agora + N meses

    Args:
        db: sessão do banco
        user_id: ID do usuário
        months: quantos meses adquiridos (padrão 1)

    Returns:
        User atualizado
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ValueError(f"Usuário {user_id} não encontrado")

    now = datetime.utcnow()

    # Calcula data de expiração
    if user.plan == "premium" and user.plan_expires_at and user.plan_expires_at > now:
        # Renova a partir da data atual de expiração
        new_expires = user.plan_expires_at + timedelta(days=30 * months)
    else:
        # Novo premium a partir de agora
        new_expires = now + timedelta(days=30 * months)

    user.plan = "premium"
    user.plan_expires_at = new_expires
    db.commit()
    db.refresh(user)

    logger.info(
        f"✅ Premium ativado: user={user_id} "
        f"({user.email}) até {new_expires.strftime('%d/%m/%Y')}"
    )

    # Notifica o usuário
    _notify_activation(user, new_expires)

    return user


def deactivate_premium(db: Session, user_id: int, reason: str = "cancelled") -> User:
    """
    Remove o plano Premium (cancela ou expira).

    O usuário volta para Free — sem deletar dados.
    Produtos acima do limite free ficam pausados automaticamente.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ValueError(f"Usuário {user_id} não encontrado")

    old_plan = user.plan
    user.plan = "free"
    user.plan_expires_at = None
    db.commit()

    # Pausa produtos acima do limite free
    free_plan = get_plan("free")
    products = (
        db.query(Product)
        .filter(Product.user_id == user_id)
        .order_by(Product.created_at.asc())
        .all()
    )

    paused_count = 0
    for i, product in enumerate(products):
        if i >= free_plan.max_products:
            from app.models.models import ProductStatus
            product.status = ProductStatus.PAUSED
            paused_count += 1

    if paused_count > 0:
        db.commit()
        logger.warning(
            f"⚠️ {paused_count} produtos pausados após downgrade "
            f"do user {user_id}"
        )

    logger.info(
        f"🔽 Premium removido: user={user_id} "
        f"({user.email}) — motivo: {reason}"
    )

    return user


# ─────────────────────────────────────────────────────────────
# VERIFICAÇÕES DE LIMITE
# ─────────────────────────────────────────────────────────────

def can_add_product(db: Session, user: User) -> tuple[bool, str]:
    """
    Verifica se usuário pode adicionar mais um produto.

    Returns:
        (True, "") se pode adicionar
        (False, "mensagem") se não pode
    """
    from app.core.plans import get_user_plan

    plan = get_user_plan(user)
    current_count = db.query(Product).filter(Product.user_id == user.id).count()

    if current_count >= plan.max_products:
        if plan.id == "free":
            return False, (
                f"Limite do plano gratuito atingido ({plan.max_products} produtos). "
                f"Faça upgrade para Premium e monitore produtos ilimitados!"
            )
        else:
            return False, f"Limite de {plan.max_products} produtos atingido."

    return True, ""


def get_subscription_status(user: User) -> dict:
    """
    Retorna status completo da assinatura do usuário.
    Usado pelo frontend para mostrar detalhes do plano.
    """
    from app.core.plans import get_user_plan, PLANS
    plan = get_user_plan(user)
    now = datetime.utcnow()

    expires_at = getattr(user, "plan_expires_at", None)
    days_remaining = None
    if expires_at and expires_at > now:
        days_remaining = (expires_at - now).days

    is_expired = (
        user.plan == "premium"
        and expires_at
        and expires_at < now
    )

    return {
        "current_plan": plan.id,
        "plan_name": plan.name,
        "is_premium": plan.id == "premium",
        "is_expired": is_expired,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "days_remaining": days_remaining,
        "max_products": plan.max_products,
        "price_brl": plan.price_brl,
        "price_usd": plan.price_usd,
        "features": {
            "check_interval": plan.max_check_interval,
            "history_days": plan.price_history_days,
            "telegram_alerts": plan.telegram_alerts,
            "promotion_channel": plan.promotion_channel,
            "priority_scraping": plan.priority_scraping,
            "csv_export": plan.csv_export,
            "api_access": plan.api_access,
        },
        "available_plans": list(PLANS.keys()),
    }


# ─────────────────────────────────────────────────────────────
# EXPIRAÇÃO AUTOMÁTICA (chamada pelo scheduler)
# ─────────────────────────────────────────────────────────────

def expire_overdue_plans(db: Session) -> int:
    """
    Verifica e expira planos premium vencidos.
    Chamado pelo scheduler diariamente.

    Returns:
        Número de planos expirados
    """
    now = datetime.utcnow()
    expired_users = (
        db.query(User)
        .filter(
            User.plan == "premium",
            User.plan_expires_at.isnot(None),
            User.plan_expires_at < now,
        )
        .all()
    )

    count = 0
    for user in expired_users:
        try:
            deactivate_premium(db, user.id, reason="expired")
            count += 1
        except Exception as e:
            logger.error(f"Erro ao expirar plano do user {user.id}: {e}")

    if count > 0:
        logger.info(f"📅 {count} planos expirados processados")

    return count


# ─────────────────────────────────────────────────────────────
# NOTIFICAÇÕES
# ─────────────────────────────────────────────────────────────

def _notify_activation(user: User, expires_at: datetime):
    """Notifica usuário sobre ativação do premium."""
    # Console (MVP)
    print(f"\n🎉 PREMIUM ATIVADO!")
    print(f"   Usuário: {user.name} ({user.email})")
    print(f"   Válido até: {expires_at.strftime('%d/%m/%Y')}\n")

    # Fase 2+: enviar via Telegram
    if user.telegram_id:
        logger.info(f"[TODO] Enviar confirmação premium via Telegram para {user.telegram_id}")
