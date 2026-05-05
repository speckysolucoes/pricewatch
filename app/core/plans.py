"""
plans.py — Definição dos Planos e seus Limites

DESIGN:
  Centralizar aqui a lógica de planos evita
  espalhar "if plan == 'free'" por todo o código.

  Para adicionar um novo plano (ex: "team"):
    1. Adicionar em PLANS
    2. O resto do código usa get_plan() — não muda nada mais
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timedelta


@dataclass
class Plan:
    """Define as capacidades e limites de um plano."""
    id: str
    name: str
    price_brl: float           # preço mensal em reais
    price_usd: float           # preço mensal em dólares
    description: str

    # Limites
    max_products: int          # máx de produtos monitorados
    max_check_interval: int    # intervalo mínimo de verificação (minutos)
    price_history_days: int    # quantos dias de histórico guardar

    # Features booleanas
    telegram_alerts: bool      # alertas via Telegram
    promotion_channel: bool    # canal de promoções
    priority_scraping: bool    # fila prioritária de scraping
    csv_export: bool           # exportação de dados
    api_access: bool           # acesso à API direta

    # Badges e marketing
    badge_color: str           # cor do badge no frontend
    badge_label: str           # texto do badge
    highlight: bool = False    # destaque na tela de planos


# ─────────────────────────────────────────────────────────────
# PLANOS DISPONÍVEIS
# ─────────────────────────────────────────────────────────────

PLANS: dict[str, Plan] = {
    "free": Plan(
        id="free",
        name="Gratuito",
        price_brl=0.0,
        price_usd=0.0,
        description="Comece a monitorar preços sem pagar nada",
        max_products=5,
        max_check_interval=60,          # verifica a cada 60min
        price_history_days=7,           # 7 dias de histórico
        telegram_alerts=True,           # alertas básicos
        promotion_channel=False,        # sem canal de promoções
        priority_scraping=False,
        csv_export=False,
        api_access=False,
        badge_color="#7070a0",
        badge_label="Free",
    ),
    "premium": Plan(
        id="premium",
        name="Premium",
        price_brl=19.0,
        price_usd=4.99,
        description="Monitoramento ilimitado com todos os recursos",
        max_products=9999,              # ilimitado
        max_check_interval=15,          # verifica a cada 15min
        price_history_days=365,         # 1 ano de histórico
        telegram_alerts=True,
        promotion_channel=True,         # canal de promoções ativo
        priority_scraping=True,         # fila prioritária
        csv_export=True,
        api_access=True,
        badge_color="#ffd166",
        badge_label="Premium",
        highlight=True,                 # destaque na UI
    ),
}


def get_plan(plan_id: str) -> Plan:
    """Retorna o plano pelo ID. Fallback para free se inválido."""
    return PLANS.get(plan_id, PLANS["free"])


def get_user_plan(user) -> Plan:
    """Retorna o plano ativo do usuário, verificando expiração."""
    plan_id = getattr(user, "plan", "free") or "free"

    # Verifica se plano premium expirou
    if plan_id == "premium":
        expires = getattr(user, "plan_expires_at", None)
        if expires and expires < datetime.utcnow():
            return PLANS["free"]   # expirou — volta para free

    return get_plan(plan_id)


def format_price(value_brl: float, value_usd: float) -> dict:
    """Formata preços para exibição."""
    return {
        "brl": f"R$ {value_brl:.2f}".replace(".", ","),
        "usd": f"US$ {value_usd:.2f}",
        "brl_raw": value_brl,
        "usd_raw": value_usd,
    }


def plans_comparison() -> list[dict]:
    """Retorna dados formatados para a tabela de comparação de planos."""
    result = []
    for plan in PLANS.values():
        result.append({
            "id": plan.id,
            "name": plan.name,
            "price": format_price(plan.price_brl, plan.price_usd),
            "description": plan.description,
            "highlight": plan.highlight,
            "badge_color": plan.badge_color,
            "badge_label": plan.badge_label,
            "features": {
                "max_products": "Ilimitado" if plan.max_products > 100 else str(plan.max_products),
                "check_interval": f"A cada {plan.max_check_interval}min",
                "history": f"{plan.price_history_days} dias",
                "telegram": plan.telegram_alerts,
                "promotions": plan.promotion_channel,
                "priority": plan.priority_scraping,
                "export": plan.csv_export,
                "api": plan.api_access,
            },
        })
    return result
