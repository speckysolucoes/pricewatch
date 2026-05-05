# ════════════════════════════════════════════════════════════
# Makefile — Comandos de Operação do Price Monitor Bot
#
# USO: make <comando>
# ════════════════════════════════════════════════════════════

.PHONY: help dev prod worker test lint clean logs backup

# Exibe ajuda
help:
	@echo ""
	@echo "Price Monitor Bot — Comandos disponíveis:"
	@echo ""
	@echo "  DESENVOLVIMENTO:"
	@echo "    make dev          → sobe API localmente (sem Docker)"
	@echo "    make worker-dev   → sobe worker local (sem Docker)"
	@echo "    make test         → roda testes"
	@echo "    make lint         → verifica código"
	@echo ""
	@echo "  DOCKER:"
	@echo "    make up           → sobe todos os serviços"
	@echo "    make down         → para todos os serviços"
	@echo "    make build        → rebuilda imagens"
	@echo "    make scale        → escala workers de scraping"
	@echo ""
	@echo "  MONITORAMENTO:"
	@echo "    make logs         → logs de todos os serviços"
	@echo "    make status       → status dos contêineres"
	@echo "    make health       → health check da API"
	@echo ""
	@echo "  MANUTENÇÃO:"
	@echo "    make backup       → backup do banco de dados"
	@echo "    make shell        → shell no contêiner da API"
	@echo "    make db-shell     → shell SQLite do banco"
	@echo ""

# ── Desenvolvimento local (sem Docker) ───────────────────
dev:
	@echo "Iniciando API em modo desenvolvimento..."
	USE_MOCK=true uvicorn main:app --reload --host 0.0.0.0 --port 8000

worker-dev:
	@echo "Iniciando worker local (requer Redis)..."
	celery -A app.worker.celery_app worker --loglevel=info --concurrency=2

beat-dev:
	@echo "Iniciando Celery Beat local..."
	celery -A app.worker.celery_app beat --loglevel=info

flower-dev:
	@echo "Iniciando Flower (monitor) local..."
	celery -A app.worker.celery_app flower --port=5555

# ── Docker ────────────────────────────────────────────────
up:
	docker compose up -d
	@echo "✅ Serviços iniciados"
	@echo "   Frontend: http://localhost/app"
	@echo "   API Docs: http://localhost/docs"
	@echo "   Flower:   http://localhost:5555"

down:
	docker compose down
	@echo "✅ Serviços parados"

build:
	docker compose build --no-cache

restart:
	docker compose restart

# Escala workers de scraping para lidar com mais produtos
# Ex: make scale N=5
scale:
	docker compose up -d --scale worker-scraping=${N:-3}
	@echo "✅ worker-scraping escalado para ${N:-3} instâncias"

# ── Monitoramento ─────────────────────────────────────────
logs:
	docker compose logs -f --tail=100

logs-api:
	docker compose logs -f api --tail=100

logs-workers:
	docker compose logs -f worker-high worker-scraping worker-notifications worker-maintenance --tail=50

logs-beat:
	docker compose logs -f beat --tail=100

status:
	docker compose ps

health:
	@curl -s http://localhost/ | python3 -m json.tool
	@echo ""
	@curl -s http://localhost/monitor/health | python3 -m json.tool

# Verifica tasks pendentes nas filas
queues:
	@docker compose exec redis redis-cli llen high_priority
	@docker compose exec redis redis-cli llen scraping
	@docker compose exec redis redis-cli llen notifications
	@docker compose exec redis redis-cli llen maintenance

# ── Manutenção ────────────────────────────────────────────
backup:
	@mkdir -p backups
	@cp price_monitor.db backups/price_monitor_$(shell date +%Y%m%d_%H%M%S).db
	@echo "✅ Backup criado em backups/"
	@ls -lh backups/*.db | tail -5

shell:
	docker compose exec api bash

db-shell:
	sqlite3 price_monitor.db

# Força execução de tasks de manutenção
expire-plans:
	docker compose exec worker-maintenance celery -A app.worker.celery_app call \
		app.worker.tasks_maintenance.expire_plans

cleanup:
	docker compose exec worker-maintenance celery -A app.worker.celery_app call \
		app.worker.tasks_maintenance.cleanup_old_history

# ── Testes e Qualidade ────────────────────────────────────
test:
	USE_MOCK=true pytest tests/ -v --tb=short

test-fast:
	USE_MOCK=true pytest tests/ -x -q

lint:
	@command -v ruff >/dev/null && ruff check app/ || echo "ruff não instalado (pip install ruff)"
	@command -v mypy >/dev/null && mypy app/ --ignore-missing-imports || echo "mypy não instalado"

# Instala dependências de desenvolvimento
install-dev:
	pip install -r requirements.txt
	pip install ruff mypy pytest pytest-asyncio

# ── Produção ──────────────────────────────────────────────
deploy-staging:
	./deploy/deploy.sh staging

deploy-prod:
	./deploy/deploy.sh production

# SSL com Let's Encrypt
ssl:
	@echo "Configurando SSL com Certbot..."
	docker run --rm -v ./deploy/ssl:/etc/letsencrypt certbot/certbot \
		certonly --standalone -d ${DOMAIN} --email ${EMAIL} --agree-tos -n
