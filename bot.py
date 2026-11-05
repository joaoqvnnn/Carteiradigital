import asyncio
import logging
import random
import re
import time
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from config import BOT_TOKEN, RENDER_EXTERNAL_URL, PORT, ADMIN_IDS
from database import (
    init_db, get_or_create_user, get_session, save_session,
    reset_session, log_event, get_admin_stats
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.Markdown))
dp = Dispatcher()

# ==================== LOCKS / RATE LIMIT ====================
_locks: dict[int, asyncio.Lock] = {}
_clicks: dict[str, list[float]] = {}

def _rate_ok(uid: int, action: str, max_c=4, window=2.0) -> bool:
    now = time.time()
    key = f"{uid}:{action}"
    times = [t for t in _clicks.get(key, []) if now - t < window]
    if len(times) >= max_c:
        return False
    times.append(now)
    _clicks[key] = times
    return True

async def _locked(uid: int, coro):
    if uid not in _locks:
        _locks[uid] = asyncio.Lock()
    async with _locks[uid]:
        return await coro

# ==================== VALIDAÇÕES ====================
def validate_cpf(cpf: str) -> bool:
    c = re.sub(r"\D", "", cpf)
    if len(c) != 11 or len(set(c)) == 1:
        return False
    s = sum(int(c[i]) * (10 - i) for i in range(9))
    d1 = (s * 10) % 11
    if d1 == 10: d1 = 0
    if d1 != int(c[9]): return False
    s = sum(int(c[i]) * (11 - i) for i in range(10))
    d2 = (s * 10) % 11
    if d2 == 10: d2 = 0
    if d2 != int(c[10]): return False
    return True

def validate_email(e: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$", e.strip()))

def validate_phone(p: str) -> bool:
    d = re.sub(r"\D", "", p)
    if len(d) not in (10, 11): return False
    if len(d) == 11 and d[2] != "9": return False
    if len(d) == 10 and d[2] not in "2345": return False
    return True

def parse_birth(s: str):
    try:
        d = datetime.strptime(s.strip(), "%d/%m/%Y").date()
    except ValueError:
        return None
    today = datetime.now().date()
    age = today.year - d.year - ((today.month, today.day) < (d.month, d.day))
    if age < 18 or age > 120:
        return None
    return d, age

def mask_email(e: str) -> str:
    local, _, dom = e.partition("@")
    return f"{local[0]}***@{dom}" if len(local) <= 2 else f"{local[0]}***{local[-1]}@{dom}"

def mask_phone(p: str) -> str:
    d = re.sub(r"\D", "", p)
    return f"({d[:2]}) {d[2]}****-{d[-4:]}" if len(d) == 11 else f"({d[:2]}) ****-{d[-4:]}"

def gen_code() -> str:
    return f"{random.randint(100000, 999999)}"

# ==================== KEYBOARDS ====================
def kb_welcome():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Abrir minha conta", callback_data="abrir_conta")],
        [InlineKeyboardButton(text="🔑 Já tenho conta", callback_data="ja_tenho")],
        [InlineKeyboardButton(text="💬 Ajuda e atendimento", callback_data="ajuda")],
    ])

def kb_voltar(cb="voltar"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Voltar", callback_data=cb)]
    ])

def kb_conf_email():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Corrigir e-mail", callback_data="re_email")],
        [InlineKeyboardButton(text="📧 Reenviar código", callback_data="reenviar_email")],
    ])

def kb_conf_fone():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Corrigir telefone", callback_data="re_fone")],
        [InlineKeyboardButton(text="📱 Reenviar código", callback_data="reenviar_fone")],
    ])

def kb_kyc():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 Abrir verificação segura", callback_data="kyc_abrir")],
        [InlineKeyboardButton(text="ℹ️ Ajuda", callback_data="ajuda")],
    ])

def kb_termos():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Aceitar e continuar", callback_data="termos_aceitar")],
        [InlineKeyboardButton(text="❌ Não aceitar", callback_data="termos_recusar")],
    ])

def kb_analise():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Consultar status", callback_data="analise_status")],
        [InlineKeyboardButton(text="💬 Atendimento", callback_data="ajuda")],
    ])

def kb_aprovado():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔓 Acessar minha conta", callback_data="acessar_conta")],
    ])

def kb_recomecar():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Continuar", callback_data="continuar")],
        [InlineKeyboardButton(text="🔄 Recomeçar", callback_data="recomecar")],
    ])

# ==================== TEXTS ====================
TXT_WELCOME = """🏦 *Banco Digital*

Abra sua conta de forma simples, rápida e segura.

🔐 *Sua conta protegida*
Cadastro seguro, verificação de identidade e proteção dos seus dados.

_Escolha uma opção abaixo._"""

TXT_RESUME = """👋 *Olá de novo!*

Você tem um cadastro em andamento.

Etapa atual: *{state}*

Deseja continuar de onde parou?"""

TXT_NOME = "📝 *Etapa 1 de 7 — Identificação*\n\nInforme seu *nome completo* (como consta no documento).\n\nExemplo: `João da Silva Souza`"

TXT_NASC = "🎂 *Etapa 2 de 7 — Data de nascimento*\n\nInforme no formato *DD/MM/AAAA*.\n\nExemplo: `15/03/1990`\n\n_Você precisa ter 18 anos ou mais._"

TXT_EMAIL = "📧 *Etapa 3 de 7 — E-mail*\n\nInforme seu e-mail. Enviaremos um código de verificação.\n\nExemplo: `seunome@email.com`"

TXT_EMAIL_CONF = "📧 *Confirmação de e-mail*\n\nEnviamos um código para *{email}*.\n\nDigite o código de 6 dígitos.\n\n🧪 _Modo demo — código: `{code}`_"

TXT_FONE = "📱 *Etapa 4 de 7 — Telefone celular*\n\nInforme com DDD.\n\nExemplo: `(11) 91234-5678`"

TXT_FONE_CONF = "📱 *Confirmação de telefone*\n\nEnviamos um código para *{phone}*.\n\nDigite o código de 6 dígitos.\n\n🧪 _Modo demo — código: `{code}`_"

TXT_CPF = "🪪 *Etapa 5 de 7 — CPF*\n\nInforme seu CPF.\n\nExemplo: `123.456.789-09`\n\n_Seus dados são processados com segurança pela instituição parceira._"

TXT_KYC = """🔐 *Etapa 6 de 7 — Verificação de identidade*

Para sua segurança, a verificação de documentos é feita em nossa *página segura* da instituição parceira.

⚠️ *Nunca envie documentos, selfies ou senhas por mensagens.*

Clique no botão para abrir a verificação segura."""

TXT_KYC_WAIT = "⏳ *Verificação em andamento*\n\nA instituição parceira está analisando seus dados.\n\n_Aguarde..._"

TXT_KYC_OK = "✅ *Identidade verificada!*\n\nSua verificação foi concluída com sucesso.\n\nVamos aos termos de abertura da conta."

TXT_TERMOS = """📄 *Etapa 7 de 7 — Termos e consentimentos*

Para abrir sua conta, você precisa aceitar:

• Termos de abertura da conta
• Política de privacidade
• Autorizações necessárias

_Versão dos termos: v1.0 · {date}_"""

TXT_ANALISE = "🔎 *Solicitação em análise*\n\nRecebemos seu cadastro! A instituição parceira está analisando sua solicitação.\n\n⏱ Prazo médio: até 24h úteis.\n\n🧪 _Modo demo — clique em "Consultar status" para simular aprovação._"

TXT_APROVADO = "🎉 *Conta aprovada!*\n\nParabéns, *{name}*! Sua conta foi aberta com sucesso.\n\n📋 *Dados da conta*\n• Identificador: `{account_id}`\n• Instituição: Instituição Parceira Demo\n• Status: Ativa ✅"

TXT_AJUDA = """💬 *Ajuda e atendimento*

• 📧 suporte@bancodemo.com
• 🕐 24h

_Não solicitamos senhas, documentos ou códigos por mensagem._"""

TXT_JA_TENHO = "🔑 *Já tenho conta*\n\nO login estará disponível no *Módulo 02*.\n\nEste é um ambiente demonstrativo de abertura de conta."

# ==================== MESSAGE MANAGER ====================
async def show(chat_id: int, text: str, keyboard=None, session=None):
    """Edita a mensagem principal; envia nova apenas se não houver."""
    if session and session.get("main_message_id"):
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=session["main_message_id"],
                text=text,
                reply_markup=keyboard,
            )
            return session["main_message_id"]
        except Exception as e:
            if "message is not modified" in str(e):
                return session["main_message_id"]
            logger.warning(f"edit falhou: {e} — enviando nova")
    msg = await bot.send_message(chat_id, text, reply_markup=keyboard)
    await save_session(chat_id, main_message_id=msg.message_id)
    return msg.message_id

async def _delete(m: Message):
    try:
        await m.delete()
    except Exception:
        pass

# ==================== HANDLERS ====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await _delete(message)
    u = message.from_user
    await get_or_create_user(u.id, u.first_name, u.username)
    await log_event(u.id, "start", "")

    session = await get_session(u.id)

    if session and session["state"] not in ("inicio", "aprovado", "recusado"):
        label = session["state"].replace("_", " ").title()
        await show(u.id, TXT_RESUME.format(state=label), kb_recomecar(), session)
        return

    if session and session.get("main_message_id"):
        try:
            await bot.delete_message(u.id, session["main_message_id"])
        except Exception:
            pass

    await reset_session(u.id)
    msg = await bot.send_message(u.id, TXT_WELCOME, reply_markup=kb_welcome())
    await save_session(u.id, state="inicio", data={}, main_message_id=msg.message_id)


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply("Acesso negado.")
        return
    stats = await get_admin_stats()
    await message.reply(stats, parse_mode="Markdown")


# ---------- CALLBACKS ----------
@dp.callback_query()
async def on_cb(cb: CallbackQuery):
    u = cb.from_user
    data = cb.data or ""

    if not _rate_ok(u.id, data):
        await cb.answer("⏳ Muitos cliques. Aguarde.", show_alert=False)
        return

    await cb.answer()
    await _locked(u.id, _handle_cb(cb))


async def _handle_cb(cb: CallbackQuery):
    u = cb.from_user
    data = cb.data or ""
    session = await get_session(u.id)
    if not session:
        await bot.send_message(u.id, "Use /start para começar.")
        return

    # ---------- WELCOME ----------
    if data == "abrir_conta":
        if session["state"] not in ("inicio", "recusado"):
            # idempotência: já em fluxo
            await show(u.id, "⚠️ Você já tem um cadastro em andamento.", kb_recomecar(), session)
            return
        await save_session(u.id, state="aguardando_nome", data={})
        await log_event(u.id, "onboarding_started")
        await show(u.id, TXT_NOME, kb_voltar("voltar_inicio"), session)
        return

    if data == "ja_tenho":
        await show(u.id, TXT_JA_TENHO, kb_voltar("voltar_inicio"), session)
        return

    if data == "ajuda":
        await show(u.id, TXT_AJUDA, kb_voltar("voltar_inicio"), session)
        return

    if data == "voltar_inicio":
        await save_session(u.id, state="inicio")
        await show(u.id, TXT_WELCOME, kb_welcome(), session)
        return

    if data == "recomecar":
        await reset_session(u.id)
        msg = await bot.send_message(u.id, TXT_WELCOME, reply_markup=kb_welcome())
        await save_session(u.id, state="inicio", data={}, main_message_id=msg.message_id)
        await log_event(u.id, "onboarding_restarted")
        return

    if data == "continuar":
        # Reexibe a tela do estado atual
        await _render_current(u.id, session)
        return

    # ---------- CONFIRMAÇÕES ----------
    if data in ("re_email", "reenviar_email"):
        d = session["data"]
        code = gen_code()
        d["email_code"] = code
        await save_session(u.id, state="aguardando_confirmacao_email", data=d)
        await show(u.id, TXT_EMAIL_CONF.format(email=mask_email(d.get("email","")), code=code), kb_conf_email(), session)
        return

    if data in ("re_fone", "reenviar_fone"):
        d = session["data"]
        code = gen_code()
        d["phone_code"] = code
        await save_session(u.id, state="aguardando_confirmacao_telefone", data=d)
        await show(u.id, TXT_FONE_CONF.format(phone=mask_phone(d.get("phone","")), code=code), kb_conf_fone(), session)
        return

    # ---------- KYC ----------
    if data == "kyc_abrir":
        d = session["data"]
        d["kyc_started"] = True
        await save_session(u.id, state="aguardando_verificacao", data=d)
        await show(u.id, TXT_KYC_WAIT, None, session)
        asyncio.create_task(_kyc_simulate(u.id))
        return

    # ---------- TERMOS ----------
    if data == "ir_termos":
        await save_session(u.id, state="aguardando_termos")
        await show(u.id, TXT_TERMOS.format(date=datetime.now().strftime("%d/%m/%Y")), kb_termos(), session)
        return

    if data == "termos_aceitar":
        d = session["data"]
        d["termos_aceitos"] = True
        d["termos_versao"] = "v1.0"
        d["termos_data"] = datetime.now().isoformat()
        await save_session(u.id, state="analise", data=d)
        await log_event(u.id, "terms_accepted", "v1.0")
        await show(u.id, TXT_ANALISE, kb_analise(), session)
        return

    if data == "termos_recusar":
        await log_event(u.id, "terms_rejected")
        await reset_session(u.id)
        try:
            await bot.delete_message(u.id, session["main_message_id"])
        except Exception:
            pass
        await bot.send_message(u.id, "Cadastro cancelado. Use /start para recomeçar.")
        return

    # ---------- ANÁLISE ----------
    if data == "analise_status":
        d = session["data"]
        account_id = f"ACC{random.randint(100000, 999999)}"
        d["account_id"] = account_id
        await save_session(u.id, state="aprovado", data=d)
        await log_event(u.id, "account_approved", account_id)
        nome = (d.get("nome") or "Cliente").split()[0]
        await show(u.id, TXT_APROVADO.format(name=nome, account_id=account_id), kb_aprovado(), session)
        return

    if data == "acessar_conta":
        await show(u.id,
            "🎉 *Bem-vindo ao Banco Digital!*\n\nO *Módulo 02 (Login e acesso à conta)* estará disponível em breve.\n\n_Obrigado por testar este ambiente demonstrativo._",
            None, session)
        return

    await cb.answer("Ação não reconhecida.", show_alert=False)


async def _kyc_simulate(user_id: int):
    await asyncio.sleep(3)
    session = await get_session(user_id)
    if not session or session["state"] != "aguardando_verificacao":
        return
    d = session["data"]
    d["kyc_approved"] = True
    await save_session(user_id, data=d)
    await log_event(user_id, "kyc_approved_simulated")
    try:
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=session["main_message_id"],
            text=TXT_KYC_OK,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="▶️ Continuar para termos", callback_data="ir_termos")]
            ]),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.warning(f"kyc edit: {e}")


async def _render_current(user_id: int, session):
    """Reexibe a tela do estado atual sem perder dados."""
    st = session["state"]
    d = session["data"]
    if st == "aguardando_nome":
        await show(user_id, TXT_NOME, kb_voltar("voltar_inicio"), session)
    elif st == "aguardando_nascimento":
        await show(user_id, TXT_NASC, kb_voltar(), session)
    elif st == "aguardando_email":
        await show(user_id, TXT_EMAIL, kb_voltar(), session)
    elif st == "aguardando_confirmacao_email":
        await show(user_id, TXT_EMAIL_CONF.format(email=mask_email(d.get("email","")), code=d.get("email_code","")), kb_conf_email(), session)
    elif st == "aguardando_telefone":
        await show(user_id, TXT_FONE, kb_voltar(), session)
    elif st == "aguardando_confirmacao_telefone":
        await show(user_id, TXT_FONE_CONF.format(phone=mask_phone(d.get("phone","")), code=d.get("phone_code","")), kb_conf_fone(), session)
    elif st == "aguardando_cpf":
        await show(user_id, TXT_CPF, kb_voltar(), session)
    elif st == "aguardando_verificacao":
        await show(user_id, TXT_KYC_WAIT, None, session)
        asyncio.create_task(_kyc_simulate(user_id))
    elif st == "aguardando_termos":
        await show(user_id, TXT_TERMOS.format(date=datetime.now().strftime("%d/%m/%Y")), kb_termos(), session)
    elif st == "analise":
        await show(user_id, TXT_ANALISE, kb_analise(), session)
    elif st in ("aprovado", "recusado"):
        await show(user_id, TXT_WELCOME, kb_welcome(), session)


# ---------- VOLTAR ----------
@dp.callback_query(F.data == "voltar")
async def cb_voltar(cb: CallbackQuery):
    await cb.answer()
    u = cb.from_user
    await _locked(u.id, _handle_voltar(cb))


async def _handle_voltar(cb: CallbackQuery):
    u = cb.from_user
    session = await get_session(u.id)
    if not session:
        return
    st = session["state"]
    prev = {
        "aguardando_nascimento": "aguardando_nome",
        "aguardando_email": "aguardando_nascimento",
        "aguardando_telefone": "aguardando_confirmacao_email",
        "aguardando_cpf": "aguardando_confirmacao_telefone",
        "aguardando_verificacao": "aguardando_cpf",
        "aguardando_termos": "aguardando_verificacao",
    }.get(st, "inicio")

    if prev == "inicio":
        await save_session(u.id, state="inicio")
        await show(u.id, TXT_WELCOME, kb_welcome(), session)
        return

    await save_session(u.id, state=prev)
    session["state"] = prev
    await _render_current(u.id, session)


# ---------- TEXTOS ----------
@dp.message(F.content_type != "text")
async def on_non_text(message: Message):
    await _delete(message)


@dp.message(F.text)
async def on_text(message: Message):
    u = message.from_user
    text = message.text.strip()
    await _delete(message)

    session = await get_session(u.id)
    if not session:
        await bot.send_message(u.id, "Use /start para começar.")
        return

    await _locked(u.id, _handle_text(u.id, text))


async def _handle_text(user_id: int, text: str):
    session = await get_session(user_id)
    st = session["state"]
    d = session["data"]

    # ---------- NOME ----------
    if st == "aguardando_nome":
        if len(text) < 5 or len(text.split()) < 2 or not re.match(r"^[A-Za-zÀ-ÿ' ]+$", text):
            await show(user_id, "❌ *Nome inválido*\n\nInforme nome e sobrenome, apenas letras.", kb_voltar("voltar_inicio"), session)
            return
        d["nome"] = text.title()
        await save_session(user_id, state="aguardando_nascimento", data=d)
        await show(user_id, TXT_NASC, kb_voltar(), session)
        return

    # ---------- NASCIMENTO ----------
    if st == "aguardando_nascimento":
        result = parse_birth(text)
        if not result:
            await show(user_id, "❌ *Data inválida*\n\nUse DD/MM/AAAA e idade entre 18 e 120 anos.", kb_voltar(), session)
            return
        dt, age = result
        d["nascimento"] = dt.isoformat()
        d["idade"] = age
        await save_session(user_id, state="aguardando_email", data=d)
        await show(user_id, TXT_EMAIL, kb_voltar(), session)
        return

    # ---------- EMAIL ----------
    if st == "aguardando_email":
        if not validate_email(text):
            await show(user_id, "❌ *E-mail inválido*\n\nTente novamente.", kb_voltar(), session)
            return
        code = gen_code()
        d["email"] = text.lower()
        d["email_code"] = code
        await save_session(user_id, state="aguardando_confirmacao_email", data=d)
        await show(user_id, TXT_EMAIL_CONF.format(email=mask_email(text), code=code), kb_conf_email(), session)
        return

    if st == "aguardando_confirmacao_email":
        if text != d.get("email_code"):
            await show(user_id, "❌ *Código incorreto*\n\nDigite novamente ou reenvie.", kb_conf_email(), session)
            return
        d["email_verified"] = True
        d.pop("email_code", None)
        await save_session(user_id, state="aguardando_telefone", data=d)
        await show(user_id, TXT_FONE, kb_voltar(), session)
        return

    # ---------- TELEFONE ----------
    if st == "aguardando_telefone":
        if not validate_phone(text):
            await show(user_id, "❌ *Telefone inválido*\n\nUse DDD + 8 ou 9 dígitos.", kb_voltar(), session)
            return
        code = gen_code()
        d["phone"] = re.sub(r"\D", "", text)
        d["phone_code"] = code
        await save_session(user_id, state="aguardando_confirmacao_telefone", data=d)
        await show(user_id, TXT_FONE_CONF.format(phone=mask_phone(d["phone"]), code=code), kb_conf_fone(), session)
        return

    if st == "aguardando_confirmacao_telefone":
        if text != d.get("phone_code"):
            await show(user_id, "❌ *Código incorreto*\n\nDigite novamente ou reenvie.", kb_conf_fone(), session)
            return
        d["phone_verified"] = True
        d.pop("phone_code", None)
        await save_session(user_id, state="aguardando_cpf", data=d)
        await show(user_id, TXT_CPF, kb_voltar(), session)
        return

    # ---------- CPF ----------
    if st == "aguardando_cpf":
        if not validate_cpf(text):
            await show(user_id, "❌ *CPF inválido*\n\nVerifique os dígitos e tente novamente.", kb_voltar(), session)
            return
        d["cpf"] = re.sub(r"\D", "", text)
        await save_session(user_id, state="aguardando_verificacao", data=d)
        await log_event(user_id, "cpf_registered")
        await show(user_id, TXT_KYC, kb_kyc(), session)
        return

    # ---------- ESTADOS SEM ENTRADA DE TEXTO ----------
    await show(user_id, "⚠️ Use os botões para continuar.", None, session)


# ==================== WEBHOOK / STARTUP ====================
async def on_startup(b: Bot):
    await init_db()
    webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"
    await b.set_webhook(webhook_url, drop_pending_updates=True)
    logger.info(f"✅ Webhook configurado: {webhook_url}")


async def on_shutdown(b: Bot):
    try:
        await b.delete_webhook()
    except Exception:
        pass


def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    handler.register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    app.router.add_get("/", lambda r: web.Response(text="ok"))
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
