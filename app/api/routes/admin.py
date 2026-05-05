"""
admin.py — Endpoints de Administração e Operações

GET  /admin/worker/status   → status dos workers Celery
GET  /admin/worker/queues   → filas e tasks pendentes
POST /admin/worker/trigger  → dispara task manualmente
GET  /admin/metrics         → métricas do sistema
GET  /admin/export/csv      → exporta dados do usuário (Premium)
"""

import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
import io

from app.core.auth import get_current_user
from app.db.database import get_db
from app.models.models import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["Admin / Operações"])


def _get_celery_inspect():
    """Retorna objeto de inspeção do Celery. None se não disponível."""
    try:
        from app.worker.celery_app import celery_app
        return celery_app.control.inspect(timeout=2.0)
    except Exception:
        return None


@router.get(
    "/worker/status",
    summary="Status dos workers Celery",
)
async def worker_status(current_user: User = Depends(get_current_user)):
    """
    Retorna status de todos os workers conectados.
    Mostra quais filas cada worker está consumindo e load atual.
    """
    inspect = _get_celery_inspect()
    if not inspect:
        return {
            "status": "unavailable",
            "message": "Workers não conectados. Verifique se o Redis e os workers estão rodando.",
            "tip": "celery -A app.worker.celery_app worker --loglevel=info",
        }

    try:
        active = inspect.active() or {}
        registered = inspect.registered() or {}
        stats = inspect.stats() or {}

        workers = []
        for worker_name, worker_stats in stats.items():
            active_tasks = active.get(worker_name, [])
            workers.append({
                "name": worker_name,
                "status": "online",
                "active_tasks": len(active_tasks),
                "active_task_names": [t.get("name", "?") for t in active_tasks[:5]],
                "pool_processes": worker_stats.get("pool", {}).get("processes", []),
                "total_tasks_processed": worker_stats.get("total", {}),
            })

        return {
            "status": "ok" if workers else "no_workers",
            "worker_count": len(workers),
            "workers": workers,
            "timestamp": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        return {"status": "error", "detail": str(e)}


@router.get(
    "/worker/queues",
    summary="Status das filas Celery",
)
async def queue_status(current_user: User = Depends(get_current_user)):
    """
    Mostra quantas tasks estão pendentes em cada fila.
    Fila com muitas tasks = workers sobrecarregados.
    """
    try:
        from app.worker.celery_app import celery_app
        import redis as redis_lib
        from app.core.config import settings

        r = redis_lib.from_url(settings.REDIS_URL)
        queues = ["high_priority", "scraping", "notifications", "maintenance"]

        queue_info = []
        for q in queues:
            length = r.llen(q)
            queue_info.append({
                "name": q,
                "pending_tasks": length,
                "status": "ok" if length < 100 else "backlogged" if length < 500 else "critical",
            })

        return {
            "queues": queue_info,
            "redis_connected": True,
            "timestamp": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        return {
            "queues": [],
            "redis_connected": False,
            "error": str(e),
        }


@router.post(
    "/worker/trigger/{task_name}",
    summary="Disparar task manualmente",
)
async def trigger_task(
    task_name: str,
    current_user: User = Depends(get_current_user),
):
    """
    Dispara uma task Celery manualmente para debugging.

    Tasks disponíveis:
      - check_all_products
      - expire_plans
      - cleanup_old_history
      - system_health_check
      - publish_promotions
    """
    allowed_tasks = {
        "check_all_products": "app.worker.tasks_scraping.check_all_products",
        "expire_plans": "app.worker.tasks_maintenance.expire_plans",
        "cleanup_old_history": "app.worker.tasks_maintenance.cleanup_old_history",
        "system_health_check": "app.worker.tasks_maintenance.system_health_check",
        "publish_promotions": "app.worker.tasks_notifications.publish_promotions",
    }

    if task_name not in allowed_tasks:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Task inválida. Disponíveis: {list(allowed_tasks.keys())}",
        )

    try:
        from app.worker.celery_app import celery_app
        result = celery_app.send_task(allowed_tasks[task_name])
        return {
            "message": f"Task '{task_name}' disparada",
            "task_id": result.id,
            "status": "queued",
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Erro ao disparar task: {str(e)}. Redis está rodando?",
        )


@router.get(
    "/metrics",
    summary="Métricas do sistema",
)
async def get_metrics(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Métricas consolidadas do sistema para monitoramento."""
    from app.models.models import Product, User as UserModel, Alert, PriceHistory, ProductStatus
    from sqlalchemy import func
    from datetime import timedelta

    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)

    return {
        "timestamp": now.isoformat(),
        "users": {
            "total": db.query(func.count(UserModel.id)).scalar(),
            "active": db.query(func.count(UserModel.id)).filter(UserModel.is_active == True).scalar(),
            "premium": db.query(func.count(UserModel.id)).filter(UserModel.plan == "premium").scalar(),
        },
        "products": {
            "total": db.query(func.count(Product.id)).scalar(),
            "active": db.query(func.count(Product.id)).filter(Product.status == ProductStatus.ACTIVE).scalar(),
            "alerted": db.query(func.count(Product.id)).filter(Product.status == ProductStatus.ALERTED).scalar(),
            "error": db.query(func.count(Product.id)).filter(Product.status == ProductStatus.ERROR).scalar(),
        },
        "alerts": {
            "total": db.query(func.count(Alert.id)).scalar(),
            "last_7_days": db.query(func.count(Alert.id)).filter(Alert.sent_at >= week_ago).scalar(),
            "delivered": db.query(func.count(Alert.id)).filter(Alert.delivered == True).scalar(),
        },
        "price_checks": {
            "total": db.query(func.count(PriceHistory.id)).scalar(),
            "last_7_days": db.query(func.count(PriceHistory.id)).filter(PriceHistory.checked_at >= week_ago).scalar(),
        },
    }


@router.get(
    "/export/csv",
    summary="Exportar histórico completo como CSV (Premium)",
)
async def export_csv(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Exporta todo o histórico de preços do usuário como CSV.
    Disponível apenas para usuários Premium.
    """
    from app.core.plans import get_user_plan

    plan = get_user_plan(current_user)
    if not plan.csv_export:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Exportação CSV é exclusiva do plano Premium.",
        )

    from app.models.models import Product, PriceHistory
    import csv

    products = db.query(Product).filter(Product.user_id == current_user.id).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "produto_id", "produto_nome", "loja",
        "url_afiliado", "data_hora", "preco",
        "disponivel", "preco_alvo",
    ])

    for product in products:
        history = (
            db.query(PriceHistory)
            .filter(PriceHistory.product_id == product.id)
            .order_by(PriceHistory.checked_at.asc())
            .all()
        )
        for h in history:
            writer.writerow([
                product.id, product.name, product.store or "",
                product.url_affiliate or product.url,
                h.checked_at.strftime("%Y-%m-%d %H:%M:%S"),
                f"{h.price:.2f}",
                "sim" if h.is_available else "nao",
                f"{product.target_price:.2f}" if product.target_price else "",
            ])

    output.seek(0)
    filename = f"pricewatch_{current_user.id}_{datetime.utcnow().strftime('%Y%m%d')}.csv"

    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),   # utf-8-sig para Excel
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
