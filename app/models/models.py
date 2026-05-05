"""
models.py — Modelos do banco de dados (tabelas)

Cada classe aqui = uma tabela no SQLite.
Usamos SQLAlchemy ORM para não escrever SQL na mão.

TABELAS:
  users    → quem usa o sistema
  products → produtos monitorados por usuário
  price_history → histórico de preços (para gráficos futuros)
  alerts   → alertas disparados (log de notificações)
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean,
    DateTime, ForeignKey, Text, Enum
)
from sqlalchemy.orm import relationship
import enum

from app.db.database import Base


# ─────────────────────────────────────────────────────────────
# ENUM: Status do produto monitorado
# ─────────────────────────────────────────────────────────────
class ProductStatus(str, enum.Enum):
    ACTIVE = "active"       # monitorando normalmente
    PAUSED = "paused"       # usuário pausou
    ALERTED = "alerted"     # alerta já foi enviado
    ERROR = "error"         # erro ao buscar preço


# ─────────────────────────────────────────────────────────────
# ENUM: Tipo de alerta enviado
# ─────────────────────────────────────────────────────────────
class AlertType(str, enum.Enum):
    PRICE_DROP = "price_drop"         # preço atingiu meta
    PERCENTAGE_DROP = "percentage_drop"  # queda percentual
    PROMOTION = "promotion"           # promoção automática detectada


# ─────────────────────────────────────────────────────────────
# TABELA: users
# ─────────────────────────────────────────────────────────────
class User(Base):
    """
    Usuário do sistema.
    No MVP, cadastro simples.
    Na Fase 3, terá senha + JWT.
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)

    # Identificação
    name = Column(String(100), nullable=False)
    email = Column(String(200), unique=True, index=True, nullable=False)

    # Autenticação (Fase 3)
    hashed_password = Column(String(255), nullable=True)

    # Telegram (Fase 2)
    telegram_id = Column(String(50), unique=True, nullable=True)

    # Plano (Fase 4)
    plan = Column(String(20), default="free")
    plan_expires_at = Column(DateTime, nullable=True)

    @property
    def max_products(self) -> int:
        return 5 if self.plan == "free" else 9999

    # Controle
    is_active = Column(Boolean, default=True)
    last_login_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relacionamentos: um usuário tem muitos produtos
    products = relationship("Product", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User id={self.id} email={self.email}>"


# ─────────────────────────────────────────────────────────────
# TABELA: products
# ─────────────────────────────────────────────────────────────
class Product(Base):
    """
    Produto que está sendo monitorado.

    Cada produto pertence a um usuário.
    Guarda o preço atual, preço alvo e URL original.
    """
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)

    # Relacionamento com usuário
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # Informações do produto
    name = Column(String(500), nullable=False)        # nome do produto
    url = Column(Text, nullable=False)                 # URL original enviada pelo usuário
    url_affiliate = Column(Text, nullable=True)        # URL com tag de afiliado
    image_url = Column(Text, nullable=True)            # imagem do produto (Fase 2)

    # Loja detectada automaticamente
    store = Column(String(50), nullable=True)          # "amazon", "mercadolivre", etc.

    # Preços
    initial_price = Column(Float, nullable=True)       # preço quando foi adicionado
    current_price = Column(Float, nullable=True)       # último preço verificado
    target_price = Column(Float, nullable=True)        # preço desejado pelo usuário
    lowest_price = Column(Float, nullable=True)        # menor preço já visto

    # Configuração de alerta por % de queda
    # Ex: 20 = alertar quando cair 20%
    alert_percentage = Column(Float, nullable=True)

    # Status atual do monitoramento
    status = Column(
        Enum(ProductStatus),
        default=ProductStatus.ACTIVE,
        nullable=False
    )

    # Controle temporal
    last_checked_at = Column(DateTime, nullable=True)  # última verificação
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relacionamentos
    user = relationship("User", back_populates="products")
    price_history = relationship("PriceHistory", back_populates="product", cascade="all, delete-orphan")
    alerts = relationship("Alert", back_populates="product", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Product id={self.id} name={self.name[:30]} price={self.current_price}>"


# ─────────────────────────────────────────────────────────────
# TABELA: price_history
# ─────────────────────────────────────────────────────────────
class PriceHistory(Base):
    """
    Histórico de preços de um produto.

    Cada verificação grava aqui.
    Serve para:
      - Mostrar gráfico de evolução de preços (Fase 3)
      - Detectar padrões de promoção
      - Calcular médias para avaliar se uma promo é boa
    """
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True, index=True)

    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    price = Column(Float, nullable=False)
    checked_at = Column(DateTime, default=datetime.utcnow)

    # Se o scraping falhou nessa verificação
    is_available = Column(Boolean, default=True)
    error_message = Column(String(500), nullable=True)

    # Relacionamento
    product = relationship("Product", back_populates="price_history")

    def __repr__(self):
        return f"<PriceHistory product={self.product_id} price={self.price} at={self.checked_at}>"


# ─────────────────────────────────────────────────────────────
# TABELA: alerts
# ─────────────────────────────────────────────────────────────
class Alert(Base):
    """
    Registro de cada alerta enviado.

    Serve para:
      - Não enviar o mesmo alerta duas vezes
      - Analytics: quais alertas geraram cliques
      - Log histórico para o usuário ver
    """
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, index=True)

    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    alert_type = Column(Enum(AlertType), nullable=False)

    # Preços no momento do alerta
    price_at_alert = Column(Float, nullable=False)
    previous_price = Column(Float, nullable=True)

    # Desconto calculado
    discount_percentage = Column(Float, nullable=True)

    # Mensagem enviada ao usuário
    message_sent = Column(Text, nullable=True)

    # Se o alerta foi entregue com sucesso
    delivered = Column(Boolean, default=False)

    sent_at = Column(DateTime, default=datetime.utcnow)

    # Relacionamento
    product = relationship("Product", back_populates="alerts")

    def __repr__(self):
        return f"<Alert id={self.id} type={self.alert_type} price={self.price_at_alert}>"
