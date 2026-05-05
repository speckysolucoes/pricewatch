"""
billing.py — Endpoints de Cobrança e Assinaturas

GET  /billing/plans            → lista planos disponíveis (público)
POST /billing/checkout         → inicia checkout (requer auth)
GET  /billing/status           → status da assinatura do usuário
POST /billing/webhook/mp       → webhook Mercado Pago
POST /billing/webhook/stripe   → webhook Stripe
GET  /billing/mock-checkout    → página de checkout simulado (desenvolvimento)
POST /billing/cancel           → cancela assinatura (requer auth)
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.plans import plans_comparison, get_user_plan
from app.db.database import get_db
from app.models.models import User
from app.payments.gateway import get_gateway, PaymentEvent
from app.payments.subscriptions import (
    activate_premium, deactivate_premium,
    get_subscription_status, can_add_product
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["Cobrança"])


# ─────────────────────────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    plan_id: str = "premium"


class CheckoutResponse(BaseModel):
    checkout_url: str
    session_id: str
    expires_at: str
    provider: str
    amount_brl: float


# ─────────────────────────────────────────────────────────────
# ENDPOINTS PÚBLICOS
# ─────────────────────────────────────────────────────────────

@router.get(
    "/plans",
    summary="Listar planos disponíveis",
    description="Retorna todos os planos com preços e features. Público — não requer autenticação.",
)
async def list_plans():
    """Lista planos para exibir na página de preços."""
    return {
        "plans": plans_comparison(),
        "currency": "BRL",
        "active_gateway": _detect_active_gateway(),
    }


def _detect_active_gateway() -> str:
    """Detecta qual gateway está configurado."""
    if settings.MERCADOPAGO_ACCESS_TOKEN:
        return "mercadopago"
    if settings.STRIPE_SECRET_KEY:
        return "stripe"
    return "mock"


# ─────────────────────────────────────────────────────────────
# ENDPOINTS AUTENTICADOS
# ─────────────────────────────────────────────────────────────

@router.post(
    "/checkout",
    response_model=CheckoutResponse,
    summary="Iniciar checkout de plano",
)
async def create_checkout(
    data: CheckoutRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Cria sessão de checkout e retorna URL para o usuário pagar.

    O frontend redireciona o usuário para checkout_url.
    Após o pagamento, o gateway chama o webhook que ativa o plano.
    """
    if data.plan_id not in ["premium"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Plano '{data.plan_id}' inválido.",
        )

    if current_user.plan == data.plan_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Você já possui o plano {data.plan_id}.",
        )

    gateway = get_gateway()

    success_url = f"{settings.BASE_URL}/app?upgrade=success"
    cancel_url = f"{settings.BASE_URL}/app?upgrade=cancelled"

    try:
        session = await gateway.create_checkout(
            user_id=current_user.id,
            user_email=current_user.email,
            plan_id=data.plan_id,
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except Exception as e:
        logger.error(f"Erro ao criar checkout para user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Erro ao conectar com gateway de pagamento: {str(e)}",
        )

    logger.info(
        f"Checkout criado: user={current_user.id} "
        f"plan={data.plan_id} session={session.session_id}"
    )

    return CheckoutResponse(
        checkout_url=session.checkout_url,
        session_id=session.session_id,
        expires_at=session.expires_at.isoformat(),
        provider=session.provider,
        amount_brl=session.amount_brl,
    )


@router.get(
    "/status",
    summary="Status da assinatura do usuário logado",
)
async def get_billing_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retorna status completo da assinatura e limites do plano atual."""
    status_data = get_subscription_status(current_user)

    # Adiciona uso atual
    from app.models.models import Product
    product_count = db.query(Product).filter(Product.user_id == current_user.id).count()
    status_data["current_product_count"] = product_count
    status_data["can_add_product"] = can_add_product(db, current_user)[0]

    return status_data


@router.post(
    "/cancel",
    summary="Cancelar assinatura",
)
async def cancel_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Cancela o plano premium.
    O usuário volta para Free imediatamente.
    Produtos acima do limite são pausados.
    """
    if current_user.plan != "premium":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Você não possui plano premium ativo.",
        )

    deactivate_premium(db, current_user.id, reason="user_cancelled")

    return {
        "message": "Assinatura cancelada. Você foi movido para o plano gratuito.",
        "plan": "free",
        "products_paused": "Produtos acima do limite foram pausados.",
    }


# ─────────────────────────────────────────────────────────────
# WEBHOOKS
# ─────────────────────────────────────────────────────────────

async def _process_payment_event(event: PaymentEvent, db: Session):
    """
    Processa um evento de pagamento normalizado.
    Chamado por ambos os webhooks (MP e Stripe).
    """
    logger.info(
        f"[Webhook] Evento: {event.event_type} | "
        f"user={event.user_id} | payment={event.payment_id}"
    )

    if event.event_type == "payment.approved" and event.user_id:
        try:
            user = activate_premium(db, event.user_id, months=1)
            logger.info(f"✅ Premium ativado via webhook para user {event.user_id}")
        except Exception as e:
            logger.error(f"❌ Erro ao ativar premium para user {event.user_id}: {e}")

    elif event.event_type in ("payment.cancelled", "payment.failed") and event.user_id:
        logger.info(f"Pagamento cancelado/falhou para user {event.user_id} — sem ação")

    else:
        logger.debug(f"Evento ignorado: {event.event_type}")


@router.post(
    "/webhook/mp",
    summary="Webhook Mercado Pago",
    include_in_schema=False,   # não expor no Swagger
)
async def webhook_mercadopago(
    request: Request,
    db: Session = Depends(get_db),
):
    """Recebe e processa notificações do Mercado Pago."""
    body = await request.body()
    signature = request.headers.get("x-signature", "")

    gateway = get_gateway()

    # Valida autenticidade do webhook
    if not await gateway.validate_webhook(body, signature):
        logger.warning("[MP] Webhook com assinatura inválida — rejeitado")
        raise HTTPException(status_code=401, detail="Assinatura inválida")

    payload = await request.json()
    event = await gateway.parse_webhook(payload)

    if event:
        await _process_payment_event(event, db)

    return {"status": "ok"}


@router.post(
    "/webhook/stripe",
    summary="Webhook Stripe",
    include_in_schema=False,
)
async def webhook_stripe(
    request: Request,
    db: Session = Depends(get_db),
):
    """Recebe e processa notificações do Stripe."""
    body = await request.body()
    signature = request.headers.get("stripe-signature", "")

    gateway = get_gateway()

    if not await gateway.validate_webhook(body, signature):
        logger.warning("[Stripe] Webhook com assinatura inválida — rejeitado")
        raise HTTPException(status_code=401, detail="Assinatura inválida")

    payload = await request.json()
    event = await gateway.parse_webhook(payload)

    if event:
        await _process_payment_event(event, db)

    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────
# MOCK CHECKOUT — só para desenvolvimento
# ─────────────────────────────────────────────────────────────

@router.get(
    "/mock-checkout",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def mock_checkout_page(
    session_id: str,
    user_id: int,
    plan_id: str,
    success_url: str,
    db: Session = Depends(get_db),
):
    """
    Página de checkout simulado para desenvolvimento.
    Exibe botões "Pagar" e "Cancelar" — sem processar dinheiro real.
    """
    user = db.query(User).filter(User.id == user_id).first()
    user_name = user.name if user else f"Usuário {user_id}"
    price = settings.PLAN_PREMIUM_PRICE_BRL / 100

    confirm_url = f"/billing/mock-pay?session_id={session_id}&user_id={user_id}&plan_id={plan_id}&success_url={success_url}"

    return HTMLResponse(f"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Checkout — PriceWatch</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #0a0a0f;
    color: #e8e8f0;
    min-height: 100vh;
    display: grid;
    place-items: center;
    padding: 20px;
  }}
  .card {{
    background: #111118;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 20px;
    padding: 40px;
    width: 420px;
    max-width: 100%;
    text-align: center;
  }}
  .badge {{
    display: inline-block;
    background: rgba(255,127,0,0.15);
    color: #ff9500;
    border: 1px solid rgba(255,127,0,0.3);
    padding: 6px 14px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 24px;
  }}
  h1 {{ font-size: 24px; margin-bottom: 8px; }}
  .sub {{ color: #7070a0; font-size: 14px; margin-bottom: 32px; }}
  .plan-box {{
    background: #1a1a24;
    border-radius: 12px;
    padding: 24px;
    margin-bottom: 24px;
    border: 1px solid rgba(255,255,255,0.06);
  }}
  .plan-name {{ font-size: 18px; font-weight: 700; color: #ffd166; margin-bottom: 8px; }}
  .plan-price {{ font-size: 36px; font-weight: 800; color: #00d97e; }}
  .plan-price span {{ font-size: 16px; color: #7070a0; font-weight: 400; }}
  .plan-desc {{ font-size: 13px; color: #7070a0; margin-top: 8px; }}
  .user-info {{ font-size: 13px; color: #7070a0; margin-bottom: 28px; }}
  .user-info strong {{ color: #e8e8f0; }}
  .btn {{
    display: block;
    width: 100%;
    padding: 14px;
    border-radius: 10px;
    border: none;
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
    text-decoration: none;
    transition: all 0.2s;
    margin-bottom: 10px;
  }}
  .btn-pay {{ background: #6c5ce7; color: white; }}
  .btn-pay:hover {{ background: #7d6ef0; transform: translateY(-1px); }}
  .btn-cancel {{
    background: transparent;
    color: #7070a0;
    border: 1px solid rgba(255,255,255,0.08);
  }}
  .btn-cancel:hover {{ color: #e8e8f0; }}
  .mock-note {{
    font-size: 11px;
    color: #3a3a5a;
    margin-top: 16px;
  }}
</style>
</head>
<body>
  <div class="card">
    <div class="badge">⚠️ Ambiente de Desenvolvimento</div>
    <h1>Finalizar Pagamento</h1>
    <div class="sub">Checkout simulado — nenhum dado real será processado</div>

    <div class="plan-box">
      <div class="plan-name">✨ Plano {plan_id.capitalize()}</div>
      <div class="plan-price">R$ {price:.2f} <span>/mês</span></div>
      <div class="plan-desc">Produtos ilimitados · Alertas prioritários · Histórico de 1 ano</div>
    </div>

    <div class="user-info">
      Comprando para: <strong>{user_name}</strong>
    </div>

    <a href="{confirm_url}" class="btn btn-pay">💳 Confirmar Pagamento</a>
    <a href="/app" class="btn btn-cancel">Cancelar</a>

    <div class="mock-note">
      Este é um checkout simulado para desenvolvimento.<br>
      Configure MERCADOPAGO_ACCESS_TOKEN ou STRIPE_SECRET_KEY no .env para pagamentos reais.
    </div>
  </div>
</body>
</html>
""")


@router.get(
    "/mock-pay",
    include_in_schema=False,
)
async def mock_confirm_payment(
    session_id: str,
    user_id: int,
    plan_id: str,
    success_url: str,
    db: Session = Depends(get_db),
):
    """Simula confirmação do pagamento e ativa o plano."""
    try:
        user = activate_premium(db, user_id, months=1)
        logger.info(f"[Mock] Pagamento confirmado e premium ativado para user {user_id}")
    except Exception as e:
        logger.error(f"[Mock] Erro: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return RedirectResponse(url=f"{success_url}&activated=true")
