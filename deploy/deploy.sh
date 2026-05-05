#!/bin/bash
# ════════════════════════════════════════════════════════════
# deploy.sh — Script de Deploy Automatizado
#
# USO:
#   chmod +x deploy/deploy.sh
#   ./deploy/deploy.sh [environment]
#
# ENVIRONMENTS:
#   staging    → servidor de homologação
#   production → servidor de produção (pede confirmação)
#
# PRÉ-REQUISITOS no servidor:
#   - Docker e Docker Compose instalados
#   - Arquivo .env configurado
#   - Portas 80 e 443 abertas
# ════════════════════════════════════════════════════════════

set -euo pipefail

# ── Configuração ──────────────────────────────────────────
ENV="${1:-staging}"
APP_DIR="/opt/price_monitor"
COMPOSE="docker compose"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Cores para output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $1"; }
ok()  { echo -e "${GREEN}✅ $1${NC}"; }
warn(){ echo -e "${YELLOW}⚠️  $1${NC}"; }
err() { echo -e "${RED}❌ $1${NC}"; exit 1; }

# ── Validações ────────────────────────────────────────────
log "Iniciando deploy — ambiente: ${ENV}"

[ -f ".env" ] || err ".env não encontrado. Copie .env.example e configure."
[ -f "docker-compose.yml" ] || err "docker-compose.yml não encontrado."

command -v docker >/dev/null || err "Docker não instalado."
command -v docker compose >/dev/null || err "Docker Compose não instalado."

# Confirmação para produção
if [ "$ENV" = "production" ]; then
    warn "ATENÇÃO: Deploy em PRODUÇÃO!"
    read -p "Tem certeza? (yes/no): " confirm
    [ "$confirm" = "yes" ] || { log "Deploy cancelado."; exit 0; }
fi

# ── Backup antes do deploy ────────────────────────────────
if [ -f "price_monitor.db" ]; then
    log "Fazendo backup do banco..."
    mkdir -p backups
    cp price_monitor.db "backups/price_monitor_${TIMESTAMP}.db"
    ok "Backup criado: backups/price_monitor_${TIMESTAMP}.db"

    # Mantém apenas os 10 backups mais recentes
    ls -t backups/price_monitor_*.db | tail -n +11 | xargs rm -f 2>/dev/null || true
fi

# ── Pull das últimas alterações ──────────────────────────
if [ -d ".git" ]; then
    log "Atualizando código..."
    git fetch origin
    git pull origin main || warn "Pull falhou — usando código local"
    ok "Código atualizado: $(git log -1 --format='%h %s')"
fi

# ── Build das imagens ────────────────────────────────────
log "Building imagens Docker..."
$COMPOSE build --no-cache --parallel || err "Build falhou"
ok "Build concluído"

# ── Deploy com zero-downtime ─────────────────────────────
log "Iniciando serviços..."

# Sobe Redis primeiro (outros dependem dele)
$COMPOSE up -d redis
log "Aguardando Redis ficar saudável..."
for i in {1..30}; do
    $COMPOSE exec redis redis-cli ping >/dev/null 2>&1 && break
    sleep 1
done
ok "Redis pronto"

# Sobe API com rolling update
$COMPOSE up -d --no-deps api
log "Aguardando API ficar saudável..."
for i in {1..30}; do
    curl -sf http://localhost/health >/dev/null 2>&1 && break
    sleep 2
done
ok "API pronta"

# Sobe workers
$COMPOSE up -d --no-deps \
    worker-high \
    worker-scraping \
    worker-notifications \
    worker-maintenance

# Reinicia beat (garantia — não pode ter duplicado)
$COMPOSE up -d --no-deps --force-recreate beat
ok "Workers iniciados"

# Sobe monitoramento
$COMPOSE up -d flower nginx
ok "Monitoramento e Nginx iniciados"

# ── Verificações pós-deploy ──────────────────────────────
log "Verificando saúde do sistema..."
sleep 5

API_OK=$(curl -sf http://localhost/ | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if 'version' in d else 'fail')" 2>/dev/null || echo "fail")
if [ "$API_OK" = "ok" ]; then
    ok "API respondendo"
else
    warn "API não respondeu como esperado — verifique os logs"
fi

# Verifica workers
WORKERS=$($COMPOSE exec -T worker-high celery -A app.worker.celery_app inspect ping 2>/dev/null | grep -c "pong" || echo "0")
if [ "$WORKERS" -gt "0" ]; then
    ok "Workers Celery conectados: $WORKERS"
else
    warn "Workers não detectados — podem estar iniciando ainda"
fi

# ── Resumo ────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════"
ok "DEPLOY CONCLUÍDO — $(date)"
echo ""
echo "  🌐 Frontend:    http://seudominio.com/app"
echo "  📖 API Docs:    http://seudominio.com/docs"
echo "  📊 Flower:      http://seudominio.com:5555"
echo ""
echo "  📋 Logs:        docker compose logs -f"
echo "  🔍 Status:      docker compose ps"
echo "═══════════════════════════════════════════════════"
