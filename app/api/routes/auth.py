"""
auth.py (routes) — Endpoints de Autenticação

POST /auth/register → cadastro
POST /auth/login    → login (retorna tokens JWT)
GET  /auth/me       → dados do usuário logado
POST /auth/refresh  → renova access_token
POST /auth/logout   → invalida refresh_token (client-side no MVP)
"""

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr, validator

from app.db.database import get_db
from app.models.models import User
from app.core.auth import (
    hash_password, authenticate_user,
    create_access_token, create_refresh_token,
    decode_token, get_current_user,
)
from app.schemas.schemas import UserOut

router = APIRouter(prefix="/auth", tags=["Autenticação"])


# ─────────────────────────────────────────────────────────────
# SCHEMAS DE AUTH
# ─────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    name: str
    email: EmailStr
    password: str

    @validator("password")
    def password_strength(cls, v):
        if len(v) < 6:
            raise ValueError("Senha deve ter no mínimo 6 caracteres")
        return v

    @validator("name")
    def name_not_empty(cls, v):
        if not v.strip():
            raise ValueError("Nome não pode estar vazio")
        return v.strip()


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOut


class RefreshRequest(BaseModel):
    refresh_token: str


class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ─────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────

@router.post(
    "/register",
    response_model=LoginResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Cadastrar novo usuário",
)
async def register(data: RegisterRequest, db: Session = Depends(get_db)):
    """
    Registra novo usuário e já retorna tokens (login automático).

    - **name**: nome completo
    - **email**: email único
    - **password**: mínimo 6 caracteres
    """
    # Verifica se email já existe
    existing = db.query(User).filter(
        User.email == data.email.lower().strip()
    ).first()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Este email já está cadastrado. Use outro ou faça login.",
        )

    # Cria usuário com senha hasheada
    user = User(
        name=data.name.strip(),
        email=data.email.lower().strip(),
        hashed_password=hash_password(data.password),
        plan="free",
        is_active=True,
        last_login_at=datetime.utcnow(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Retorna tokens imediatamente (não precisa fazer login separado)
    access_token = create_access_token(user.id, user.email)
    refresh_token = create_refresh_token(user.id)

    # Adiciona total_products para o schema
    user.total_products = 0

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserOut.model_validate(user),
    )


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Login com email e senha",
)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    """
    Autentica o usuário e retorna tokens JWT.

    Aceita tanto form-data (padrão OAuth2) quanto JSON.
    - **username**: email do usuário
    - **password**: senha
    """
    user = authenticate_user(db, form_data.username, form_data.password)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email ou senha inválidos.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Atualiza último login
    user.last_login_at = datetime.utcnow()
    db.commit()

    access_token = create_access_token(user.id, user.email)
    refresh_token = create_refresh_token(user.id)

    user.total_products = len(user.products)

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserOut.model_validate(user),
    )


@router.get(
    "/me",
    response_model=UserOut,
    summary="Dados do usuário logado",
)
async def get_me(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Retorna dados do usuário autenticado.
    Requer Bearer token no header Authorization.
    """
    current_user.total_products = len(current_user.products)
    return current_user


@router.post(
    "/refresh",
    response_model=RefreshResponse,
    summary="Renovar access token",
)
async def refresh_token(data: RefreshRequest, db: Session = Depends(get_db)):
    """
    Troca um refresh_token válido por um novo access_token.
    Útil para manter o usuário logado sem pedir senha novamente.
    """
    payload = decode_token(data.refresh_token)

    if not payload or payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token inválido ou expirado. Faça login novamente.",
        )

    user_id = payload.get("sub")
    user = db.query(User).filter(
        User.id == int(user_id),
        User.is_active == True,
    ).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuário não encontrado.",
        )

    new_access_token = create_access_token(user.id, user.email)

    return RefreshResponse(access_token=new_access_token)


@router.post(
    "/logout",
    summary="Logout",
)
async def logout(current_user: User = Depends(get_current_user)):
    """
    MVP: logout é client-side (apagar o token no frontend).
    Fase 5: adicionar blacklist de tokens no Redis.
    """
    return {
        "message": "Logout realizado. Descarte o token no cliente.",
        "user_id": current_user.id,
    }
