"""
scheduler.py — Scheduler Fase 2

Novidade: adiciona job de detecção de promoções separado.
Jobs:
  1. price_check      → verifica preços (a cada X min)
  2. promotion_check  → analisa promoções (a cada Y min)
"""

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import settings
from app.services.monitor import run_price_check_cycle

logger = logging.getLogger(__name__)
_scheduler: AsyncIOScheduler = None


async def _run_promotion_check():
    """Job de detecção de promoções para o canal."""
    from app.db.database import SessionLocal
    from app.telegram.promotions import detect_and_publish_promotions
    db = SessionLocal()
    try:
        await detect_and_publish_promotions(db)
    finally:
        db.close()


def start_scheduler():
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")

    # Job 1: verificação de preços
    _scheduler.add_job(
        func=run_price_check_cycle,
        trigger=IntervalTrigger(minutes=settings.CHECK_INTERVAL_MINUTES),
        id="price_check",
        name="Verificação de Preços",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Job 2: canal de promoções (roda 5 min após o check de preços)
    promotion_interval = max(settings.CHECK_INTERVAL_MINUTES, 60)  # mínimo 1h
    _scheduler.add_job(
        func=_run_promotion_check,
        trigger=IntervalTrigger(minutes=promotion_interval),
        id="promotion_check",
        name="Canal de Promoções",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Job 3: expiração de planos (diário às 3h)
    _scheduler.add_job(
        func=_run_expire_plans,
        trigger='cron',
        hour=3, minute=0,
        id="expire_plans",
        name="Expirar Planos Vencidos",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info(
        f"✅ Scheduler iniciado | "
        f"Preços: {settings.CHECK_INTERVAL_MINUTES}min | "
        f"Promoções: {promotion_interval}min"
    )


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("🛑 Scheduler parado")


def get_scheduler_status() -> dict:
    if not _scheduler:
        return {"running": False, "jobs": []}
    jobs = [
        {"id": j.id, "name": j.name, "next_run": str(j.next_run_time)}
        for j in _scheduler.get_jobs()
    ]
    return {
        "running": _scheduler.running,
        "jobs": jobs,
        "check_interval_minutes": settings.CHECK_INTERVAL_MINUTES,
    }


async def _run_expire_plans():
    """Job diário: expira planos premium vencidos."""
    from app.db.database import SessionLocal
    from app.payments.subscriptions import expire_overdue_plans
    db = SessionLocal()
    try:
        count = expire_overdue_plans(db)
        if count:
            import logging
            logging.getLogger(__name__).info(f"📅 {count} planos expirados")
    finally:
        db.close()
