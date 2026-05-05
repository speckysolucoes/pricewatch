# 🤖 Price Monitor Bot — Sistema de Monitoramento de Preços com Afiliados

MVP completo em Python + FastAPI para monitorar preços e gerar alertas com links de afiliado.

## 🚀 Início Rápido

```bash
# 1. Instalar dependências
pip install -r requirements.txt

# 2. Configurar variáveis de ambiente
cp .env.example .env
# edite o .env com suas configurações

# 3. Rodar o servidor
uvicorn main:app --reload

# 4. Acessar documentação interativa
# http://localhost:8000/docs
```

## 📁 Estrutura do Projeto

```
price_monitor/
├── main.py                          # Ponto de entrada FastAPI
├── requirements.txt                 # Dependências
├── .env.example                     # Variáveis de ambiente (copiar para .env)
├── price_monitor.db                 # SQLite (criado automaticamente)
│
├── app/
│   ├── core/
│   │   └── config.py               # Configurações centrais (Settings)
│   │
│   ├── db/
│   │   └── database.py             # Conexão SQLAlchemy + get_db()
│   │
│   ├── models/
│   │   └── models.py               # Tabelas: User, Product, PriceHistory, Alert
│   │
│   ├── schemas/
│   │   └── schemas.py              # Validação Pydantic (entrada/saída da API)
│   │
│   ├── services/
│   │   ├── affiliate.py            # Geração de links de afiliado
│   │   ├── price_checker.py        # Verificação de preços (mock → scraping)
│   │   ├── monitor.py              # Motor de monitoramento
│   │   ├── notifier.py             # Sistema de alertas
│   │   └── scheduler.py            # APScheduler (verificação periódica)
│   │
│   └── api/
│       └── routes/
│           ├── users.py            # CRUD de usuários
│           ├── products.py         # CRUD de produtos + endpoints de ação
│           └── monitor.py          # Health check + estatísticas
│
└── scripts/
    └── test_basic.py               # Teste integrado do sistema
```

## 🌐 Endpoints da API

### Usuários
| Método | Rota | Descrição |
|--------|------|-----------|
| POST | `/users/` | Criar usuário |
| GET | `/users/` | Listar usuários |
| GET | `/users/{id}` | Buscar usuário |
| DELETE | `/users/{id}` | Desativar usuário |

### Produtos
| Método | Rota | Descrição |
|--------|------|-----------|
| POST | `/products/` | Adicionar produto para monitorar |
| GET | `/products/` | Listar produtos (com filtros) |
| GET | `/products/{id}` | Detalhes + histórico de preços |
| PATCH | `/products/{id}` | Atualizar preço alvo / % alerta |
| DELETE | `/products/{id}` | Remover produto |
| POST | `/products/{id}/check` | Verificar preço agora (manual) |
| POST | `/products/{id}/reactivate` | Reativar produto pausado |

### Monitoramento
| Método | Rota | Descrição |
|--------|------|-----------|
| GET | `/monitor/health` | Status do sistema |
| POST | `/monitor/run` | Rodar ciclo manualmente |
| GET | `/monitor/stats` | Estatísticas gerais |

## 💡 Exemplo de Uso

```bash
# 1. Criar usuário
curl -X POST http://localhost:8000/users/ \
  -H "Content-Type: application/json" \
  -d '{"name": "João Silva", "email": "joao@exemplo.com"}'

# 2. Adicionar produto para monitorar
curl -X POST http://localhost:8000/products/ \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 1,
    "name": "Notebook Gamer",
    "url": "https://www.amazon.com.br/dp/B08N5WRWNW",
    "target_price": 2500.00,
    "alert_percentage": 10.0
  }'

# 3. Verificar preço manualmente
curl -X POST http://localhost:8000/products/1/check

# 4. Ver estatísticas
curl http://localhost:8000/monitor/stats
```

## 🗺️ Roadmap

### ✅ MVP (Atual)
- [x] Banco SQLite com histórico de preços
- [x] API REST completa
- [x] Sistema de afiliados (Amazon + ML + UTM)
- [x] Detecção automática de loja
- [x] Scheduler de verificação periódica
- [x] Alertas por preço alvo e queda percentual
- [x] Mock de preços (para testes sem scraping)

### 🔄 Fase 2 — Telegram + Scraping Real
- [ ] Bot Telegram com comandos (/add, /list, /remove)
- [ ] Canal de promoções automático
- [ ] Scraping real: Amazon + Mercado Livre
- [ ] Imagens dos produtos nas notificações

### 🎨 Fase 3 — Interface Web
- [ ] Frontend React/Vue
- [ ] Login com JWT
- [ ] Painel de controle
- [ ] Gráfico de histórico de preços
- [ ] Migração para PostgreSQL

### 💰 Fase 4 — Monetização
- [ ] Plano gratuito (5 produtos) vs Premium (ilimitado)
- [ ] Integração Stripe / Mercado Pago
- [ ] Dashboard de comissões de afiliados

### ⚡ Fase 5 — Escala
- [ ] Celery + Redis (workers independentes)
- [ ] Rate limiting inteligente por loja
- [ ] Cache de preços
- [ ] Múltiplos servidores

## 🧠 Como Detectar Boas Promoções

O sistema pode ser expandido para detectar promoções genuínas:

1. **Preço histórico**: alertar apenas se for menor que a média dos últimos 30 dias
2. **Sazonalidade**: Black Friday, Dia das Mães, etc. têm padrões conhecidos  
3. **Comparação entre lojas**: mesmo produto em múltiplas lojas
4. **Preço vs MSRP**: comparar com preço sugerido pelo fabricante

## 📈 Como Crescer Usuários

1. **Canal Telegram público** de promoções = aquisição orgânica
2. **SEO**: páginas de acompanhamento de preços por produto
3. **Compartilhamento**: "Encontrei por R$X, histórico mostra mínimo de R$Y"
4. **Parcerias**: blogs de economia, influenciadores de finanças
