"""
dashboard.py — Endpoints do Painel de Controle

Fornece dados consolidados para o frontend:
  GET /dashboard/stats        → resumo geral do usuário
  GET /dashboard/alerts       → alertas recentes
  GET /dashboard/price-history/{product_id} → histórico para gráfico
  GET /dashboard/savings      → economia total gerada pelos alertas
"""

from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from pydantic import BaseModel

from app.db.database import get_db
from app.models.models import User, Product, Alert, PriceHistory, ProductStatus, AlertType
from app.core.auth import get_current_user

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


# ─────────────────────────────────────────────────────────────
# SCHEMAS DE RESPOSTA
# ─────────────────────────────────────────────────────────────

class ProductSummary(BaseModel):
    id: int
    name: str
    store: Optional[str]
    current_price: Optional[float]
    initial_price: Optional[float]
    target_price: Optional[float]
    status: str
    discount_pct: Optional[float]
    url_affiliate: Optional[str]
    last_checked_at: Optional[datetime]

    class Config:
        from_attributes = True


class AlertSummary(BaseModel):
    id: int
    product_name: str
    product_id: int
    alert_type: str
    price_at_alert: float
    previous_price: Optional[float]
    discount_percentage: Optional[float]
    sent_at: datetime

    class Config:
        from_attributes = True


class PricePoint(BaseModel):
    price: float
    checked_at: datetime
    is_available: bool


class DashboardStats(BaseModel):
    # Produtos
    total_products: int
    active_products: int
    alerted_products: int
    paused_products: int
    error_products: int
    max_products: int           # limite do plano

    # Alertas
    total_alerts: int
    alerts_last_7_days: int

    # Economia calculada
    total_saved: float          # soma das economias quando alertas foram disparados
    avg_discount_pct: float     # desconto médio dos alertas

    # Plano
    plan: str
    plan_expires_at: Optional[datetime]

    # Dados recentes
    recent_alerts: List[AlertSummary]
    best_deals: List[ProductSummary]  # produtos com maior queda atual


# ─────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────

@router.get(
    "/stats",
    response_model=DashboardStats,
    summary="Resumo completo do painel",
)
async def get_dashboard_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Retorna todos os dados necessários para renderizar o dashboard.
    Um único endpoint para evitar múltiplos requests do frontend.
    """
    user_id = current_user.id

    # ── Contagem por status ─────────────────────────────────
    products = db.query(Product).filter(Product.user_id == user_id).all()
    total = len(products)

    status_counts = {}
    for p in products:
        s = str(p.status).split(".")[-1].lower()
        status_counts[s] = status_counts.get(s, 0) + 1

    # ── Alertas ─────────────────────────────────────────────
    product_ids = [p.id for p in products]

    total_alerts = 0
    alerts_last_7 = 0
    total_saved = 0.0
    total_discount_sum = 0.0
    discount_count = 0

    recent_alerts_data = []

    if product_ids:
        alerts = (
            db.query(Alert)
            .filter(Alert.product_id.in_(product_ids))
            .order_by(desc(Alert.sent_at))
            .all()
        )
        total_alerts = len(alerts)

        week_ago = datetime.utcnow() - timedelta(days=7)
        alerts_last_7 = sum(1 for a in alerts if a.sent_at >= week_ago)

        for a in alerts:
            if a.previous_price and a.price_at_alert and a.previous_price > a.price_at_alert:
                saved = a.previous_price - a.price_at_alert
                total_saved += saved
            if a.discount_percentage:
                total_discount_sum += a.discount_percentage
                discount_count += 1

        # 5 alertas mais recentes para o painel
        for a in alerts[:5]:
            product = db.query(Product).filter(Product.id == a.product_id).first()
            if product:
                recent_alerts_data.append(AlertSummary(
                    id=a.id,
                    product_name=product.name[:50],
                    product_id=a.product_id,
                    alert_type=str(a.alert_type).split(".")[-1],
                    price_at_alert=a.price_at_alert,
                    previous_price=a.previous_price,
                    discount_percentage=a.discount_percentage,
                    sent_at=a.sent_at,
                ))

    avg_discount = round(total_discount_sum / discount_count, 1) if discount_count else 0.0

    # ── Melhores oportunidades atuais ───────────────────────
    best_deals = []
    for p in products:
        if p.initial_price and p.current_price and p.initial_price > 0:
            disc = (1 - p.current_price / p.initial_price) * 100
            if disc > 0:
                best_deals.append((disc, p))

    best_deals.sort(key=lambda x: x[0], reverse=True)
    best_deals_formatted = []
    for disc, p in best_deals[:5]:
        best_deals_formatted.append(ProductSummary(
            id=p.id,
            name=p.name[:50],
            store=p.store,
            current_price=p.current_price,
            initial_price=p.initial_price,
            target_price=p.target_price,
            status=str(p.status).split(".")[-1].lower(),
            discount_pct=round(disc, 1),
            url_affiliate=p.url_affiliate,
            last_checked_at=p.last_checked_at,
        ))

    return DashboardStats(
        total_products=total,
        active_products=status_counts.get("active", 0),
        alerted_products=status_counts.get("alerted", 0),
        paused_products=status_counts.get("paused", 0),
        error_products=status_counts.get("error", 0),
        max_products=current_user.max_products,
        total_alerts=total_alerts,
        alerts_last_7_days=alerts_last_7,
        total_saved=round(total_saved, 2),
        avg_discount_pct=avg_discount,
        plan=current_user.plan,
        plan_expires_at=current_user.plan_expires_at,
        recent_alerts=recent_alerts_data,
        best_deals=best_deals_formatted,
    )


@router.get(
    "/price-history/{product_id}",
    response_model=List[PricePoint],
    summary="Histórico de preços para gráfico",
)
async def get_price_history(
    product_id: int,
    days: int = Query(30, ge=1, le=365, description="Quantos dias de histórico"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Retorna o histórico de preços de um produto.
    Usado pelo frontend para renderizar o gráfico de linha.

    - **days**: período em dias (padrão 30, máximo 365)
    """
    # Verifica que o produto pertence ao usuário
    product = db.query(Product).filter(
        Product.id == product_id,
        Product.user_id == current_user.id,
    ).first()

    if not product:
        raise HTTPException(status_code=404, detail="Produto não encontrado")

    since = datetime.utcnow() - timedelta(days=days)

    history = (
        db.query(PriceHistory)
        .filter(
            PriceHistory.product_id == product_id,
            PriceHistory.checked_at >= since,
            PriceHistory.is_available == True,
        )
        .order_by(PriceHistory.checked_at.asc())
        .all()
    )

    return [
        PricePoint(
            price=h.price,
            checked_at=h.checked_at,
            is_available=h.is_available,
        )
        for h in history
    ]


@router.get(
    "/alerts",
    response_model=List[AlertSummary],
    summary="Histórico de alertas do usuário",
)
async def get_user_alerts(
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lista os alertas mais recentes do usuário com dados do produto."""
    product_ids = [p.id for p in current_user.products]

    if not product_ids:
        return []

    alerts = (
        db.query(Alert)
        .filter(Alert.product_id.in_(product_ids))
        .order_by(desc(Alert.sent_at))
        .limit(limit)
        .all()
    )

    result = []
    for a in alerts:
        product = db.query(Product).filter(Product.id == a.product_id).first()
        if product:
            result.append(AlertSummary(
                id=a.id,
                product_name=product.name[:50],
                product_id=a.product_id,
                alert_type=str(a.alert_type).split(".")[-1],
                price_at_alert=a.price_at_alert,
                previous_price=a.previous_price,
                discount_percentage=a.discount_percentage,
                sent_at=a.sent_at,
            ))

    return result


@router.get(
    "/savings",
    summary="Relatório de economia total",
)
async def get_savings_report(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Calcula quanto o usuário economizou com os alertas.
    Ótimo para mostrar valor do produto e incentivar upgrade.
    """
    product_ids = [p.id for p in current_user.products]

    if not product_ids:
        return {
            "total_saved": 0,
            "total_alerts": 0,
            "best_saving": None,
            "message": "Nenhum alerta disparado ainda. Adicione produtos para começar!",
        }

    alerts = (
        db.query(Alert)
        .filter(
            Alert.product_id.in_(product_ids),
            Alert.previous_price.isnot(None),
        )
        .all()
    )

    total_saved = 0.0
    best_saving = None
    best_saving_pct = 0.0

    for a in alerts:
        if a.previous_price and a.previous_price > a.price_at_alert:
            saved = a.previous_price - a.price_at_alert
            total_saved += saved

            pct = ((a.previous_price - a.price_at_alert) / a.previous_price) * 100
            if pct > best_saving_pct:
                best_saving_pct = pct
                product = db.query(Product).filter(Product.id == a.product_id).first()
                if product:
                    best_saving = {
                        "product_name": product.name[:50],
                        "saved_amount": round(saved, 2),
                        "discount_pct": round(pct, 1),
                        "alerted_at": a.sent_at.isoformat(),
                    }

    return {
        "total_saved": round(total_saved, 2),
        "total_alerts": len(alerts),
        "best_saving": best_saving,
        "message": f"Você economizou R$ {total_saved:.2f} com {len(alerts)} alertas!" if alerts else "Ainda sem economias registradas.",
    }
