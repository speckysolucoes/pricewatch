"""
monitor.py — Endpoints de Monitoramento e Status

Rotas:
  GET  /monitor/health    → status geral do sistema
  POST /monitor/run       → rodar ciclo de verificação manualmente
  GET  /monitor/stats     → estatísticas gerais
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime

from app.db.database import get_db
from app.models.models import Product, User, Alert, ProductStatus
from app.services.scheduler import get_scheduler_status
from app.services.monitor import run_price_check_cycle

router = APIRouter(prefix="/monitor", tags=["Monitoramento"])


@router.get(
    "/health",
    summary="Status do sistema",
    description="Verifica se o sistema está funcionando corretamente.",
)
async def health_check(db: Session = Depends(get_db)):
    """Health check completo do sistema."""
    try:
        # Testa conexão com banco
        db.execute("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False

    scheduler_status = get_scheduler_status()

    return {
        "status": "ok" if db_ok else "degraded",
        "timestamp": datetime.utcnow().isoformat(),
        "database": "connected" if db_ok else "error",
        "scheduler": scheduler_status,
    }


@router.post(
    "/run",
    summary="Rodar verificação manual",
    description="Dispara um ciclo de verificação de preços imediatamente (sem esperar o scheduler).",
)
async def run_check_manually():
    """
    Força um ciclo de verificação agora.
    Útil para testes e debugging.
    """
    start = datetime.utcnow()
    result = await run_price_check_cycle()
    elapsed = (datetime.utcnow() - start).total_seconds()

    return {
        "message": "Ciclo de verificação concluído",
        "started_at": start.isoformat(),
        "elapsed_seconds": round(elapsed, 2),
        "result": result,
    }


@router.get(
    "/stats",
    summary="Estatísticas gerais do sistema",
)
async def get_stats(db: Session = Depends(get_db)):
    """Retorna estatísticas gerais para um mini-dashboard."""

    # Contagens por status
    status_counts = (
        db.query(Product.status, func.count(Product.id))
        .group_by(Product.status)
        .all()
    )

    # Total de alertas enviados
    total_alerts = db.query(func.count(Alert.id)).scalar()

    # Alertas entregues com sucesso
    delivered_alerts = db.query(func.count(Alert.id)).filter(Alert.delivered == True).scalar()

    return {
        "users": {
            "total": db.query(func.count(User.id)).scalar(),
            "active": db.query(func.count(User.id)).filter(User.is_active == True).scalar(),
        },
        "products": {
            "total": db.query(func.count(Product.id)).scalar(),
            "by_status": {str(s): c for s, c in status_counts},
        },
        "alerts": {
            "total": total_alerts,
            "delivered": delivered_alerts,
        },
        "generated_at": datetime.utcnow().isoformat(),
    }
