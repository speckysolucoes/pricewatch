"""
database.py — Configuração da conexão com o banco de dados

SQLAlchemy + SQLite para o MVP.
Para migrar para PostgreSQL na Fase 3:
  Basta trocar DATABASE_URL em config.py — o resto não muda.
"""

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from app.core.config import settings


# ─────────────────────────────────────────────────────────────
# ENGINE: conexão com o banco
# ─────────────────────────────────────────────────────────────
# connect_args é necessário APENAS para SQLite (thread safety)
# Para PostgreSQL, remover esse parâmetro
engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False},  # SQLite only
    echo=settings.DEBUG,   # mostra SQL no console quando DEBUG=True
)

# ─────────────────────────────────────────────────────────────
# SESSION: fábrica de sessões de banco
# ─────────────────────────────────────────────────────────────
SessionLocal = sessionmaker(
    autocommit=False,  # controle manual de transações
    autoflush=False,   # não salva automaticamente
    bind=engine,
)

# ─────────────────────────────────────────────────────────────
# BASE: classe pai de todos os modelos
# ─────────────────────────────────────────────────────────────
Base = declarative_base()


# ─────────────────────────────────────────────────────────────
# DEPENDENCY: injetar sessão nos endpoints FastAPI
# ─────────────────────────────────────────────────────────────
def get_db():
    """
    Dependency injection para FastAPI.

    Uso nos endpoints:
        def meu_endpoint(db: Session = Depends(get_db)):
            ...

    Garante que a sessão sempre seja fechada após o request,
    mesmo se der erro.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────
# INIT: criar tabelas no banco
# ─────────────────────────────────────────────────────────────
def init_db():
    """
    Cria todas as tabelas no banco se não existirem.
    Chamado na inicialização da aplicação.

    Na Fase 3, isso será substituído por Alembic (migrations).
    """
    # Importar modelos aqui para garantir que estão registrados no Base
    from app.models import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    print("✅ Banco de dados inicializado")
