"""
gateway.py — Abstração de Gateway de Pagamento

Define a interface comum para qualquer gateway.
O resto do sistema só chama PaymentGateway — não sabe qual provider está por baixo.

PROVIDERS IMPLEMENTADOS:
  MercadoPagoGateway → pagamentos BR (Pix, cartão, boleto)
  StripeGateway      → pagamentos globais (cartão internacional)
  MockGateway        → testes (sem dinheiro real)

COMO ESCOLHER O GATEWAY:
  get_gateway() → retorna o provider ativo baseado nas configurações do .env
    - MERCADOPAGO_ACCESS_TOKEN configurado → usa Mercado Pago
    - STRIPE_SECRET_KEY configurado        → usa Stripe
    - Nenhum configurado                   → usa Mock (desenvolvimento)

FLUXO DE PAGAMENTO:
  1. Frontend → POST /billing/checkout     → recebe checkout_url
  2. Usuário  → acessa checkout_url        → paga
  3. Provider → POST /billing/webhook      → confirma pagamento
  4. Backend  → ativa plano premium        → notifica usuário
"""

import hashlib
import hmac
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# DATA CLASSES COMPARTILHADOS
# ─────────────────────────────────────────────────────────────

@dataclass
class CheckoutSession:
    """
    Sessão de checkout criada pelo gateway.
    Enviada ao frontend para redirecionar o usuário.
    """
    session_id: str           # ID único da sessão no gateway
    checkout_url: str         # URL onde o usuário paga
    expires_at: datetime      # quando a sessão expira
    provider: str             # "mercadopago" | "stripe" | "mock"
    amount_brl: float         # valor em reais (para exibição)


@dataclass
class PaymentEvent:
    """
    Evento recebido via webhook do gateway.
    Representa um pagamento confirmado, cancelado, etc.
    """
    event_id: str
    event_type: str           # "payment.approved" | "subscription.cancelled" | etc.
    user_id: Optional[int]    # quem pagou (extraído dos metadados)
    payment_id: str           # ID do pagamento no gateway
    amount: Optional[float]   # valor pago
    currency: str             # "BRL" | "USD"
    provider: str
    raw_data: dict            # payload bruto do webhook


# ─────────────────────────────────────────────────────────────
# INTERFACE BASE
# ─────────────────────────────────────────────────────────────

class PaymentGateway(ABC):
    """Interface que todo gateway deve implementar."""

    @abstractmethod
    async def create_checkout(
        self,
        user_id: int,
        user_email: str,
        plan_id: str,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutSession:
        """Cria sessão de checkout e retorna URL para redirecionar o usuário."""
        ...

    @abstractmethod
    async def validate_webhook(self, payload: bytes, signature: str) -> bool:
        """Valida que o webhook veio realmente do gateway (anti-fraude)."""
        ...

    @abstractmethod
    async def parse_webhook(self, payload: dict) -> Optional[PaymentEvent]:
        """Extrai dados do payload do webhook."""
        ...

    @abstractmethod
    async def cancel_subscription(self, subscription_id: str) -> bool:
        """Cancela assinatura recorrente."""
        ...


# ─────────────────────────────────────────────────────────────
# MERCADO PAGO
# ─────────────────────────────────────────────────────────────

class MercadoPagoGateway(PaymentGateway):
    """
    Integração com Mercado Pago.

    Usa a API de Preferências para criar checkout.
    Suporta: Pix, cartão de crédito/débito, boleto.

    Documentação: https://www.mercadopago.com.br/developers
    """

    BASE_URL = "https://api.mercadopago.com"

    def __init__(self):
        self.token = settings.MERCADOPAGO_ACCESS_TOKEN
        self.webhook_secret = settings.MERCADOPAGO_WEBHOOK_SECRET
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-Idempotency-Key": "",   # preenchido por request
        }

    async def create_checkout(
        self,
        user_id: int,
        user_email: str,
        plan_id: str,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutSession:
        """
        Cria preferência de pagamento no Mercado Pago.

        A preferência define:
          - O que está sendo comprado (plano Premium)
          - Quanto custa
          - Para onde redirecionar após pagamento
          - Metadados (user_id) para identificar no webhook
        """
        import uuid
        idempotency_key = str(uuid.uuid4())

        payload = {
            "items": [
                {
                    "id": f"plan_{plan_id}",
                    "title": f"PriceWatch {plan_id.capitalize()} — 1 mês",
                    "description": "Monitoramento de preços ilimitado",
                    "quantity": 1,
                    "currency_id": "BRL",
                    "unit_price": settings.PLAN_PREMIUM_PRICE_BRL / 100,
                }
            ],
            "payer": {"email": user_email},
            "back_urls": {
                "success": success_url,
                "failure": cancel_url,
                "pending": success_url,
            },
            "auto_return": "approved",
            "external_reference": str(user_id),   # identifica o usuário no webhook
            "metadata": {
                "user_id": user_id,
                "plan_id": plan_id,
            },
            "statement_descriptor": "PRICEWATCH",
            "expires": True,
            "expiration_date_to": (
                datetime.utcnow() + timedelta(hours=24)
            ).strftime("%Y-%m-%dT%H:%M:%S.000-03:00"),
        }

        headers = {**self.headers, "X-Idempotency-Key": idempotency_key}

        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.BASE_URL}/checkout/preferences",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        logger.info(f"[MP] Preferência criada: {data['id']} para user {user_id}")

        # Em produção: data["init_point"]
        # Em sandbox: data["sandbox_init_point"]
        checkout_url = data.get("sandbox_init_point", data.get("init_point"))

        return CheckoutSession(
            session_id=data["id"],
            checkout_url=checkout_url,
            expires_at=datetime.utcnow() + timedelta(hours=24),
            provider="mercadopago",
            amount_brl=settings.PLAN_PREMIUM_PRICE_BRL / 100,
        )

    async def validate_webhook(self, payload: bytes, signature: str) -> bool:
        """
        Valida assinatura do webhook do Mercado Pago.
        Usa HMAC-SHA256 com o webhook secret.
        """
        if not self.webhook_secret:
            logger.warning("[MP] Webhook secret não configurado — aceitando sem validar")
            return True

        try:
            # O MP envia: x-signature: ts=...,v1=...
            parts = dict(part.split("=", 1) for part in signature.split(","))
            ts = parts.get("ts", "")
            v1 = parts.get("v1", "")

            # Mensagem para validar: "id:{id};request-id:{request_id};ts:{ts};"
            # Simplificado: valida apenas o v1 contra o payload
            expected = hmac.new(
                self.webhook_secret.encode(),
                f"{ts}".encode() + payload,
                hashlib.sha256,
            ).hexdigest()

            return hmac.compare_digest(v1, expected)

        except Exception as e:
            logger.error(f"[MP] Erro na validação do webhook: {e}")
            return False

    async def parse_webhook(self, payload: dict) -> Optional[PaymentEvent]:
        """
        Processa webhook do Mercado Pago.

        Tipos de evento relevantes:
          payment.created  → pagamento iniciado
          payment.updated  → status mudou (approved, rejected, etc.)
        """
        event_type = payload.get("action", "")
        data = payload.get("data", {})
        payment_id = str(data.get("id", ""))

        if not payment_id:
            return None

        # Busca detalhes do pagamento para pegar os metadados
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.BASE_URL}/v1/payments/{payment_id}",
                    headers=self.headers,
                )
                resp.raise_for_status()
                payment = resp.json()
        except Exception as e:
            logger.error(f"[MP] Erro ao buscar pagamento {payment_id}: {e}")
            return None

        status = payment.get("status", "")
        user_id = payment.get("external_reference")   # = user_id que passamos

        # Normaliza tipo de evento
        if status == "approved":
            normalized_type = "payment.approved"
        elif status in ("cancelled", "refunded", "charged_back"):
            normalized_type = "payment.cancelled"
        else:
            normalized_type = f"payment.{status}"

        return PaymentEvent(
            event_id=f"mp_{payment_id}",
            event_type=normalized_type,
            user_id=int(user_id) if user_id else None,
            payment_id=payment_id,
            amount=payment.get("transaction_amount"),
            currency="BRL",
            provider="mercadopago",
            raw_data=payment,
        )

    async def cancel_subscription(self, subscription_id: str) -> bool:
        """Cancela assinatura recorrente do Mercado Pago."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.put(
                    f"{self.BASE_URL}/preapproval/{subscription_id}",
                    json={"status": "cancelled"},
                    headers=self.headers,
                )
                return resp.status_code == 200
        except Exception as e:
            logger.error(f"[MP] Erro ao cancelar assinatura {subscription_id}: {e}")
            return False


# ─────────────────────────────────────────────────────────────
# STRIPE
# ─────────────────────────────────────────────────────────────

class StripeGateway(PaymentGateway):
    """
    Integração com Stripe.

    Usa Stripe Checkout Sessions (hosted).
    Suporta: cartão de crédito/débito internacional.

    Documentação: https://stripe.com/docs/api
    """

    async def create_checkout(
        self,
        user_id: int,
        user_email: str,
        plan_id: str,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutSession:
        import stripe
        stripe.api_key = settings.STRIPE_SECRET_KEY

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": f"PriceWatch {plan_id.capitalize()}",
                            "description": "Monitoramento de preços ilimitado por 1 mês",
                        },
                        "unit_amount": settings.PLAN_PREMIUM_PRICE_USD,
                        "recurring": {"interval": "month"},
                    },
                    "quantity": 1,
                }
            ],
            mode="subscription",
            customer_email=user_email,
            metadata={"user_id": str(user_id), "plan_id": plan_id},
            success_url=success_url + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=cancel_url,
        )

        logger.info(f"[Stripe] Session criada: {session.id} para user {user_id}")

        return CheckoutSession(
            session_id=session.id,
            checkout_url=session.url,
            expires_at=datetime.fromtimestamp(session.expires_at),
            provider="stripe",
            amount_brl=settings.PLAN_PREMIUM_PRICE_BRL / 100,
        )

    async def validate_webhook(self, payload: bytes, signature: str) -> bool:
        import stripe
        try:
            stripe.Webhook.construct_event(
                payload, signature, settings.STRIPE_WEBHOOK_SECRET
            )
            return True
        except stripe.error.SignatureVerificationError:
            return False

    async def parse_webhook(self, payload: dict) -> Optional[PaymentEvent]:
        event_type = payload.get("type", "")
        data = payload.get("data", {}).get("object", {})

        user_id = None
        meta = data.get("metadata", {})
        if meta.get("user_id"):
            user_id = int(meta["user_id"])

        normalized = {
            "checkout.session.completed": "payment.approved",
            "customer.subscription.deleted": "payment.cancelled",
            "invoice.payment_failed": "payment.failed",
        }.get(event_type, event_type)

        return PaymentEvent(
            event_id=payload.get("id", ""),
            event_type=normalized,
            user_id=user_id,
            payment_id=data.get("id", ""),
            amount=data.get("amount_total", 0) / 100 if data.get("amount_total") else None,
            currency=data.get("currency", "usd").upper(),
            provider="stripe",
            raw_data=payload,
        )

    async def cancel_subscription(self, subscription_id: str) -> bool:
        import stripe
        stripe.api_key = settings.STRIPE_SECRET_KEY
        try:
            stripe.Subscription.delete(subscription_id)
            return True
        except Exception as e:
            logger.error(f"[Stripe] Erro ao cancelar {subscription_id}: {e}")
            return False


# ─────────────────────────────────────────────────────────────
# MOCK — para desenvolvimento e testes
# ─────────────────────────────────────────────────────────────

class MockGateway(PaymentGateway):
    """
    Gateway simulado para desenvolvimento.
    Não processa dinheiro real.

    A URL de checkout aponta para /billing/mock-checkout
    que simula o pagamento e chama o webhook internamente.
    """

    async def create_checkout(
        self,
        user_id: int,
        user_email: str,
        plan_id: str,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutSession:
        import uuid
        session_id = f"mock_{uuid.uuid4().hex[:12]}"

        # URL da página mock no próprio backend
        checkout_url = (
            f"{settings.BASE_URL}/billing/mock-checkout"
            f"?session_id={session_id}"
            f"&user_id={user_id}"
            f"&plan_id={plan_id}"
            f"&success_url={success_url}"
        )

        logger.info(f"[Mock] Checkout criado: {session_id} para user {user_id}")

        return CheckoutSession(
            session_id=session_id,
            checkout_url=checkout_url,
            expires_at=datetime.utcnow() + timedelta(hours=1),
            provider="mock",
            amount_brl=settings.PLAN_PREMIUM_PRICE_BRL / 100,
        )

    async def validate_webhook(self, payload: bytes, signature: str) -> bool:
        return True   # mock sempre válido

    async def parse_webhook(self, payload: dict) -> Optional[PaymentEvent]:
        return PaymentEvent(
            event_id=payload.get("id", "mock_event"),
            event_type=payload.get("type", "payment.approved"),
            user_id=payload.get("user_id"),
            payment_id=payload.get("payment_id", "mock_payment"),
            amount=settings.PLAN_PREMIUM_PRICE_BRL / 100,
            currency="BRL",
            provider="mock",
            raw_data=payload,
        )

    async def cancel_subscription(self, subscription_id: str) -> bool:
        logger.info(f"[Mock] Assinatura {subscription_id} cancelada")
        return True


# ─────────────────────────────────────────────────────────────
# FACTORY — escolhe o gateway ativo
# ─────────────────────────────────────────────────────────────

def get_gateway() -> PaymentGateway:
    """
    Retorna o gateway de pagamento ativo baseado nas configurações.

    Prioridade:
      1. Mercado Pago (se token configurado)
      2. Stripe (se chave configurada)
      3. Mock (desenvolvimento — sem dinheiro real)
    """
    if settings.MERCADOPAGO_ACCESS_TOKEN:
        logger.debug("Gateway: Mercado Pago")
        return MercadoPagoGateway()

    if settings.STRIPE_SECRET_KEY:
        logger.debug("Gateway: Stripe")
        return StripeGateway()

    logger.info("⚠️  Gateway: Mock (configure MERCADOPAGO ou STRIPE no .env)")
    return MockGateway()
