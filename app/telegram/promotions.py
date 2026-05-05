"""
promotions.py — Canal de Promoções Automático

Detecta quando um produto tem uma promoção GENUINAMENTE boa
e publica automaticamente em um canal Telegram.

O QUE É UMA BOA PROMOÇÃO?
  Não basta o preço cair — precisa ser um desconto real.
  Usamos múltiplos critérios:

  1. Desconto mínimo de 15% vs preço anterior
  2. Preço atual é menor ou igual ao menor preço histórico
  3. Produto não foi anunciado nas últimas 24h (evita spam)
  4. Disponibilidade confirmada (produto em estoque)

  Quanto mais critérios atendidos, maior o "score" da promoção.
  Publicamos apenas promoções com score suficiente.

BÔNUS — Como melhorar no futuro:
  - Comparar com preço médio dos últimos 30/90 dias
  - Integrar com histórico de preços do Buscapé/Zoom
  - Usar ML para detectar padrões de Black Friday fake
  - Score baseado em avaliações do produto (nota + nº de reviews)
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, List
from dataclasses import dataclass

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.db.database import SessionLocal
from app.models.models import Product, Alert, AlertType, PriceHistory, ProductStatus
from app.core.config import settings
from app.telegram.messages import msg_alert

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# SCORING DE PROMOÇÕES
# ─────────────────────────────────────────────────────────────

@dataclass
class PromotionScore:
    """
    Pontuação de uma promoção.
    Determina se vale publicar no canal.
    """
    product: Product
    current_price: float
    previous_price: Optional[float]
    discount_percentage: float
    score: float             # 0.0 a 1.0
    reasons: List[str]       # motivos que aumentaram o score
    should_publish: bool     # publicar no canal?


# Thresholds configuráveis
MIN_DISCOUNT_PCT = 15.0       # desconto mínimo para considerar
MIN_SCORE_TO_PUBLISH = 0.5    # score mínimo para publicar
COOLDOWN_HOURS = 24           # não publicar o mesmo produto em X horas


def calculate_promotion_score(
    db: Session,
    product: Product,
    current_price: float,
    previous_price: Optional[float],
) -> PromotionScore:
    """
    Calcula o score de qualidade de uma promoção.

    Critérios e pesos:
      +0.3 → desconto ≥ MIN_DISCOUNT_PCT%
      +0.2 → desconto ≥ 25%
      +0.2 → desconto ≥ 40% (oferta explosiva)
      +0.2 → é o menor preço histórico
      +0.1 → produto com histórico longo (mais confiável)
      -0.5 → já publicado nas últimas COOLDOWN_HOURS horas (anti-spam)
    """
    score = 0.0
    reasons = []

    if not previous_price or previous_price <= 0:
        return PromotionScore(
            product=product,
            current_price=current_price,
            previous_price=previous_price,
            discount_percentage=0.0,
            score=0.0,
            reasons=["Sem preço anterior para comparar"],
            should_publish=False,
        )

    discount_pct = (1 - current_price / previous_price) * 100

    # ── Critério 1: desconto mínimo ─────────────────────────
    if discount_pct >= MIN_DISCOUNT_PCT:
        score += 0.30
        reasons.append(f"Desconto de {discount_pct:.1f}%")
    else:
        # Sem desconto mínimo → não publica
        return PromotionScore(
            product=product,
            current_price=current_price,
            previous_price=previous_price,
            discount_percentage=discount_pct,
            score=0.0,
            reasons=[f"Desconto de apenas {discount_pct:.1f}% (mínimo: {MIN_DISCOUNT_PCT}%)"],
            should_publish=False,
        )

    # ── Critério 2: desconto generoso ───────────────────────
    if discount_pct >= 25:
        score += 0.20
        reasons.append("Desconto generoso (≥25%)")

    if discount_pct >= 40:
        score += 0.20
        reasons.append("Oferta explosiva (≥40%)")

    # ── Critério 3: menor preço histórico ───────────────────
    if product.lowest_price and current_price <= product.lowest_price:
        score += 0.20
        reasons.append("Menor preço já registrado")

    # ── Critério 4: histórico longo (produto mais confiável) ─
    history_count = (
        db.query(func.count(PriceHistory.id))
        .filter(PriceHistory.product_id == product.id)
        .scalar()
    )
    if history_count and history_count >= 10:
        score += 0.10
        reasons.append(f"Produto com {history_count} verificações históricas")

    # ── Penalidade: já publicado recentemente ────────────────
    cooldown_threshold = datetime.utcnow() - timedelta(hours=COOLDOWN_HOURS)
    recent_alert = (
        db.query(Alert)
        .filter(
            Alert.product_id == product.id,
            Alert.alert_type == AlertType.PROMOTION,
            Alert.sent_at >= cooldown_threshold,
        )
        .first()
    )
    if recent_alert:
        score -= 0.50
        reasons.append(f"⚠️ Já publicado há menos de {COOLDOWN_HOURS}h (anti-spam)")

    should_publish = score >= MIN_SCORE_TO_PUBLISH

    return PromotionScore(
        product=product,
        current_price=current_price,
        previous_price=previous_price,
        discount_percentage=discount_pct,
        score=min(1.0, max(0.0, score)),
        reasons=reasons,
        should_publish=should_publish,
    )


# ─────────────────────────────────────────────────────────────
# PUBLICAÇÃO NO CANAL
# ─────────────────────────────────────────────────────────────

async def publish_to_channel(
    product: Product,
    current_price: float,
    previous_price: float,
    discount_pct: float,
) -> bool:
    """
    Publica promoção no canal Telegram.

    Returns:
        True se publicou com sucesso
    """
    if not settings.TELEGRAM_CHANNEL_ID or not settings.TELEGRAM_BOT_TOKEN:
        logger.info("[Canal] Telegram não configurado — apenas logando promoção")
        _log_promotion(product, current_price, previous_price, discount_pct)
        return False

    from app.telegram.bot import get_bot_application
    from telegram.constants import ParseMode

    app = get_bot_application()
    if not app:
        logger.warning("[Canal] Bot Telegram não inicializado")
        _log_promotion(product, current_price, previous_price, discount_pct)
        return False

    try:
        message = msg_alert(
            product=product,
            current_price=current_price,
            previous_price=previous_price,
            alert_type=AlertType.PROMOTION,
        )

        await app.bot.send_message(
            chat_id=settings.TELEGRAM_CHANNEL_ID,
            text=message,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=False,  # mostra preview do produto
        )

        logger.info(
            f"📢 Promoção publicada no canal: "
            f"'{product.name[:40]}' "
            f"R${previous_price:.2f} → R${current_price:.2f} "
            f"(-{discount_pct:.1f}%)"
        )
        return True

    except Exception as e:
        logger.error(f"Erro ao publicar no canal: {e}")
        return False


def _log_promotion(product, current_price, previous_price, discount_pct):
    """Log de promoção quando canal não está configurado."""
    print("\n" + "🔥" * 20)
    print(f"PROMOÇÃO DETECTADA: {product.name[:50]}")
    print(f"De: R$ {previous_price:.2f} → Por: R$ {current_price:.2f} (-{discount_pct:.1f}%)")
    print(f"Link: {product.url_affiliate or product.url}")
    print("🔥" * 20 + "\n")


# ─────────────────────────────────────────────────────────────
# ANÁLISE DE TODOS OS PRODUTOS ATIVOS
# ─────────────────────────────────────────────────────────────

async def detect_and_publish_promotions(db: Session) -> dict:
    """
    Analisa todos os produtos com queda de preço recente
    e publica os que atingirem o score mínimo.

    Chamado:
      - No ciclo de monitoramento (após verificar preços)
      - Pelo scheduler separado de promoções
    """
    stats = {
        "analyzed": 0,
        "published": 0,
        "skipped_score": 0,
        "skipped_cooldown": 0,
        "errors": 0,
    }

    # Busca produtos ativos com preço atual e anterior
    products = (
        db.query(Product)
        .filter(
            Product.status.in_([ProductStatus.ACTIVE, ProductStatus.ALERTED]),
            Product.current_price.isnot(None),
            Product.initial_price.isnot(None),
        )
        .all()
    )

    logger.info(f"[Canal] Analisando {len(products)} produtos para promoções...")

    for product in products:
        try:
            stats["analyzed"] += 1

            # Usa preço inicial como referência (mais estável que "anterior")
            reference_price = product.initial_price

            promo = calculate_promotion_score(
                db=db,
                product=product,
                current_price=product.current_price,
                previous_price=reference_price,
            )

            logger.debug(
                f"[{product.id}] {product.name[:30]}: "
                f"score={promo.score:.2f} | {' | '.join(promo.reasons)}"
            )

            if not promo.should_publish:
                if "anti-spam" in " ".join(promo.reasons):
                    stats["skipped_cooldown"] += 1
                else:
                    stats["skipped_score"] += 1
                continue

            # Publica no canal
            published = await publish_to_channel(
                product=product,
                current_price=product.current_price,
                previous_price=reference_price,
                discount_pct=promo.discount_percentage,
            )

            if published:
                # Registra o alerta para controle de cooldown
                alert = Alert(
                    product_id=product.id,
                    alert_type=AlertType.PROMOTION,
                    price_at_alert=product.current_price,
                    previous_price=reference_price,
                    discount_percentage=promo.discount_percentage,
                    delivered=True,
                )
                db.add(alert)
                db.commit()
                stats["published"] += 1

        except Exception as e:
            logger.error(f"Erro ao processar promoção produto [{product.id}]: {e}")
            stats["errors"] += 1

    logger.info(f"[Canal] Resultado: {stats}")
    return stats
