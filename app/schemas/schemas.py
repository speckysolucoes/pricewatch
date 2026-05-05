"""
schemas.py — Schemas de validação com Pydantic

Schemas definem o formato dos dados que entram e saem da API.
São diferentes dos Models (banco) — servem para:
  - Validar input do usuário (campos obrigatórios, tipos)
  - Formatar output (o que retornar ao cliente)
  - Gerar documentação automática no Swagger

Padrão usado:
  XxxBase   → campos compartilhados
  XxxCreate → campos para criar (POST)
  XxxUpdate → campos para atualizar (PATCH) — todos opcionais
  XxxOut    → campos que a API retorna (GET)
"""

from pydantic import BaseModel, EmailStr, HttpUrl, validator
from typing import Optional, List
from datetime import datetime
from app.models.models import ProductStatus, AlertType


# ═════════════════════════════════════════════════════════════
# SCHEMAS DE USUÁRIO
# ═════════════════════════════════════════════════════════════

class UserBase(BaseModel):
    name: str
    email: EmailStr  # valida formato de email automaticamente


class UserCreate(UserBase):
    """Dados necessários para criar um usuário."""
    telegram_id: Optional[str] = None


class UserOut(UserBase):
    """O que a API retorna sobre um usuário."""
    id: int
    is_active: bool
    telegram_id: Optional[str]
    created_at: datetime

    # Total de produtos monitorados (campo calculado)
    total_products: Optional[int] = 0

    class Config:
        from_attributes = True  # permite criar a partir de objetos SQLAlchemy


# ═════════════════════════════════════════════════════════════
# SCHEMAS DE PRODUTO
# ═════════════════════════════════════════════════════════════

class ProductBase(BaseModel):
    name: str
    url: str  # URL original do produto

    # Preço que o usuário quer pagar
    target_price: Optional[float] = None

    # Alternativa: alertar quando cair X%
    # Ex: 20.0 = alertar quando cair 20%
    alert_percentage: Optional[float] = None

    @validator("target_price")
    def target_price_must_be_positive(cls, v):
        if v is not None and v <= 0:
            raise ValueError("Preço alvo deve ser maior que zero")
        return v

    @validator("alert_percentage")
    def percentage_must_be_valid(cls, v):
        if v is not None and (v <= 0 or v > 100):
            raise ValueError("Percentual deve ser entre 1 e 100")
        return v


class ProductCreate(ProductBase):
    """Dados para adicionar um produto novo."""
    user_id: int


class ProductUpdate(BaseModel):
    """Dados para atualizar produto — todos opcionais."""
    name: Optional[str] = None
    target_price: Optional[float] = None
    alert_percentage: Optional[float] = None
    status: Optional[ProductStatus] = None


class PriceHistoryOut(BaseModel):
    """Item do histórico de preços."""
    price: float
    checked_at: datetime
    is_available: bool

    class Config:
        from_attributes = True


class AlertOut(BaseModel):
    """Alerta enviado."""
    id: int
    alert_type: AlertType
    price_at_alert: float
    previous_price: Optional[float]
    discount_percentage: Optional[float]
    message_sent: Optional[str]
    delivered: bool
    sent_at: datetime

    class Config:
        from_attributes = True


class ProductOut(ProductBase):
    """O que a API retorna sobre um produto."""
    id: int
    user_id: int
    store: Optional[str]
    url_affiliate: Optional[str]
    image_url: Optional[str]

    initial_price: Optional[float]
    current_price: Optional[float]
    lowest_price: Optional[float]

    status: ProductStatus
    last_checked_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    # Incluir histórico e alertas nas buscas detalhadas
    price_history: List[PriceHistoryOut] = []
    alerts: List[AlertOut] = []

    # Campo calculado: desconto atual vs preço inicial
    @property
    def current_discount_percentage(self) -> Optional[float]:
        if self.initial_price and self.current_price and self.initial_price > 0:
            return round((1 - self.current_price / self.initial_price) * 100, 2)
        return None

    class Config:
        from_attributes = True


class ProductListOut(BaseModel):
    """Versão resumida para listagem (sem histórico completo)."""
    id: int
    name: str
    store: Optional[str]
    url_affiliate: Optional[str]
    current_price: Optional[float]
    target_price: Optional[float]
    initial_price: Optional[float]
    status: ProductStatus
    last_checked_at: Optional[datetime]

    class Config:
        from_attributes = True


# ═════════════════════════════════════════════════════════════
# SCHEMAS GERAIS
# ═════════════════════════════════════════════════════════════

class MessageResponse(BaseModel):
    """Resposta simples de sucesso."""
    message: str
    success: bool = True


class ErrorResponse(BaseModel):
    """Resposta de erro padrão."""
    detail: str
    error_code: Optional[str] = None
