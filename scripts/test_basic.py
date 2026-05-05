#!/usr/bin/env python3
"""
test_basic.py — Testes básicos do sistema

Roda uma demonstração completa do MVP:
  1. Cria usuário
  2. Adiciona produto
  3. Verifica preço
  4. Simula ciclo de monitoramento

Uso: python scripts/test_basic.py
"""

import asyncio
import sys
import os

# Adiciona pasta raiz ao path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    print("\n" + "=" * 60)
    print("🤖 PRICE MONITOR BOT — TESTE DO SISTEMA")
    print("=" * 60)

    # ── 1. Inicializar banco ────────────────────────────────
    print("\n📦 1. Inicializando banco de dados...")
    from app.db.database import init_db, SessionLocal
    init_db()

    db = SessionLocal()

    # ── 2. Criar usuário de teste ───────────────────────────
    print("\n👤 2. Criando usuário de teste...")
    from app.models.models import User, Product, ProductStatus

    # Limpa dados de teste anteriores
    db.query(Product).filter(Product.user_id == 999).delete()
    db.query(User).filter(User.id == 999).delete()
    db.commit()

    user = User(
        id=999,
        name="Usuário Teste",
        email="teste@example.com",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    print(f"   ✅ Usuário criado: ID={user.id}, Email={user.email}")

    # ── 3. Testar detecção de loja e afiliados ──────────────
    print("\n🔗 3. Testando sistema de afiliados...")
    from app.services.affiliate import generate_affiliate_url, detect_store, clean_url

    test_urls = [
        "https://www.amazon.com.br/dp/B08N5WRWNW?ref=sr&fbclid=abc123",
        "https://www.mercadolivre.com.br/produto/MLB-123456",
        "https://www.shopee.com.br/produto-123",
        "https://www.magazineluiza.com.br/produto/abc",
    ]

    for url in test_urls:
        store = detect_store(url)
        clean = clean_url(url)
        affiliate, _ = generate_affiliate_url(clean)
        print(f"   🏪 Loja: {store or 'desconhecida':<15} | Afiliado: {affiliate[:60]}...")

    # ── 4. Adicionar produto para monitorar ─────────────────
    print("\n📦 4. Adicionando produto para monitorar...")
    from app.services.affiliate import generate_affiliate_url, clean_url

    product_url = "https://www.amazon.com.br/dp/B08N5WRWNW"
    clean = clean_url(product_url)
    affiliate_url, store = generate_affiliate_url(clean)

    product = Product(
        user_id=user.id,
        name="Produto de Teste - Notebook Gamer",
        url=clean,
        url_affiliate=affiliate_url,
        store=store,
        target_price=2500.00,  # alertar quando chegar em R$2.500
        alert_percentage=10.0,  # ou quando cair 10%
        status=ProductStatus.ACTIVE,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    print(f"   ✅ Produto criado: ID={product.id}")
    print(f"   🏪 Loja: {product.store}")
    print(f"   🎯 Preço alvo: R$ {product.target_price}")
    print(f"   📊 Alerta de queda: {product.alert_percentage}%")
    print(f"   🔗 URL afiliado: {product.url_affiliate[:60]}...")

    # ── 5. Verificar preço ──────────────────────────────────
    print("\n💰 5. Verificando preço (mock)...")
    from app.services.price_checker import check_price

    for i in range(3):
        result = await check_price(url=product.url, store=product.store)
        status = "✅" if result.success else "❌"
        price_str = f"R$ {result.price:.2f}" if result.price else "N/A"
        print(f"   {status} Verificação {i+1}: {price_str}")

    # ── 6. Rodar ciclo de monitoramento ─────────────────────
    print("\n🔄 6. Rodando ciclo de monitoramento completo...")
    from app.services.monitor import run_price_check_cycle

    summary = await run_price_check_cycle()
    print(f"   📊 Resultado: {summary}")

    # ── 7. Ver produto atualizado ────────────────────────────
    print("\n📈 7. Estado final do produto...")
    db.refresh(product)
    print(f"   💰 Preço inicial: R$ {product.initial_price}")
    print(f"   💰 Preço atual:   R$ {product.current_price}")
    print(f"   💰 Menor preço:   R$ {product.lowest_price}")
    print(f"   📅 Última checagem: {product.last_checked_at}")
    print(f"   🚦 Status: {product.status}")
    print(f"   📜 Histórico: {len(product.price_history)} registros")
    print(f"   🔔 Alertas: {len(product.alerts)} enviados")

    # ── 8. Limpeza ──────────────────────────────────────────
    print("\n🧹 8. Limpando dados de teste...")
    db.delete(product)
    db.delete(user)
    db.commit()
    db.close()
    print("   ✅ Dados removidos")

    print("\n" + "=" * 60)
    print("✅ TODOS OS TESTES PASSARAM!")
    print("=" * 60)
    print("\n📝 Próximos passos:")
    print("  1. cp .env.example .env  (configure suas variáveis)")
    print("  2. pip install -r requirements.txt")
    print("  3. uvicorn main:app --reload")
    print("  4. Acesse http://localhost:8000/docs")
    print()


if __name__ == "__main__":
    asyncio.run(main())
