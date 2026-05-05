"""
products.py — Endpoints de Produtos

Rotas:
  POST   /products/              → adicionar produto para monitorar
  GET    /products/              → listar todos os produtos
  GET    /products/{id}          → buscar produto com histórico
  PATCH  /products/{id}          → atualizar produto (preço alvo, etc.)
  DELETE /products/{id}          → parar monitoramento
  POST   /products/{id}/check    → verificar preço manualmente agora
  POST   /products/{id}/reactivate → reativar produto pausado/alertado
"""

from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from sqlalchemy.orm import Session
from typing import List

from app.db.database import get_db
from app.models.models import Product, User, ProductStatus
from app.schemas.schemas import (
    ProductCreate, ProductOut, ProductListOut,
    ProductUpdate, MessageResponse
)
from app.services.affiliate import generate_affiliate_url, clean_url
from app.services.price_checker import check_price
from app.services.monitor import check_product, reactivate_product

router = APIRouter(prefix="/products", tags=["Produtos"])


# ─────────────────────────────────────────────────────────────
# POST /products/ — Adicionar produto
# ─────────────────────────────────────────────────────────────

@router.post(
    "/",
    response_model=ProductOut,
    status_code=status.HTTP_201_CREATED,
    summary="Adicionar produto para monitorar",
)
async def add_product(
    product_data: ProductCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Adiciona um produto para monitoramento.

    Processo:
      1. Valida se usuário existe
      2. Limpa a URL (remove rastreamentos de terceiros)
      3. Gera URL de afiliado
      4. Detecta a loja automaticamente
      5. Verifica o preço inicial (em background)
      6. Salva no banco

    - **url**: URL direta do produto (Amazon, ML, etc.)
    - **target_price**: preço desejado (opcional)
    - **alert_percentage**: % de queda para alertar (opcional)
    """
    # ── Valida usuário ──────────────────────────────────────
    user = db.query(User).filter(
        User.id == product_data.user_id,
        User.is_active == True
    ).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Usuário {product_data.user_id} não encontrado",
        )

    # ── Verifica limite do plano ────────────────────────────
    from app.payments.subscriptions import can_add_product
    allowed, reason = can_add_product(db, user)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=reason,
        )

    # ── Valida que tem pelo menos uma condição de alerta ────
    if not product_data.target_price and not product_data.alert_percentage:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Informe target_price (preço alvo) ou alert_percentage (% de queda)",
        )

    # ── Limpa e processa a URL ──────────────────────────────
    clean = clean_url(product_data.url)
    affiliate_url, store = generate_affiliate_url(clean)

    # ── Cria o produto no banco ─────────────────────────────
    product = Product(
        user_id=product_data.user_id,
        name=product_data.name,
        url=clean,
        url_affiliate=affiliate_url,
        store=store,
        target_price=product_data.target_price,
        alert_percentage=product_data.alert_percentage,
        status=ProductStatus.ACTIVE,
    )

    db.add(product)
    db.commit()
    db.refresh(product)

    # ── Verifica preço inicial em background ────────────────
    # O usuário recebe resposta imediata e o preço é verificado depois
    background_tasks.add_task(_initial_price_check, db, product.id)

    return product


async def _initial_price_check(db: Session, product_id: int):
    """
    Verifica o preço inicial do produto em background.
    Chamado logo após adicionar o produto.
    """
    product = db.query(Product).filter(Product.id == product_id).first()
    if product:
        result = await check_price(url=product.url, store=product.store)
        if result.success:
            product.initial_price = result.price
            product.current_price = result.price
            product.lowest_price = result.price
            if result.product_name and result.product_name != product.name:
                product.name = result.product_name
            db.commit()


# ─────────────────────────────────────────────────────────────
# GET /products/ — Listar produtos
# ─────────────────────────────────────────────────────────────

@router.get(
    "/",
    response_model=List[ProductListOut],
    summary="Listar todos os produtos monitorados",
)
async def list_products(
    user_id: int = None,
    status: ProductStatus = None,
    store: str = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """
    Lista produtos com filtros opcionais.

    - **user_id**: filtrar por usuário
    - **status**: filtrar por status (active, paused, alerted, error)
    - **store**: filtrar por loja (amazon, mercadolivre, etc.)
    """
    query = db.query(Product)

    if user_id:
        query = query.filter(Product.user_id == user_id)
    if status:
        query = query.filter(Product.status == status)
    if store:
        query = query.filter(Product.store == store)

    products = query.offset(skip).limit(limit).all()
    return products


# ─────────────────────────────────────────────────────────────
# GET /products/{id} — Buscar produto com histórico
# ─────────────────────────────────────────────────────────────

@router.get(
    "/{product_id}",
    response_model=ProductOut,
    summary="Buscar produto com histórico completo",
)
async def get_product(product_id: int, db: Session = Depends(get_db)):
    """Retorna detalhes completos de um produto incluindo histórico de preços."""
    product = db.query(Product).filter(Product.id == product_id).first()

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Produto {product_id} não encontrado",
        )

    return product


# ─────────────────────────────────────────────────────────────
# PATCH /products/{id} — Atualizar produto
# ─────────────────────────────────────────────────────────────

@router.patch(
    "/{product_id}",
    response_model=ProductOut,
    summary="Atualizar configurações do produto",
)
async def update_product(
    product_id: int,
    update_data: ProductUpdate,
    db: Session = Depends(get_db),
):
    """
    Atualiza preço alvo, percentual de alerta ou nome do produto.
    Apenas campos informados são alterados.
    """
    product = db.query(Product).filter(Product.id == product_id).first()

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Produto {product_id} não encontrado",
        )

    # Atualiza apenas campos informados
    update_dict = update_data.dict(exclude_unset=True)
    for field, value in update_dict.items():
        setattr(product, field, value)

    db.commit()
    db.refresh(product)

    return product


# ─────────────────────────────────────────────────────────────
# DELETE /products/{id} — Remover produto
# ─────────────────────────────────────────────────────────────

@router.delete(
    "/{product_id}",
    response_model=MessageResponse,
    summary="Parar monitoramento de um produto",
)
async def delete_product(product_id: int, db: Session = Depends(get_db)):
    """Remove um produto do monitoramento (exclusão permanente)."""
    product = db.query(Product).filter(Product.id == product_id).first()

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Produto {product_id} não encontrado",
        )

    db.delete(product)
    db.commit()

    return MessageResponse(message=f"Produto '{product.name[:50]}' removido do monitoramento")


# ─────────────────────────────────────────────────────────────
# POST /products/{id}/check — Verificar agora
# ─────────────────────────────────────────────────────────────

@router.post(
    "/{product_id}/check",
    summary="Verificar preço agora (manual)",
    description="Força uma verificação imediata sem esperar o próximo ciclo.",
)
async def check_product_now(product_id: int, db: Session = Depends(get_db)):
    """Verifica o preço do produto imediatamente."""
    product = db.query(Product).filter(Product.id == product_id).first()

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Produto {product_id} não encontrado",
        )

    result = await check_product(db, product)

    return {
        "message": "Verificação concluída",
        "product_id": product_id,
        "result": result,
    }


# ─────────────────────────────────────────────────────────────
# POST /products/{id}/reactivate — Reativar produto
# ─────────────────────────────────────────────────────────────

@router.post(
    "/{product_id}/reactivate",
    response_model=MessageResponse,
    summary="Reativar monitoramento de produto pausado/alertado",
)
async def reactivate(product_id: int, db: Session = Depends(get_db)):
    """
    Reativa um produto que foi pausado ou já teve alerta enviado.
    Útil quando o preço subiu e você quer monitorar de novo.
    """
    product = reactivate_product(db, product_id)

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Produto {product_id} não encontrado ou já está ativo",
        )

    return MessageResponse(message=f"Produto '{product.name[:50]}' reativado com sucesso")
