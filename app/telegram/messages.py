"""
messages.py — Templates e formatação de mensagens Telegram

Todas as mensagens do bot saem daqui.
Centralizar facilita mudar o estilo sem tocar na lógica.

MARKDOWN NO TELEGRAM:
  *negrito*
  _itálico_
  `código`
  [texto](url)
  \\escape de caracteres especiais: . - ( ) ! #
"""

from typing import Optional
from app.models.models import Product, AlertType


# ─────────────────────────────────────────────────────────────
# ESCAPE de caracteres especiais do Telegram MarkdownV2
# ─────────────────────────────────────────────────────────────

def escape_md(text: str) -> str:
    """
    Escapa caracteres especiais para Telegram MarkdownV2.
    Obrigatório para não quebrar a formatação.
    """
    special = r'\_*[]()~`>#+-=|{}.!'
    for char in special:
        text = text.replace(char, f"\\{char}")
    return text


def fmt_price(price: float) -> str:
    """Formata preço brasileiro: 1299.90 → R$ 1\\.299,90"""
    # Formato BR com milhar
    formatted = f"R$ {price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return escape_md(formatted)


# ─────────────────────────────────────────────────────────────
# MENSAGENS DO BOT
# ─────────────────────────────────────────────────────────────

def msg_welcome(user_name: str) -> str:
    return (
        f"👋 Olá, *{escape_md(user_name)}*\\!\n\n"
        "Sou o *Price Monitor Bot* 🤖\n"
        "Monitoro preços e te aviso quando cair\\.\n\n"
        "📌 *Comandos disponíveis:*\n"
        "/add \\— Monitorar produto\n"
        "/list \\— Meus produtos\n"
        "/remove \\— Parar monitoramento\n"
        "/status \\— Ver preço atual\n"
        "/help \\— Ajuda\n\n"
        "Envie a URL de qualquer produto para começar\\!"
    )


def msg_help() -> str:
    return (
        "🤖 *Price Monitor Bot \\— Ajuda*\n\n"
        "*Adicionar produto:*\n"
        "`/add <url> <preço_alvo>`\n"
        "Exemplo: `/add https://amazon.com.br/dp/B08X 299`\n\n"
        "*Listar produtos:*\n"
        "`/list`\n\n"
        "*Ver preço atual:*\n"
        "`/status <id>`\n\n"
        "*Remover produto:*\n"
        "`/remove <id>`\n\n"
        "*Reativar monitoramento:*\n"
        "`/reactivate <id>`\n\n"
        "💡 Você também pode enviar uma URL diretamente\\!"
    )


def msg_product_added(product: Product) -> str:
    store_emoji = {
        "amazon": "📦",
        "mercadolivre": "🛒",
        "shopee": "🛍️",
        "magalu": "🏪",
    }.get(product.store or "", "🔗")

    lines = [
        f"✅ *Produto adicionado\\!*\n",
        f"{store_emoji} *{escape_md(product.name[:60])}*\n",
        f"🏪 Loja: `{escape_md(product.store or 'Desconhecida')}`",
        f"🆔 ID: `{product.id}`",
    ]

    if product.current_price:
        lines.append(f"💰 Preço atual: *{fmt_price(product.current_price)}*")

    if product.target_price:
        lines.append(f"🎯 Preço alvo: *{fmt_price(product.target_price)}*")

    if product.alert_percentage:
        pct = escape_md(str(product.alert_percentage))
        lines.append(f"📉 Alerta em: *{pct}%* de queda")

    lines.append(f"\n🔔 Te aviso quando o preço baixar\\!")

    return "\n".join(lines)


def msg_product_list(products: list) -> str:
    if not products:
        return (
            "📭 *Você não tem produtos monitorados*\n\n"
            "Use `/add <url> <preço_alvo>` para começar\\!"
        )

    status_emoji = {
        "active": "🟢",
        "paused": "⏸️",
        "alerted": "🔔",
        "error": "🔴",
    }

    lines = [f"📋 *Seus produtos \\({len(products)}\\):*\n"]

    for p in products:
        emoji = status_emoji.get(str(p.status).split(".")[-1].lower(), "⚪")
        price_str = fmt_price(p.current_price) if p.current_price else "\\-\\-\\-"
        target_str = fmt_price(p.target_price) if p.target_price else "\\-\\-\\-"

        lines.append(
            f"{emoji} *{escape_md(p.name[:35])}*\n"
            f"   ID: `{p.id}` \\| Atual: {price_str} \\| Alvo: {target_str}\n"
        )

    lines.append("Use `/status <id>` para ver detalhes ou `/remove <id>` para parar\\.")
    return "\n".join(lines)


def msg_price_status(product: Product) -> str:
    store_emoji = {
        "amazon": "📦", "mercadolivre": "🛒",
    }.get(product.store or "", "🔗")

    lines = [
        f"{store_emoji} *{escape_md(product.name[:60])}*\n",
        f"💰 Preço atual: *{fmt_price(product.current_price) if product.current_price else 'N/A'}*",
    ]

    if product.initial_price:
        lines.append(f"📌 Preço inicial: {fmt_price(product.initial_price)}")

    if product.lowest_price:
        lines.append(f"📉 Menor já visto: *{fmt_price(product.lowest_price)}*")

    if product.target_price:
        remaining = (product.current_price or 0) - product.target_price
        if remaining > 0:
            lines.append(
                f"🎯 Faltam {fmt_price(remaining)} para o alvo de {fmt_price(product.target_price)}"
            )
        else:
            lines.append(f"✅ Preço alvo *já atingido\\!* Alvo: {fmt_price(product.target_price)}")

    if product.last_checked_at:
        from datetime import timezone
        from app.telegram.messages import escape_md as _e
        dt = product.last_checked_at.strftime("%d/%m %H:%M")
        lines.append(f"\n🕐 Verificado: `{dt}`")

    if product.url_affiliate:
        lines.append(f"\n[Ver produto]({product.url_affiliate})")

    return "\n".join(lines)


def msg_product_removed(product_name: str, product_id: int) -> str:
    return (
        f"🗑️ *Produto removido\\!*\n\n"
        f"_{escape_md(product_name[:60])}_\n\n"
        f"ID `{product_id}` não será mais monitorado\\."
    )


def msg_ask_target_price(product_name: str, current_price: Optional[float]) -> str:
    lines = [
        f"🔗 Produto encontrado:\n*{escape_md(product_name[:60])}*\n",
    ]
    if current_price:
        lines.append(f"💰 Preço atual: *{fmt_price(current_price)}*\n")

    lines.append(
        "Qual *preço alvo* você quer? \\(responda com o valor\\)\n"
        "Exemplo: `299` ou `1500.50`\n\n"
        "Ou use `/add <url> <preço>` para adicionar direto\\."
    )
    return "\n".join(lines)


def msg_alert(
    product: Product,
    current_price: float,
    previous_price: Optional[float],
    alert_type: AlertType,
) -> str:
    """Mensagem de alerta — enviada quando preço cai."""
    discount_pct = None
    if previous_price and previous_price > 0:
        discount_pct = round((1 - current_price / previous_price) * 100, 1)

    store_emoji = {
        "amazon": "📦", "mercadolivre": "🛒",
        "shopee": "🛍️", "magalu": "🏪",
    }.get(product.store or "", "🏷️")

    if alert_type == AlertType.PRICE_DROP:
        header = "🎯 *PREÇO ALVO ATINGIDO\\!*"
    elif alert_type == AlertType.PERCENTAGE_DROP:
        pct_str = escape_md(str(discount_pct)) if discount_pct else "?"
        header = f"📉 *QUEDA DE {pct_str}%\\!*"
    else:
        header = "🔥 *PROMOÇÃO DETECTADA\\!*"

    lines = [
        header, "",
        f"{store_emoji} *{escape_md(product.name[:60])}*", "",
    ]

    if previous_price:
        lines.append(f"~~{fmt_price(previous_price)}~~")

    lines.append(f"✅ *{fmt_price(current_price)}*")

    if discount_pct:
        pct_str = escape_md(str(discount_pct))
        lines.append(f"🏷️ Desconto: *{pct_str}%*")

    url = product.url_affiliate or product.url
    lines.extend(["", f"[👉 VER PRODUTO]({url})"])
    lines.extend(["", "⚡ _Price Monitor Bot_"])

    return "\n".join(lines)


def msg_error(detail: str) -> str:
    return f"❌ *Ops\\!* {escape_md(detail)}"


def msg_invalid_url() -> str:
    return (
        "❌ *URL inválida*\n\n"
        "Envie uma URL completa de produto\\. Exemplo:\n"
        "`https://www.amazon.com.br/dp/B08N5WRWNW`\n\n"
        "Lojas suportadas: Amazon, Mercado Livre e outras\\."
    )
