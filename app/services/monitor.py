"""
monitor.py — Motor de Monitoramento de Preços

É o CORAÇÃO do sistema. Responsável por:
  1. Buscar todos os produtos ativos no banco
  2. Verificar o preço atual de cada um
  3. Comparar com preço alvo e histórico
  4. Disparar alertas quando necessário
  5. Atualizar banco de dados

FLUXO DE VERIFICAÇÃO:
  Para cada produto ativo:
    → check_price() → PriceResult
    → salvar em price_history
    → atualizar current_price e lowest_price
    → verificar se deve alertar:
        • target_price: preço <= alvo? → alerta
        • alert_percentage: queda >= %? → alerta
    → se deve alertar → send_alert()
"""

import asyncio
import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.models.models import Product, PriceHistory, ProductStatus, AlertType
from app.services.price_checker import check_price
from app.services.notifier import send_alert

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# VERIFICAÇÃO DE UM PRODUTO
# ─────────────────────────────────────────────────────────────

async def check_product(db: Session, product: Product) -> dict:
    """
    Verifica o preço de um único produto e processa alertas.

    Returns:
        dict com resultado da verificação
    """
    logger.info(f"Verificando produto [{product.id}]: {product.name[:50]}")

    # ── 1. Verificar preço ──────────────────────────────────
    result = await check_price(url=product.url, store=product.store)

    # Atualiza timestamp independente de sucesso ou falha
    product.last_checked_at = datetime.utcnow()

    # ── 2. Registrar no histórico ──────────────────────────
    history_entry = PriceHistory(
        product_id=product.id,
        price=result.price if result.success else 0,
        is_available=result.success,
        error_message=result.error if not result.success else None,
    )
    db.add(history_entry)

    # ── 3. Tratar falha ────────────────────────────────────
    if not result.success:
        product.status = ProductStatus.ERROR
        db.commit()
        logger.warning(f"Falha ao verificar produto [{product.id}]: {result.error}")
        return {
            "product_id": product.id,
            "success": False,
            "error": result.error,
        }

    current_price = result.price
    previous_price = product.current_price

    # ── 4. Atualizar nome se vier do scraper ───────────────
    if result.product_name and result.product_name != product.name:
        product.name = result.product_name

    # ── 5. Definir preço inicial ───────────────────────────
    if product.initial_price is None:
        product.initial_price = current_price

    # ── 6. Atualizar preço atual ───────────────────────────
    product.current_price = current_price

    # ── 7. Atualizar menor preço histórico ─────────────────
    if product.lowest_price is None or current_price < product.lowest_price:
        product.lowest_price = current_price

    # Status volta para ativo se estava em erro
    if product.status == ProductStatus.ERROR:
        product.status = ProductStatus.ACTIVE

    # ── 8. Verificar condições de alerta ───────────────────
    should_alert = False
    alert_type = None

    # Condição 1: preço alvo atingido
    if (
        product.target_price is not None
        and current_price <= product.target_price
        and product.status == ProductStatus.ACTIVE
    ):
        should_alert = True
        alert_type = AlertType.PRICE_DROP
        logger.info(
            f"Produto [{product.id}]: preço alvo atingido! "
            f"R${current_price} <= R${product.target_price}"
        )

    # Condição 2: queda percentual atingida
    elif (
        product.alert_percentage is not None
        and previous_price is not None
        and previous_price > 0
    ):
        actual_drop = (1 - current_price / previous_price) * 100
        if actual_drop >= product.alert_percentage:
            should_alert = True
            alert_type = AlertType.PERCENTAGE_DROP
            logger.info(
                f"Produto [{product.id}]: queda de {actual_drop:.1f}% "
                f"(alvo: {product.alert_percentage}%)"
            )

    # ── 9. Disparar alerta se necessário ──────────────────
    if should_alert and alert_type:
        await send_alert(
            db=db,
            product=product,
            current_price=current_price,
            alert_type=alert_type,
            previous_price=previous_price,
        )

        # Marca como alertado (não alerta de novo até o preço subir e baixar)
        # Para reativar: usuário pode resetar via API
        product.status = ProductStatus.ALERTED

    # ── 10. Salvar tudo no banco ───────────────────────────
    db.commit()
    db.refresh(product)

    return {
        "product_id": product.id,
        "success": True,
        "previous_price": previous_price,
        "current_price": current_price,
        "alerted": should_alert,
    }


# ─────────────────────────────────────────────────────────────
# VERIFICAÇÃO DE TODOS OS PRODUTOS
# ─────────────────────────────────────────────────────────────

async def run_price_check_cycle() -> dict:
    """
    Roda um ciclo completo de verificação de preços.

    Chamado pelo scheduler a cada X minutos.

    Processo:
      1. Abre sessão de banco
      2. Busca todos os produtos ativos
      3. Verifica cada um (com controle de concorrência)
      4. Fecha sessão
      5. Retorna resumo do ciclo

    CONCORRÊNCIA:
      asyncio.gather() verifica múltiplos produtos em paralelo.
      Limite de 5 simultâneos para não sobrecarregar os sites.
    """
    db = SessionLocal()
    start_time = datetime.utcnow()

    logger.info("=" * 50)
    logger.info(f"🔄 Iniciando ciclo de verificação: {start_time}")

    try:
        # Busca apenas produtos ATIVOS (não pausados, não com erro permanente)
        active_products: List[Product] = (
            db.query(Product)
            .filter(Product.status == ProductStatus.ACTIVE)
            .all()
        )

        total = len(active_products)
        logger.info(f"📦 Produtos para verificar: {total}")

        if total == 0:
            logger.info("Nenhum produto ativo. Aguardando próximo ciclo.")
            return {"total": 0, "success": 0, "errors": 0, "alerts": 0}

        # ── Verificação com limite de concorrência ──────────
        # Semáforo: máximo 5 verificações simultâneas
        # Evita banimento por excesso de requests
        semaphore = asyncio.Semaphore(5)

        async def check_with_semaphore(product):
            async with semaphore:
                return await check_product(db, product)

        # Executa todas as verificações em paralelo (respeitando o limite)
        results = await asyncio.gather(
            *[check_with_semaphore(p) for p in active_products],
            return_exceptions=True,  # não cancela tudo se um falhar
        )

        # ── Contabiliza resultados ─────────────────────────
        success_count = sum(1 for r in results if isinstance(r, dict) and r.get("success"))
        error_count = sum(1 for r in results if isinstance(r, dict) and not r.get("success"))
        alert_count = sum(1 for r in results if isinstance(r, dict) and r.get("alerted"))
        exception_count = sum(1 for r in results if isinstance(r, Exception))

        elapsed = (datetime.utcnow() - start_time).total_seconds()

        summary = {
            "total": total,
            "success": success_count,
            "errors": error_count + exception_count,
            "alerts": alert_count,
            "elapsed_seconds": round(elapsed, 2),
        }

        logger.info(f"✅ Ciclo completo em {elapsed:.1f}s: {summary}")
        logger.info("=" * 50)

        return summary

    except Exception as e:
        logger.error(f"❌ Erro crítico no ciclo de monitoramento: {e}", exc_info=True)
        return {"error": str(e), "total": 0, "success": 0, "errors": 1, "alerts": 0}

    finally:
        db.close()


# ─────────────────────────────────────────────────────────────
# UTILITÁRIOS DO MONITOR
# ─────────────────────────────────────────────────────────────

def reactivate_product(db: Session, product_id: int) -> Optional[Product]:
    """
    Reativa um produto que foi alertado ou pausado.

    Permite monitorar novamente após preço subir e baixar.
    """
    product = db.query(Product).filter(Product.id == product_id).first()

    if product and product.status in [ProductStatus.ALERTED, ProductStatus.PAUSED]:
        product.status = ProductStatus.ACTIVE
        db.commit()
        db.refresh(product)
        logger.info(f"Produto [{product_id}] reativado")

    return product
