"""
celery_app.py — Configuração do Celery

ARQUITETURA DE FILAS:
  high_priority  → usuários Premium (verificados primeiro)
  default        → usuários Free
  alerts         → envio de notificações (separado para não bloquear scraping)
  reports        → tarefas longas (exportação CSV, relatórios)

POR QUÊ CELERY EM VEZ DE APSCHEDULER?
  APScheduler (Fases 1-4):
    ✅ Simples, zero infra extra
    ❌ Roda dentro do processo da API (mata os dois juntos)
    ❌ Não distribui entre múltiplos servidores
    ❌ Sem retry automático de tasks falhas
    ❌ Sem monitoramento de tasks

  Celery (Fase 5+):
    ✅ Workers independentes (API e workers sobem separado)
    ✅ Distribui tasks entre N máquinas
    ✅ Retry automático com backoff exponencial
    ✅ Flower: dashboard visual de tasks e workers
    ✅ Prioridade de filas (Premium antes de Free)
    ✅ Dead letter queue (tasks que falharam após X retries)
    ❌ Requer Redis (ou RabbitMQ) como broker

COMO RODAR:
  # Terminal 1 — API
  uvicorn main:app --reload

  # Terminal 2 — Worker scraping (prioridade alta + normal)
  celery -A app.worker.celery_app worker \
    --queues=high_priority,default \
    --concurrency=4 \
    --loglevel=info \
    --hostname=scraper@%h

  # Terminal 3 — Worker alertas
  celery -A app.worker.celery_app worker \
    --queues=alerts \
    --concurrency=2 \
    --loglevel=info \
    --hostname=alerts@%h

  # Terminal 4 — Beat (scheduler distribuído)
  celery -A app.worker.celery_app beat \
    --loglevel=info

  # Terminal 5 — Flower (dashboard — opcional)
  celery -A app.worker.celery_app flower \
    --port=5555
  # Acesse: http://localhost:5555
"""

import os
from celery import Celery
from celery.schedules import crontab
from kombu import Queue, Exchange

from app.core.config import settings

# ─────────────────────────────────────────────────────────────
# APLICAÇÃO CELERY
# ─────────────────────────────────────────────────────────────

celery_app = Celery(
    "price_monitor",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.worker.tasks_scraping",
        "app.worker.tasks_alerts",
        "app.worker.tasks_reports",
    ],
)

# ─────────────────────────────────────────────────────────────
# CONFIGURAÇÃO DE FILAS
# ─────────────────────────────────────────────────────────────

# Exchanges (categorias de roteamento)
default_exchange = Exchange("default", type="direct")
priority_exchange = Exchange("priority", type="direct")

celery_app.conf.task_queues = (
    # Fila de alta prioridade — Premium users
    Queue(
        "high_priority",
        exchange=priority_exchange,
        routing_key="high_priority",
        queue_arguments={"x-max-priority": 10},  # suporte a prioridade por mensagem
    ),
    # Fila padrão — Free users
    Queue(
        "default",
        exchange=default_exchange,
        routing_key="default",
    ),
    # Fila de alertas — separada para não bloquear scraping
    Queue(
        "alerts",
        exchange=default_exchange,
        routing_key="alerts",
    ),
    # Fila de relatórios — tarefas longas
    Queue(
        "reports",
        exchange=default_exchange,
        routing_key="reports",
    ),
)

# Fila padrão para tasks sem roteamento explícito
celery_app.conf.task_default_queue = "default"
celery_app.conf.task_default_exchange = "default"
celery_app.conf.task_default_routing_key = "default"

# ─────────────────────────────────────────────────────────────
# ROTEAMENTO DE TASKS
# ─────────────────────────────────────────────────────────────

celery_app.conf.task_routes = {
    # Scraping vai para high_priority ou default (decidido em runtime)
    "app.worker.tasks_scraping.check_product_price": {"queue": "default"},
    "app.worker.tasks_scraping.run_monitoring_cycle": {"queue": "default"},

    # Alertas sempre em fila separada
    "app.worker.tasks_alerts.send_price_alert": {"queue": "alerts"},
    "app.worker.tasks_alerts.publish_promotion": {"queue": "alerts"},

    # Relatórios e manutenção em fila própria
    "app.worker.tasks_reports.export_user_history_csv": {"queue": "reports"},
    "app.worker.tasks_reports.expire_plans_task": {"queue": "reports"},
    "app.worker.tasks_reports.cleanup_old_history": {"queue": "reports"},
}

# ─────────────────────────────────────────────────────────────
# CONFIGURAÇÕES DE PERFORMANCE
# ─────────────────────────────────────────────────────────────

celery_app.conf.update(
    # Serialização
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="America/Sao_Paulo",
    enable_utc=True,

    # Performance
    worker_prefetch_multiplier=1,   # não pré-busca tasks — garante distribuição justa
    task_acks_late=True,            # confirma task só após completar (não perder tasks se worker cair)
    worker_max_tasks_per_child=100, # reinicia worker após 100 tasks (evita memory leaks)
    worker_max_memory_per_child=200_000,  # reinicia se usar mais que 200MB

    # Resultado
    result_expires=3600,            # limpa resultados após 1h
    task_ignore_result=False,       # guarda resultado para debug

    # Retry padrão para todas as tasks
    task_acks_on_failure_or_timeout=True,

    # Compressão
    task_compression="gzip",        # comprime payloads grandes
    result_compression="gzip",
)

# ─────────────────────────────────────────────────────────────
# BEAT SCHEDULE — agendamento distribuído
# ─────────────────────────────────────────────────────────────
# Substitui o APScheduler da Fase 4.
# Celery Beat distribui as tasks entre workers disponíveis.

celery_app.conf.beat_schedule = {
    # Ciclo principal de verificação de preços
    "monitoring-cycle": {
        "task": "app.worker.tasks_scraping.run_monitoring_cycle",
        "schedule": settings.CHECK_INTERVAL_MINUTES * 60,  # em segundos
        "options": {"queue": "default"},
    },

    # Canal de promoções — a cada hora
    "promotion-channel": {
        "task": "app.worker.tasks_alerts.run_promotion_detection",
        "schedule": crontab(minute=0),   # todo início de hora
        "options": {"queue": "alerts"},
    },

    # Expirar planos vencidos — diariamente às 3h
    "expire-plans": {
        "task": "app.worker.tasks_reports.expire_plans_task",
        "schedule": crontab(hour=3, minute=0),
        "options": {"queue": "reports"},
    },

    # Limpar histórico antigo — semanalmente (domingo às 4h)
    "cleanup-history": {
        "task": "app.worker.tasks_reports.cleanup_old_history",
        "schedule": crontab(hour=4, minute=0, day_of_week=0),
        "options": {"queue": "reports"},
    },

    # Health check do sistema — a cada 5 minutos
    "system-health": {
        "task": "app.worker.tasks_reports.system_health_check",
        "schedule": 300,   # 5 minutos
        "options": {"queue": "reports"},
    },
}
