import os
import re
import sys
import json
import logging
import subprocess
import threading
import unicodedata
import urllib.request
from pathlib import Path
from datetime import date, timedelta

from dotenv import load_dotenv
load_dotenv()

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Rutas base ─────────────────────────────────────────────────────────────────
PRODUCTOS_DIR = Path(os.getenv("PRODUCTOS_DIR", "/app/data/productos"))
PIPELINE_SCRIPT = Path("/app/pipeline.py")

# ── Credenciales ───────────────────────────────────────────────────────────────
BOT_TOKEN         = os.getenv("BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CHAT_ID           = os.getenv("CHAT_ID")
SHOPIFY_TOKEN     = os.getenv("SHOPIFY_TOKEN")
SHOPIFY_SHOP      = os.getenv("SHOPIFY_SHOP", "")

if not BOT_TOKEN:         print("BOT_TOKEN vacio")
if not ANTHROPIC_API_KEY: print("ANTHROPIC_API_KEY vacio")
if not CHAT_ID:           print("CHAT_ID vacio")
if not SHOPIFY_TOKEN:     print("SHOPIFY_TOKEN vacio")

if not (BOT_TOKEN and ANTHROPIC_API_KEY):
    raise ValueError("Faltan BOT_TOKEN o ANTHROPIC_API_KEY")

# ── Estados ────────────────────────────────────────────────────────────────────
(
    FOTOS,
    SPEC_LINEA,
    SPEC_TIPO,
    SPEC_MATERIAL,
    SPEC_OCASION,
    SPEC_CIERRE,
    SPEC_ALTURA,
    SPEC_TALLAS,
    SPEC_DIAS,
    SPEC_PRECIO,
    SPEC_PROVEEDOR,
    CONFIRMACION,
) = range(12)


# ── Teclados inline ────────────────────────────────────────────────────────────

def _kb(prefix: str, opciones: list, cols: int = 3) -> InlineKeyboardMarkup:
    rows, row = [], []
    for op in opciones:
        row.append(InlineKeyboardButton(op, callback_data=f"{prefix}:{op}"))
        if len(row) == cols:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _kb_listo() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Listo", callback_data="fotos_listo")]
    ])


def _kb_confirmar() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Iniciar pipeline", callback_data="confirmar")],
        [InlineKeyboardButton("❌ Cancelar",          callback_data="cancelar_conf")],
    ])


def _kb_qa(nombre_color: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1️⃣", callback_data=f"qa:{nombre_color}:1"),
            InlineKeyboardButton("2️⃣", callback_data=f"qa:{nombre_color}:2"),
            InlineKeyboardButton("3️⃣", callback_data=f"qa:{nombre_color}:3"),
        ],
        [InlineKeyboardButton("🔄 Regenerar", callback_data=f"qa:{nombre_color}:regen")],
    ])


# ── Helper: envío síncrono desde threads ──────────────────────────────────────

def _telegram_send(token: str, chat_id: str, text: str) -> None:
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req  = urllib.request.Request(url, data=body, method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
    except Exception as e:
        logger.error("_telegram_send error: %s", e)


# ── Normalización de captions ──────────────────────────────────────────────────

def _quitar_tildes(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )


def _normalizar_nombre(texto: str) -> str:
    """Estilo: sin tildes, MAYÚSCULAS, espacios normalizados."""
    return _quitar_tildes(" ".join(texto.split())).upper()


def _normalizar_color(texto: str) -> str:
    """Color: sin tildes, Title Case, espacios normalizados."""
    return _quitar_tildes(" ".join(texto.split())).title()


def _detectar_caption(raw: str) -> tuple:
    """
    Devuelve (tipo, valor_normalizado).
    tipo: 'pin' | 'ref' | 'color' | 'vacio'
    """
    texto = " ".join(raw.split())   # colapsar espacios múltiples

    if not texto:
        return "vacio", ""

    # PIN: cualquier capitalización de "pin"
    if texto.upper() == "PIN":
        return "pin", "PIN"

    # REF: acepta REF:, Ref:, ref:, REF : (con espacio antes de los dos puntos)
    m = re.match(r'^[Rr][Ee][Ff]\s*:\s*(.+)$', texto)
    if m:
        return "ref", _normalizar_nombre(m.group(1))

    # Color: cualquier otro texto no vacío
    return "color", _normalizar_color(texto)


def _estado_fotos(ud: dict) -> str:
    nombre   = ud.get("nombre")
    foto_pin = ud.get("foto_pin")
    colores  = ud.get("colores", {})

    lineas = ["📋 Estado actual:"]
    lineas.append(f"  Estilo     : {nombre if nombre else '—'}")
    lineas.append(f"  Pinterest  : {'✓' if foto_pin else '—'}")
    if colores:
        lineas.append(f"  Colores ({len(colores)}): {', '.join(colores.keys())}")
    else:
        lineas.append("  Colores    : —")
    return "\n".join(lineas)


def _condicion_listo(ud: dict) -> bool:
    return bool(ud.get("nombre") and ud.get("foto_pin") and ud.get("colores"))


# ── PASO 1: Fotos una por una ──────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Hola! Soy el bot de Deko Automatizacion.\n\n"
        "Envia las fotos una por una con caption:\n\n"
        "  REF: NOMBRE  →  nombre del estilo  (ej: REF: Lucia)\n"
        "  PIN          →  referencia Pinterest\n"
        "  NEGRO        →  foto de ese color  (ej: Nude, animal print)\n\n"
        "El boton ✅ Listo aparecera cuando tengas minimo\n"
        "1 estilo + 1 PIN + 1 color.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return FOTOS


async def recibir_foto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    if not msg.photo:
        await msg.reply_text("Envia una foto con caption.")
        return FOTOS

    file_id     = msg.photo[-1].file_id
    caption_raw = (msg.caption or "").strip()
    ud          = context.user_data

    if not caption_raw:
        await msg.reply_text(
            "Esta foto no tiene caption.\n\n"
            "Agrega:\n"
            "  REF: NOMBRE  →  nombre del estilo\n"
            "  PIN          →  referencia Pinterest\n"
            "  NEGRO        →  nombre del color"
        )
        return FOTOS

    tipo, valor = _detectar_caption(caption_raw)

    if tipo == "pin":
        ud["foto_pin"] = file_id
        confirmacion   = "📸 Referencia Pinterest registrada"

    elif tipo == "ref":
        ud["nombre"] = valor
        confirmacion  = f"📌 Estilo: {valor}"

    elif tipo == "color":
        colores_ud    = ud.setdefault("colores", {})
        es_duplicado  = valor in colores_ud
        colores_ud[valor] = file_id
        confirmacion  = f"🎨 Color: {valor}"
        if es_duplicado:
            confirmacion += " (actualizado)"

    else:
        await msg.reply_text("Caption no reconocido. Usa REF:, PIN o el nombre del color.")
        return FOTOS

    estado = _estado_fotos(ud)
    listo  = _condicion_listo(ud)

    texto = f"{confirmacion}\n\n{estado}"

    if listo:
        kb = _kb_listo()
    else:
        kb = None
        faltantes = []
        if not ud.get("nombre"):
            faltantes.append("foto REF: NOMBRE")
        if not ud.get("foto_pin"):
            faltantes.append("foto PIN")
        if not ud.get("colores"):
            faltantes.append("al menos 1 color")
        if faltantes:
            texto += "\n\nFalta: " + ", ".join(faltantes)

    await msg.reply_text(texto, reply_markup=kb)
    return FOTOS


async def fotos_listo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)

    ud      = context.user_data
    nombre  = ud.get("nombre", "?")
    colores = ud.get("colores", {})

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=(
            f"Fotos listas\n\n"
            f"Estilo   : {nombre}\n"
            f"Colores  : {', '.join(colores.keys())}\n"
            f"Pinterest: ✓\n\n"
            "Ahora selecciona las especificaciones:"
        ),
    )
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Linea del producto:",
        reply_markup=_kb("spec_linea", ["Mujer", "Hombre", "Nino", "Nina"], cols=4),
    )
    return SPEC_LINEA


# ── PASO 2: Especificaciones con botones inline ────────────────────────────────

async def spec_linea(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    valor = query.data.split(":", 1)[1]
    context.user_data["linea"] = valor
    await query.edit_message_text(f"Linea: {valor} ✓")

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Tipo de calzado:",
        reply_markup=_kb("spec_tipo",
            ["Tenis", "Botines", "Botas", "Sandalias", "Tacones",
             "Mocasines", "Baletas", "Alpargatas", "Plataformas", "Planas"],
            cols=3),
    )
    return SPEC_TIPO


async def spec_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    valor = query.data.split(":", 1)[1]
    context.user_data["tipo_calzado"] = valor
    await query.edit_message_text(f"Tipo: {valor} ✓")

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Material:",
        reply_markup=_kb("spec_material",
            ["Cuero genuino", "Sintetico", "Yute", "Lona", "Tejido"],
            cols=2),
    )
    return SPEC_MATERIAL


async def spec_material(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    valor = query.data.split(":", 1)[1]
    context.user_data["material"] = valor
    await query.edit_message_text(f"Material: {valor} ✓")

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Ocasion de uso:",
        reply_markup=_kb("spec_ocasion", ["Casual", "Formal", "Deportivo"], cols=3),
    )
    return SPEC_OCASION


async def spec_ocasion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    valor = query.data.split(":", 1)[1]
    context.user_data["ocasion"] = valor
    await query.edit_message_text(f"Ocasion: {valor} ✓")

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Tipo de cierre:",
        reply_markup=_kb("spec_cierre",
            ["Cordon", "Velcro", "Hebilla", "Elastico", "Sin cordon"],
            cols=3),
    )
    return SPEC_CIERRE


async def spec_cierre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    valor = query.data.split(":", 1)[1]
    context.user_data["cierre"] = valor
    await query.edit_message_text(f"Cierre: {valor} ✓")

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Altura de suela (ej: 3cm, 1.5cm):",
    )
    return SPEC_ALTURA


async def spec_altura(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texto = update.message.text.strip()
    if not re.search(r'\d', texto):
        await update.message.reply_text("Incluye un numero (ej: 3cm). Intentalo de nuevo:")
        return SPEC_ALTURA

    context.user_data["altura_suela"] = texto
    await update.message.reply_text("Tallas disponibles (ej: 35-40 o 35,36,37,38,39,40):")
    return SPEC_TALLAS


async def spec_tallas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texto = update.message.text.strip()
    if not re.match(r'^\d{2}-\d{2}$|^\d{2}(\s*,\s*\d{2})+$|^\d{2}$', texto):
        await update.message.reply_text(
            "Formato invalido.\n  Rango: 35-40\n  Lista: 35,36,37,38\nIntentalo de nuevo:"
        )
        return SPEC_TALLAS

    context.user_data["tallas"] = texto
    await update.message.reply_text("Dias activo (default 10, escribe el numero):")
    return SPEC_DIAS


async def spec_dias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texto = update.message.text.strip()
    if not texto.isdigit() or int(texto) < 1:
        await update.message.reply_text("Ingresa un numero entero positivo (ej: 10):")
        return SPEC_DIAS

    context.user_data["dias_activo"] = texto
    await update.message.reply_text("Precio (solo el numero, sin simbolo):")
    return SPEC_PRECIO


async def spec_precio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texto = update.message.text.strip()
    if not re.match(r'^\d+(\.\d{1,2})?$', texto):
        await update.message.reply_text("Ingresa solo el numero (ej: 89000):")
        return SPEC_PRECIO

    context.user_data["precio"] = texto
    await update.message.reply_text("Proveedor:")
    return SPEC_PROVEEDOR


async def spec_proveedor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texto = update.message.text.strip()
    if not texto:
        await update.message.reply_text("Ingresa el nombre del proveedor:")
        return SPEC_PROVEEDOR

    context.user_data["proveedor"] = texto.upper().replace(" ", "_")

    await update.message.reply_text(
        _construir_resumen(context.user_data),
        reply_markup=_kb_confirmar(),
    )
    return CONFIRMACION


def _construir_resumen(ud: dict) -> str:
    colores = list(ud.get("colores", {}).keys())
    return (
        "── RESUMEN DEL ESTILO ──\n\n"
        f"Nombre    : {ud.get('nombre', '?')}\n"
        f"Colores   : {', '.join(colores)}\n\n"
        f"Linea     : {ud.get('linea', '?')}\n"
        f"Tipo      : {ud.get('tipo_calzado', '?')}\n"
        f"Material  : {ud.get('material', '?')}\n"
        f"Ocasion   : {ud.get('ocasion', '?')}\n"
        f"Cierre    : {ud.get('cierre', '?')}\n"
        f"Altura    : {ud.get('altura_suela', '?')}\n"
        f"Tallas    : {ud.get('tallas', '?')}\n"
        f"Dias      : {ud.get('dias_activo', '10')}\n"
        f"Precio    : ${ud.get('precio', '?')}\n"
        f"Proveedor : {ud.get('proveedor', '?')}\n\n"
        "Todo correcto?"
    )


# ── PASO 3 y 4: Confirmacion y Pipeline ───────────────────────────────────────

async def confirmar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "cancelar_conf":
        await query.edit_message_text("Cancelado. Usa /nuevo para empezar.")
        context.user_data.clear()
        return ConversationHandler.END

    await query.edit_message_text("Procesando...")

    ud      = context.user_data
    nombre  = ud["nombre"]
    colores = ud["colores"]
    pin_id  = ud["foto_pin"]
    chat_id = query.message.chat_id

    try:
        carpeta = PRODUCTOS_DIR / nombre
        carpeta.mkdir(parents=True, exist_ok=True)

        for color, file_id in colores.items():
            tg_file = await context.bot.get_file(file_id)
            await tg_file.download_to_drive(str(carpeta / f"{nombre}_{color}_1.jpg"))

        pin_file = await context.bot.get_file(pin_id)
        await pin_file.download_to_drive(str(carpeta / "referencia_pinterest.jpg"))

        _crear_procesar_txt(nombre, ud, list(colores.keys()), carpeta)

        threading.Thread(
            target=_run_pipeline,
            args=(nombre, str(carpeta), CHAT_ID, BOT_TOKEN),
            daemon=True,
        ).start()

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"Pipeline iniciado para {nombre}\n"
                f"Colores: {', '.join(colores.keys())}\n\n"
                "Te notifico cuando este listo."
            ),
        )

    except Exception as exc:
        logger.error("Error procesando %s: %s", nombre, exc, exc_info=True)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Error al procesar {nombre}:\n{exc}\n\nContacta al administrador.",
        )

    context.user_data.clear()
    return ConversationHandler.END


def _crear_procesar_txt(nombre: str, ud: dict, colores: list, carpeta: Path) -> None:
    lineas = [
        f"nombre={nombre}",
        f"linea={ud.get('linea', '')}",
        f"tipo_calzado={ud.get('tipo_calzado', '')}",
        f"material={ud.get('material', '')}",
        f"ocasion={ud.get('ocasion', '')}",
        f"cierre={ud.get('cierre', '')}",
        f"altura_suela={ud.get('altura_suela', '')}",
        f"tallas={ud.get('tallas', '')}",
        f"dias_activo={ud.get('dias_activo', '10')}",
        f"precio={ud.get('precio', '')}",
        f"proveedor={ud.get('proveedor', '')}",
        f"colores={','.join(colores)}",
    ]
    (carpeta / "PROCESAR.txt").write_text("\n".join(lineas) + "\n", encoding="utf-8")


def _run_pipeline(nombre: str, carpeta: str, chat_id: str, token: str) -> None:
    try:
        proc = subprocess.run(
            [sys.executable, str(PIPELINE_SCRIPT), nombre],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.stdout:
            logger.info("Pipeline stdout [%s]:\n%s", nombre, proc.stdout[:2000])
        if proc.stderr:
            logger.error("Pipeline stderr [%s]:\n%s", nombre, proc.stderr[:2000])
        if proc.returncode != 0:
            _telegram_send(token, chat_id,
                f"❌ Error en pipeline {nombre}:\n{proc.stderr[-500:]}")
    except subprocess.TimeoutExpired:
        _telegram_send(token, chat_id,
            f"⚠️ Pipeline {nombre} excedio 10 minutos. Revisar manualmente.")


# ── QA: seleccion de imagen (standalone, fuera del ConversationHandler) ────────

async def qa_seleccion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    partes = query.data.split(":")
    if len(partes) < 3:
        return

    nombre_color = partes[1]
    eleccion     = partes[2]

    choice_file = PRODUCTOS_DIR / nombre_color / "qa_choice.txt"
    choice_file.parent.mkdir(parents=True, exist_ok=True)
    choice_file.write_text(eleccion, encoding="utf-8")

    if eleccion == "regen":
        await query.edit_message_caption(caption="🔄 Regenerando imagen...")
    else:
        await query.edit_message_caption(caption=f"✅ Version {eleccion} seleccionada.")


# ── /cancelar ─────────────────────────────────────────────────────────────────

async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Operacion cancelada. Usa /nuevo para empezar.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ── Shopify helpers ────────────────────────────────────────────────────────────

def _shopify_api(method, endpoint, data=None):
    url  = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/{endpoint}"
    body = json.dumps(data).encode() if data else None
    req  = urllib.request.Request(url, data=body, method=method, headers={
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()}")


def _shopify_buscar_productos(nombre_upper):
    try:
        r = _shopify_api("GET", "products.json?limit=250&fields=id,title,status")
    except RuntimeError:
        return []
    return [
        p for p in r.get("products", [])
        if p["title"].upper() == nombre_upper
        or p["title"].upper().startswith(nombre_upper + " - ")
    ]


def _shopify_escribir_metafields(product_id, fecha_pub, dias_activo, activo=True):
    try:
        r = _shopify_api("GET", f"products/{product_id}/metafields.json?namespace=deko")
        existentes = {m["key"]: m for m in r.get("metafields", [])}
    except RuntimeError:
        existentes = {}

    updates = {
        "fecha_pub":   (fecha_pub, "single_line_text_field"),
        "dias_activo": (str(dias_activo), "number_integer"),
        "activo":      ("true" if activo else "false", "single_line_text_field"),
    }
    for key, (value, type_) in updates.items():
        if key in existentes:
            mf_id = existentes[key]["id"]
            try:
                _shopify_api("PUT", f"metafields/{mf_id}.json",
                             {"metafield": {"id": mf_id, "value": value, "type": type_}})
            except RuntimeError:
                pass
        else:
            try:
                _shopify_api("POST", f"products/{product_id}/metafields.json", {
                    "metafield": {"namespace": "deko", "key": key,
                                  "value": value, "type": type_}
                })
            except RuntimeError:
                pass


# ── /reactivar ────────────────────────────────────────────────────────────────

async def cmd_reactivar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Uso: /reactivar NOMBRE DIAS\nEjemplo: /reactivar ALMA 10"
        )
        return

    nombre = args[0].upper()
    try:
        dias = int(args[1])
        if dias < 1:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("DIAS debe ser un numero entero positivo.")
        return

    if not SHOPIFY_TOKEN or not SHOPIFY_SHOP:
        await update.message.reply_text("SHOPIFY_TOKEN o SHOPIFY_SHOP no configurados.")
        return

    productos = _shopify_buscar_productos(nombre)
    if not productos:
        await update.message.reply_text(
            f"No encontre productos en Shopify para '{nombre}'.\n"
            f"Verifica que el nombre sea exacto (ej: ALMA, Salome)."
        )
        return

    errores = []
    for p in productos:
        try:
            _shopify_api("PUT", f"products/{p['id']}.json",
                         {"product": {"id": p["id"], "status": "active"}})
        except RuntimeError as e:
            errores.append(f"{p['title']}: {e}")

    if errores:
        await update.message.reply_text(
            f"Errores al reactivar {nombre}:\n" + "\n".join(errores)
        )
        return

    hoy        = date.today()
    fecha_venc = hoy + timedelta(days=dias)

    for p in productos:
        _shopify_escribir_metafields(p["id"], hoy.isoformat(), dias, activo=True)

    await update.message.reply_text(
        f"✅ {nombre} reactivado por {dias} dias ({len(productos)} productos).\n"
        f"Se desactivara el {fecha_venc.strftime('%d/%m/%Y')}."
    )


# ── Error handler ──────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Error no controlado:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "Ocurrio un error inesperado. Usa /cancelar y luego /nuevo para reintentar."
        )


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("nuevo", cmd_start),
        ],
        states={
            FOTOS: [
                MessageHandler(filters.PHOTO, recibir_foto),
                CallbackQueryHandler(fotos_listo, pattern="^fotos_listo$"),
            ],
            SPEC_LINEA: [
                CallbackQueryHandler(spec_linea, pattern="^spec_linea:"),
            ],
            SPEC_TIPO: [
                CallbackQueryHandler(spec_tipo, pattern="^spec_tipo:"),
            ],
            SPEC_MATERIAL: [
                CallbackQueryHandler(spec_material, pattern="^spec_material:"),
            ],
            SPEC_OCASION: [
                CallbackQueryHandler(spec_ocasion, pattern="^spec_ocasion:"),
            ],
            SPEC_CIERRE: [
                CallbackQueryHandler(spec_cierre, pattern="^spec_cierre:"),
            ],
            SPEC_ALTURA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, spec_altura),
            ],
            SPEC_TALLAS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, spec_tallas),
            ],
            SPEC_DIAS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, spec_dias),
            ],
            SPEC_PRECIO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, spec_precio),
            ],
            SPEC_PROVEEDOR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, spec_proveedor),
            ],
            CONFIRMACION: [
                CallbackQueryHandler(confirmar, pattern="^(confirmar|cancelar_conf)$"),
            ],
        },
        fallbacks=[CommandHandler("cancelar", cmd_cancelar)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("reactivar", cmd_reactivar))
    app.add_handler(CallbackQueryHandler(qa_seleccion, pattern="^qa:"))
    app.add_error_handler(error_handler)

    port        = int(os.getenv("PORT", "8000"))
    webhook_url = "https://web-production-f7d03.up.railway.app/webhook"

    logger.info("Bot Deko iniciado en modo webhook (puerto %s).", port)
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path="webhook",
        webhook_url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
