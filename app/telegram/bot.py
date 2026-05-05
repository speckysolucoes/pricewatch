"""
bot.py — Bot Telegram completo

Comandos implementados:
  /start    → boas-vindas + cadastro automático
  /help     → ajuda
  /add      → adicionar produto
  /list     → listar produtos do usuário
  /remove   → parar monitoramento
  /status   → ver preço atual de um produto
  /reactivate → reativar produto alertado/pausado

Fluxo conversacional:
  Usuário envia URL → bot pergunta preço alvo → bot confirma e adiciona

SETUP:
  1. Crie um bot em @BotFather → copie o token
  2. Adicione TELEGRAM_BOT_TOKEN no .env
  3. Para canal de promoções: crie canal, adicione bot como admin,
     copie o ID do canal para TELEGRAM_CHANNEL_ID
  4. O bot inicia junto com o FastAPI (ver main.py)
"""

import logging
import re
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

from app.core.config import settings
from app.db.database import SessionLocal
from app.models.models import User, Product, ProductStatus
from app.services.affiliate import generate_affiliate_url, clean_url, detect_store
from app.services.price_checker import check_price
from app.services.monitor import check_product, reactivate_product
from app.telegram.messages import (
    msg_welcome, msg_help, msg_product_added, msg_product_list,
    msg_price_status, msg_product_removed, msg_ask_target_price,
    msg_error, msg_invalid_url, escape_md,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# UTILITÁRIOS DE BANCO
# ─────────────────────────────────────────────────────────────

def get_or_create_user(telegram_id: str, name: str, email: str = None) -> User:
    """Busca ou cria usuário pelo Telegram ID."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == str(telegram_id)).first()
        if not user:
            # Cria automaticamente quando usuário inicia o bot
            user = User(
                name=name,
                email=email or f"telegram_{telegram_id}@bot.local",
                telegram_id=str(telegram_id),
                is_active=True,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            logger.info(f"Novo usuário via Telegram: {name} (ID: {telegram_id})")
        return user
    finally:
        db.close()


def is_valid_url(text: str) -> bool:
    """Verifica se o texto é uma URL válida."""
    url_pattern = re.compile(
        r'^https?://'
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'
        r'localhost|'
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
        r'(?::\d+)?'
        r'(?:/?|[/?]\S+)$', re.IGNORECASE
    )
    return bool(url_pattern.match(text.strip()))


# ─────────────────────────────────────────────────────────────
# HANDLERS DE COMANDOS
# ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start — Boas-vindas e cadastro automático.
    Chamado quando usuário inicia conversa com o bot.
    """
    user = update.effective_user
    tg_user = get_or_create_user(
        telegram_id=str(user.id),
        name=user.full_name,
    )

    await update.message.reply_text(
        msg_welcome(user.first_name),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/help — Mostra lista de comandos."""
    await update.message.reply_text(
        msg_help(),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add <url> [preço_alvo]

    Exemplos:
      /add https://amazon.com.br/dp/B08X
      /add https://amazon.com.br/dp/B08X 299.90
    """
    user = update.effective_user
    args = context.args

    if not args:
        await update.message.reply_text(
            "❓ *Como usar:*\n"
            "`/add <url> <preço_alvo>`\n\n"
            "Exemplo:\n"
            "`/add https://www.amazon.com.br/dp/B08N5WRWNW 299`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    url = args[0]
    if not is_valid_url(url):
        await update.message.reply_text(
            msg_invalid_url(),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # Preço alvo (opcional na linha de comando)
    target_price = None
    if len(args) >= 2:
        try:
            target_price = float(args[1].replace(",", "."))
        except ValueError:
            await update.message.reply_text(
                msg_error("Preço inválido. Use números, ex: `299` ou `1299.90`"),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

    # Feedback imediato — scraping pode demorar
    msg = await update.message.reply_text(
        "🔍 Buscando produto\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    # Busca informações do produto
    clean = clean_url(url)
    store = detect_store(clean)
    result = await check_price(url=clean, store=store)

    db = SessionLocal()
    try:
        tg_user = get_or_create_user(str(user.id), user.full_name)
        db_user = db.query(User).filter(User.id == tg_user.id).first()

        if not result.success:
            # Cria produto mesmo sem preço atual (será verificado no próximo ciclo)
            product_name = f"Produto {store or 'desconhecido'}"
        else:
            product_name = result.product_name or f"Produto {store or 'desconhecido'}"

        # Se não tem preço alvo, salva URL e pede para o usuário
        if target_price is None and not result.success:
            await msg.edit_text(
                msg_ask_target_price(product_name, result.price),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            # Salva URL no contexto para o próximo passo
            context.user_data["pending_url"] = clean
            context.user_data["pending_name"] = product_name
            context.user_data["pending_price"] = result.price
            return

        # Se não tem preço alvo mas tem preço atual: pede confirmação
        if target_price is None and result.success:
            await msg.edit_text(
                msg_ask_target_price(product_name, result.price),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            context.user_data["pending_url"] = clean
            context.user_data["pending_name"] = product_name
            context.user_data["pending_price"] = result.price
            return

        # Cria produto com preço alvo definido
        affiliate_url, _ = generate_affiliate_url(clean)

        product = Product(
            user_id=db_user.id,
            name=product_name,
            url=clean,
            url_affiliate=affiliate_url,
            store=store,
            initial_price=result.price,
            current_price=result.price,
            lowest_price=result.price,
            target_price=target_price,
            status=ProductStatus.ACTIVE,
        )
        db.add(product)
        db.commit()
        db.refresh(product)

        await msg.edit_text(
            msg_product_added(product),
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    finally:
        db.close()


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/list — Lista produtos do usuário."""
    user = update.effective_user
    tg_user = get_or_create_user(str(user.id), user.full_name)

    db = SessionLocal()
    try:
        products = (
            db.query(Product)
            .filter(Product.user_id == tg_user.id)
            .order_by(Product.created_at.desc())
            .limit(20)
            .all()
        )

        await update.message.reply_text(
            msg_product_list(products),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    finally:
        db.close()


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status <id> — Ver preço atual de um produto."""
    user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "❓ Use: `/status <id>`\nEx: `/status 42`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    try:
        product_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            msg_error("ID inválido\\. Use um número inteiro\\."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    tg_user = get_or_create_user(str(user.id), user.full_name)
    db = SessionLocal()
    try:
        product = db.query(Product).filter(
            Product.id == product_id,
            Product.user_id == tg_user.id,
        ).first()

        if not product:
            await update.message.reply_text(
                msg_error(f"Produto {product_id} não encontrado\\. Use /list para ver seus produtos\\."),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        # Verifica preço agora
        msg = await update.message.reply_text(
            "🔍 Verificando preço agora\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

        await check_product(db, product)
        db.refresh(product)

        await msg.edit_text(
            msg_price_status(product),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    finally:
        db.close()


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/remove <id> — Para monitoramento de produto."""
    user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "❓ Use: `/remove <id>`\nEx: `/remove 42`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    try:
        product_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(msg_error("ID inválido\\."), parse_mode=ParseMode.MARKDOWN_V2)
        return

    tg_user = get_or_create_user(str(user.id), user.full_name)
    db = SessionLocal()
    try:
        product = db.query(Product).filter(
            Product.id == product_id,
            Product.user_id == tg_user.id,
        ).first()

        if not product:
            await update.message.reply_text(
                msg_error(f"Produto {product_id} não encontrado\\."),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        # Botões de confirmação
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Sim, remover", callback_data=f"remove_confirm_{product_id}"),
                InlineKeyboardButton("❌ Cancelar", callback_data="remove_cancel"),
            ]
        ])

        name_escaped = escape_md(product.name[:50])
        await update.message.reply_text(
            f"⚠️ Remover *{name_escaped}*\\?\n\nID `{product_id}`",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    finally:
        db.close()


async def callback_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback dos botões de confirmação de remoção."""
    query = update.callback_query
    await query.answer()

    if query.data == "remove_cancel":
        await query.edit_message_text("❌ Remoção cancelada\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    product_id = int(query.data.replace("remove_confirm_", ""))
    user = query.from_user
    tg_user = get_or_create_user(str(user.id), user.full_name)

    db = SessionLocal()
    try:
        product = db.query(Product).filter(
            Product.id == product_id,
            Product.user_id == tg_user.id,
        ).first()

        if product:
            name = product.name
            db.delete(product)
            db.commit()
            await query.edit_message_text(
                msg_product_removed(name, product_id),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await query.edit_message_text(
                msg_error("Produto não encontrado\\."),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
    finally:
        db.close()


async def cmd_reactivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/reactivate <id> — Reativa produto alertado/pausado."""
    if not context.args:
        await update.message.reply_text("❓ Use: `/reactivate <id>`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    try:
        product_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(msg_error("ID inválido\\."), parse_mode=ParseMode.MARKDOWN_V2)
        return

    db = SessionLocal()
    try:
        product = reactivate_product(db, product_id)
        if product:
            name_escaped = escape_md(product.name[:50])
            await update.message.reply_text(
                f"✅ *{name_escaped}* reativado\\! Monitoramento retomado\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await update.message.reply_text(
                msg_error("Produto não encontrado ou já está ativo\\."),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────
# HANDLER DE MENSAGENS (URLs enviadas direto)
# ─────────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Processa mensagens de texto:
      - URL → busca produto e pede preço alvo
      - Número → interpreta como preço alvo para URL pendente
    """
    text = update.message.text.strip()
    user = update.effective_user

    # ── Caso 1: tem URL pendente → user está respondendo com preço ──
    if "pending_url" in context.user_data:
        try:
            target_price = float(text.replace(",", ".").replace("R$", "").strip())
            if target_price <= 0:
                raise ValueError

            pending_url = context.user_data.pop("pending_url")
            pending_name = context.user_data.pop("pending_name", "Produto")
            pending_price = context.user_data.pop("pending_price", None)

            store = detect_store(pending_url)
            affiliate_url, _ = generate_affiliate_url(pending_url)
            tg_user = get_or_create_user(str(user.id), user.full_name)

            db = SessionLocal()
            try:
                db_user = db.query(User).filter(User.id == tg_user.id).first()
                product = Product(
                    user_id=db_user.id,
                    name=pending_name,
                    url=pending_url,
                    url_affiliate=affiliate_url,
                    store=store,
                    initial_price=pending_price,
                    current_price=pending_price,
                    lowest_price=pending_price,
                    target_price=target_price,
                    status=ProductStatus.ACTIVE,
                )
                db.add(product)
                db.commit()
                db.refresh(product)

                await update.message.reply_text(
                    msg_product_added(product),
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            finally:
                db.close()
            return

        except (ValueError, TypeError):
            await update.message.reply_text(
                msg_error("Valor inválido\\. Informe apenas o número, ex: `299` ou `1299\\.90`"),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

    # ── Caso 2: URL enviada diretamente ─────────────────────
    if is_valid_url(text):
        # Redireciona para o handler de /add sem preço alvo
        context.args = [text]
        await cmd_add(update, context)
        return

    # ── Caso 3: mensagem genérica ────────────────────────────
    await update.message.reply_text(
        "🤔 Não entendi\\. Envie uma URL de produto ou use /help para ver os comandos\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ─────────────────────────────────────────────────────────────
# INICIALIZAÇÃO DO BOT
# ─────────────────────────────────────────────────────────────

_application: Optional[Application] = None


def build_application() -> Application:
    """Constrói e configura a Application do Telegram."""
    if not settings.TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN não configurado no .env")

    app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()

    # Registra handlers de comandos
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("reactivate", cmd_reactivate))

    # Callback de botões inline
    app.add_handler(CallbackQueryHandler(callback_remove, pattern=r"^remove_"))

    # Mensagens de texto (URLs e respostas ao fluxo conversacional)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("✅ Telegram bot configurado com todos os handlers")
    return app


async def start_bot():
    """
    Inicia o bot em modo polling.
    Chamado no startup do FastAPI.

    POLLING vs WEBHOOK:
      - Polling: bot pergunta ao Telegram se tem mensagens novas (mais simples)
      - Webhook: Telegram envia mensagens para URL do servidor (mais eficiente)
      MVP: polling. Produção: considerar webhook.
    """
    global _application

    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("⚠️ TELEGRAM_BOT_TOKEN não configurado — bot Telegram desativado")
        return

    try:
        _application = build_application()
        await _application.initialize()
        await _application.start()
        await _application.updater.start_polling(
            drop_pending_updates=True,  # ignora mensagens enquanto estava offline
            allowed_updates=["message", "callback_query"],
        )
        logger.info("🤖 Telegram bot iniciado em modo polling")

    except Exception as e:
        logger.error(f"❌ Erro ao iniciar bot Telegram: {e}")


async def stop_bot():
    """Para o bot graciosamente."""
    global _application

    if _application:
        try:
            await _application.updater.stop()
            await _application.stop()
            await _application.shutdown()
            logger.info("🛑 Telegram bot parado")
        except Exception as e:
            logger.error(f"Erro ao parar bot: {e}")


def get_bot_application() -> Optional[Application]:
    """Retorna a application do bot para uso externo (ex: notifier.py)."""
    return _application
