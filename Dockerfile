# ════════════════════════════════════════════════════════════
# Dockerfile — Price Monitor Bot
# Build multi-stage para imagem final menor e segura
# ════════════════════════════════════════════════════════════

# ── Stage 1: Builder ──────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Instala dependências de build
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copia e instala requirements
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: Runtime ──────────────────────────────────────
FROM python:3.12-slim AS runtime

# Instala apenas dependências de runtime (sem gcc)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Cria usuário não-root (segurança)
RUN useradd --create-home --shell /bin/bash app
WORKDIR /app

# Copia dependências instaladas do builder
COPY --from=builder /install /usr/local

# Copia código da aplicação
COPY --chown=app:app . .

# Cria diretórios necessários
RUN mkdir -p /app/logs /app/data && chown -R app:app /app

# Troca para usuário não-root
USER app

# Variáveis de ambiente padrão
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PORT=8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:${PORT}/ || exit 1

# Expõe porta da API
EXPOSE 8000

# Comando padrão: API
# Workers sobrescrevem isso no docker-compose.yml
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
