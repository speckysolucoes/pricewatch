"""
auth.py — Sistema de Autenticação JWT

Responsável por:
  1. Hash de senhas com bcrypt (nunca salvar senha em texto)
  2. Geração de tokens JWT (access + refresh)
  3. Validação de tokens
  4. Dependency injection: get_current_user()

FLUXO DE AUTENTICAÇÃO:
  POST /auth/register → cria usuário com senha hasheada
  POST /auth/login    → valida credenciais → retorna access_token + refresh_token
  GET  /auth/me       → valida Bearer token → retorna dados do usuário
  POST /auth/refresh  → troca refresh_token por novo access_token

TOKENS JWT:
  access_token  → vida curta (7 dias no MVP, 1h em produção)
                  enviado no header: Authorization: Bearer <token>
  refresh_token → vida longa (30 dias)
                  usado para renovar access_token sem novo login
"""

from datetime import datetime, timedelta
from typing import Optional, Union

from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import get_db
from app.models.models import User

# ─────────────────────────────────────────────────────────────
# HASH DE SENHAS
# ─────────────────────────────────────────────────────────────

# bcrypt é o padrão ouro para hash de senhas
# "deprecated=auto" atualiza hashes antigos automaticamente
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """
    Gera hash bcrypt da senha.
    O hash inclui o salt — cada hash é diferente mesmo para mesma senha.
    """
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verifica se senha bate com o hash.
    Nunca compare strings diretamente — use sempre essa função.
    """
    return pwd_context.verify(plain_password, hashed_password)


# ─────────────────────────────────────────────────────────────
# TOKENS JWT
# ─────────────────────────────────────────────────────────────

def create_access_token(user_id: int, email: str) -> str:
    """
    Cria JWT de acesso.

    Payload (dados no token):
      sub   → user_id (subject)
      email → email do usuário
      type  → "access" (distingue de refresh)
      exp   → timestamp de expiração
      iat   → timestamp de criação
    """
    expire = datetime.utcnow() + timedelta(
        minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {
        "sub": str(user_id),
        "email": email,
        "type": "access",
        "exp": expire,
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(user_id: int) -> str:
    """
    Cria JWT de refresh (vida mais longa).
    Contém menos dados — só o necessário para renovar.
    """
    expire = datetime.utcnow() + timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "exp": expire,
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """
    Decodifica e valida um token JWT.

    Returns:
        Payload do token ou None se inválido/expirado
    """
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        return payload
    except JWTError:
        return None


# ─────────────────────────────────────────────────────────────
# DEPENDENCY INJECTION
# ─────────────────────────────────────────────────────────────

# OAuth2PasswordBearer: extrai token do header "Authorization: Bearer <token>"
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    Dependency que valida o token e retorna o usuário autenticado.

    Uso nos endpoints protegidos:
        @router.get("/meu-endpoint")
        def meu_endpoint(user: User = Depends(get_current_user)):
            return {"user": user.email}

    Lança 401 se:
      - Token ausente ou mal formatado
      - Token expirado
      - Usuário não existe mais
      - Usuário desativado
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Não autenticado. Faça login para continuar.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    payload = decode_token(token)

    if payload is None:
        raise credentials_exception

    # Verifica tipo do token (não aceita refresh como access)
    if payload.get("type") != "access":
        raise credentials_exception

    user_id = payload.get("sub")
    if user_id is None:
        raise credentials_exception

    # Busca usuário no banco
    user = db.query(User).filter(
        User.id == int(user_id),
        User.is_active == True,
    ).first()

    if user is None:
        raise credentials_exception

    return user


def get_current_user_optional(
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """
    Versão opcional: retorna None se não autenticado (não lança erro).
    Útil para endpoints públicos que têm comportamento diferente se logado.
    """
    try:
        return get_current_user(token=token, db=db)
    except HTTPException:
        return None


# ─────────────────────────────────────────────────────────────
# VALIDAÇÃO DE USUÁRIO PARA LOGIN
# ─────────────────────────────────────────────────────────────

def authenticate_user(db: Session, email: str, password: str) -> Optional[User]:
    """
    Valida credenciais de login.

    Returns:
        User se credenciais corretas, None se inválidas.

    Segurança: mesmo tempo de resposta para email errado e senha errada
    (evita enumeração de usuários por timing attacks).
    """
    user = db.query(User).filter(
        User.email == email.lower().strip(),
        User.is_active == True,
    ).first()

    if not user:
        # Executa verify_password mesmo com usuário inexistente
        # para evitar timing attacks
        verify_password("dummy", "$2b$12$SWrDVdtM5eHZZbyd.AixbuaymjTlHlYL4yY25BVY.xXSbRm.Komay")
        return None

    if not user.hashed_password:
        return None

    if not verify_password(password, user.hashed_password):
        return None

    return user
