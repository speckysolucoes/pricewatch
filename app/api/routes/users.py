"""
users.py — Endpoints de Usuários

Rotas:
  POST /users/          → criar usuário
  GET  /users/          → listar usuários
  GET  /users/{id}      → buscar usuário específico
  GET  /users/{id}/products → produtos de um usuário
  DELETE /users/{id}    → remover usuário
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from app.db.database import get_db
from app.models.models import User, Product
from app.schemas.schemas import UserCreate, UserOut, MessageResponse

router = APIRouter(prefix="/users", tags=["Usuários"])


@router.post(
    "/",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Criar usuário",
    description="Registra um novo usuário no sistema.",
)
async def create_user(user_data: UserCreate, db: Session = Depends(get_db)):
    """
    Cria um novo usuário.

    - **name**: nome completo
    - **email**: email único (usado como identificação)
    - **telegram_id**: opcional, para notificações no Telegram (Fase 2)
    """
    # Verifica se email já existe
    existing = db.query(User).filter(User.email == user_data.email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Usuário com email '{user_data.email}' já existe",
        )

    # Cria o usuário
    user = User(
        name=user_data.name,
        email=user_data.email,
        telegram_id=user_data.telegram_id,
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    return user


@router.get(
    "/",
    response_model=List[UserOut],
    summary="Listar usuários",
)
async def list_users(
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """Lista todos os usuários (com paginação)."""
    users = db.query(User).filter(User.is_active == True).offset(skip).limit(limit).all()

    # Adiciona contagem de produtos em cada usuário
    for user in users:
        user.total_products = len(user.products)

    return users


@router.get(
    "/{user_id}",
    response_model=UserOut,
    summary="Buscar usuário",
)
async def get_user(user_id: int, db: Session = Depends(get_db)):
    """Busca um usuário pelo ID."""
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Usuário {user_id} não encontrado",
        )

    user.total_products = len(user.products)
    return user


@router.delete(
    "/{user_id}",
    response_model=MessageResponse,
    summary="Remover usuário",
)
async def delete_user(user_id: int, db: Session = Depends(get_db)):
    """
    Remove um usuário e todos seus produtos monitorados.
    (Soft delete — marca como inativo)
    """
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Usuário {user_id} não encontrado",
        )

    # Soft delete: não remove do banco, só desativa
    user.is_active = False
    db.commit()

    return MessageResponse(message=f"Usuário {user_id} desativado com sucesso")
