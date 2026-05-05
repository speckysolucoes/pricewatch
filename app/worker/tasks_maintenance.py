"""
tasks_maintenance.py — Tasks de Manutenção e Operações Periódicas

Tasks que rodam em background periodicamente:
  expire_plans          → expira planos premium vencidos (diário)
  cleanup_old_history   → remove histórico de preços antigo (semanal)
  system_health_check   → verifica saúde do sistema (5 em 5 min)
  reactivate_error_products → tenta reativar produtos em erro (hourly)
  export_user_csv       → exporta dados do usuário como CSV (on-demand)
"""

import csv
import io
import json
import logging
from datetime import datetime, timedelta

from celery.utils.log import get_task_logger
from app.worker.celery_app import celery_app

logger = get_task_logger(__name__)


# ─────────────────────────────────────────────────────────────
# EXPIRAR PLANOS VENCIDOS
# ─────────────────────────────────────────────────────────────

@celery_app.task(
    name="app.worker.tasks_maintenance.expire_plans",
    queue="maintenance",
    time_limit=120,
)
def expire_plans():
    """
    Expira planos premium vencidos e faz downgrade para free.
    Roda diariamente às 3h (configurado no beat_schedule).
    """
    from app.db.database import SessionLocal
    from app.payments.subscriptions import expire_overdue_plans

    db = SessionLocal()
    try:
        count = expire_overdue_plans(db)
        logger.info(f"[Manutenção] {count} planos expirados")
        return {"expired": count, "ran_at": datetime.utcnow().isoformat()}
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────
# LIMPEZA DE HISTÓRICO ANTIGO
# ─────────────────────────────────────────────────────────────

@celery_app.task(
    name="app.worker.tasks_maintenance.cleanup_old_history",
    queue="maintenance",
    time_limit=300,    # pode demorar em DBs grandes
)
def cleanup_old_history():
    """
    Remove registros antigos de price_history para economizar espaço.

    Política de retenção:
      - Usuários free:    mantém 7 dias
      - Usuários premium: mantém 365 dias

    Estratégia:
      Em vez de deletar linha a linha (lento), usa DELETE em lote
      com WHERE para aproveitar índices.
    """
    from app.db.database import SessionLocal
    from app.models.models import PriceHistory, Product, User
    from sqlalchemy import and_

    db = SessionLocal()
    total_deleted = 0

    try:
        # Processa por plano para aplicar limites diferentes
        for plan, days in [("free", 7), ("premium", 365)]:
            cutoff = datetime.utcnow() - timedelta(days=days)

            # Subquery: IDs dos produtos de usuários deste plano
            user_ids = db.query(User.id).filter(User.plan == plan).subquery()
            product_ids = (
                db.query(Product.id)
                .filter(Product.user_id.in_(user_ids))
                .subquery()
            )

            # Delete em lote
            deleted = (
                db.query(PriceHistory)
                .filter(
                    PriceHistory.product_id.in_(product_ids),
                    PriceHistory.checked_at < cutoff,
                )
                .delete(synchronize_session=False)
            )

            db.commit()
            total_deleted += deleted
            logger.info(f"[Limpeza] Plano {plan}: {deleted} registros removidos (cutoff: {days} dias)")

        return {
            "deleted": total_deleted,
            "ran_at": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        db.rollback()
        logger.error(f"[Limpeza] Erro: {e}")
        raise
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────
# HEALTH CHECK DO SISTEMA
# ─────────────────────────────────────────────────────────────

@celery_app.task(
    name="app.worker.tasks_maintenance.system_health_check",
    queue="maintenance",
    time_limit=30,
    ignore_result=False,   # armazena resultado para monitoramento
)
def system_health_check():
    """
    Verifica saúde de todos os componentes do sistema.
    Roda a cada 5 minutos.

    Retorna status de:
      - Banco de dados
      - Redis
      - Worker Celery (se chegou aqui, está ok)
      - Quantidade de produtos ativos
      - Produtos em erro (indica problema de scraping)
    """
    from app.db.database import SessionLocal
    from app.models.models import Product, User, ProductStatus
    from sqlalchemy import func

    status = {
        "timestamp": datetime.utcnow().isoformat(),
        "worker": "ok",
        "database": "unknown",
        "redis": "unknown",
        "metrics": {},
    }

    # Checa banco
    db = SessionLocal()
    try:
        db.execute("SELECT 1")
        status["database"] = "ok"

        # Coleta métricas
        total_products = db.query(func.count(Product.id)).scalar()
        active = db.query(func.count(Product.id)).filter(Product.status == ProductStatus.ACTIVE).scalar()
        errors = db.query(func.count(Product.id)).filter(Product.status == ProductStatus.ERROR).scalar()
        total_users = db.query(func.count(User.id)).filter(User.is_active == True).scalar()
        premium_users = db.query(func.count(User.id)).filter(User.plan == "premium", User.is_active == True).scalar()

        status["metrics"] = {
            "total_products": total_products,
            "active_products": active,
            "error_products": errors,
            "error_rate_pct": round((errors / total_products * 100) if total_products > 0 else 0, 1),
            "total_users": total_users,
            "premium_users": premium_users,
        }

        # Alerta se muitos produtos em erro
        if errors and total_products and (errors / total_products) > 0.20:
            logger.warning(
                f"⚠️ ALERTA: {errors}/{total_products} produtos em erro "
                f"({status['metrics']['error_rate_pct']}%) — possível problema no scraping"
            )

    except Exception as e:
        status["database"] = f"error: {str(e)}"
        logger.error(f"[HealthCheck] DB error: {e}")
    finally:
        db.close()

    # Checa Redis
    try:
        from app.core.config import settings
        import redis as redis_lib
        r = redis_lib.from_url(settings.REDIS_URL)
        r.ping()
        info = r.info("memory")
        status["redis"] = "ok"
        status["redis_memory_mb"] = round(info.get("used_memory", 0) / 1024 / 1024, 1)
    except Exception as e:
        status["redis"] = f"error: {str(e)}"
        logger.error(f"[HealthCheck] Redis error: {e}")

    log_level = logging.WARNING if "error" in str(status.values()) else logging.INFO
    logger.log(log_level, f"[HealthCheck] {status}")

    return status


# ─────────────────────────────────────────────────────────────
# REATIVAR PRODUTOS EM ERRO
# ─────────────────────────────────────────────────────────────

@celery_app.task(
    name="app.worker.tasks_maintenance.reactivate_error_products",
    queue="maintenance",
    time_limit=60,
)
def reactivate_error_products():
    """
    Reativa produtos que estão em estado ERROR há mais de 1 hora.

    Produtos ficam em ERROR quando o scraping falha 3x seguidas.
    Após 1h, tentamos novamente — o site pode ter voltado.
    """
    from app.db.database import SessionLocal
    from app.models.models import Product, ProductStatus

    db = SessionLocal()
    reactivated = 0

    try:
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)

        error_products = (
            db.query(Product)
            .filter(
                Product.status == ProductStatus.ERROR,
                Product.last_checked_at < one_hour_ago,
            )
            .all()
        )

        for product in error_products:
            product.status = ProductStatus.ACTIVE
            reactivated += 1

        if reactivated > 0:
            db.commit()
            logger.info(f"[Manutenção] {reactivated} produtos reativados após erro")

        return {"reactivated": reactivated}

    finally:
        db.close()


# ─────────────────────────────────────────────────────────────
# EXPORTAR CSV DE USUÁRIO (on-demand — Premium only)
# ─────────────────────────────────────────────────────────────

@celery_app.task(
    name="app.worker.tasks_maintenance.export_user_csv",
    queue="maintenance",
    time_limit=120,
    bind=True,
)
def export_user_csv(self, user_id: int) -> dict:
    """
    Gera CSV com todo o histórico de preços do usuário.
    Feature exclusiva do plano Premium.

    Retorna o CSV como string (o endpoint de API salva/envia).

    Formato do CSV:
      produto_id, produto_nome, loja, data, preco, disponivel
    """
    from app.db.database import SessionLocal
    from app.models.models import User, Product, PriceHistory
    from app.core.plans import get_user_plan

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return {"error": "Usuário não encontrado"}

        plan = get_user_plan(user)
        if not plan.csv_export:
            return {"error": "Exportação CSV disponível apenas no plano Premium"}

        # Busca todos os produtos e histórico
        products = db.query(Product).filter(Product.user_id == user_id).all()

        output = io.StringIO()
        writer = csv.writer(output)

        # Cabeçalho
        writer.writerow([
            "produto_id", "produto_nome", "loja",
            "url_afiliado", "data_hora", "preco",
            "disponivel", "preco_alvo",
        ])

        total_rows = 0
        for product in products:
            history = (
                db.query(PriceHistory)
                .filter(PriceHistory.product_id == product.id)
                .order_by(PriceHistory.checked_at.asc())
                .all()
            )
            for h in history:
                writer.writerow([
                    product.id,
                    product.name,
                    product.store or "",
                    product.url_affiliate or product.url,
                    h.checked_at.strftime("%Y-%m-%d %H:%M:%S"),
                    f"{h.price:.2f}",
                    "sim" if h.is_available else "nao",
                    f"{product.target_price:.2f}" if product.target_price else "",
                ])
                total_rows += 1

        csv_content = output.getvalue()

        logger.info(f"[Export] CSV gerado para user {user_id}: {total_rows} linhas")

        return {
            "user_id": user_id,
            "total_rows": total_rows,
            "total_products": len(products),
            "csv_content": csv_content,
            "generated_at": datetime.utcnow().isoformat(),
        }

    finally:
        db.close()
