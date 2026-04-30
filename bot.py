import os
import re
import json
import logging
import subprocess
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import anthropic
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
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
BASE_DIR = Path("C:/Deko_Automatizacion")
PRODUCTOS_DIR = BASE_DIR / "productos"
PIPELINE_SCRIPT = BASE_DIR / "monitor_productos.py"

# ── Credenciales (variables de entorno) ────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CHAT_ID = os.getenv("CHAT_ID")
SHOPIFY_TOKEN = os.getenv("SHOPIFY_TOKEN")

# Validación con logs para debugging
if not BOT_TOKEN:
    print("BOT_TOKEN vacio")
if not ANTHROPIC_API_KEY:
    print("ANTHROPIC_API_KEY vacio")
if not CHAT_ID:
    print("CHAT_ID vacio")
if not SHOPIFY_TOKEN:
    print("SHOPIFY_TOKEN vacio")

# Si al menos BOT_TOKEN y ANTHROPIC_API_KEY existen, continúa
if not (BOT_TOKEN and ANTHROPIC_API_KEY):
    raise ValueError("Faltan BOT_TOKEN o ANTHROPIC_API_KEY")

# ── Estados de la conversación ─────────────────────────────────────────────────
(
    NOMBRE,
    CANTIDAD_COLORES,
    DATOS,
    FOTOS_COLOR,
    FOTO_REFERENCIA,
    CONFIRMACION,
) = range(6)

# Campos requeridos en PROCESAR.txt (orden canonical)
CAMPOS_REQUERIDOS = [
    "material",
    "altura_suela",
    "plantilla_confort",
    "ocasion",
    "precio",
    "tipo_calzado",
    "proveedor",
    "cordon",
    "tallas",
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_datos(texto: str) -> tuple[dict, list]:
    """
    Parsea un bloque de texto con formato  campo: valor  o  campo=valor.
    Devuelve (datos_dict, campos_faltantes).
    """
    datos: dict[str, str] = {}
    for campo in CAMPOS_REQUERIDOS:
        match = re.search(rf"(?i)\b{campo}\s*[:=]\s*(.+)", texto)
        if match:
            datos[campo] = match.group(1).strip()
    faltantes = [c for c in CAMPOS_REQUERIDOS if c not in datos]
    return datos, faltantes


async def _validar_con_claude(datos: dict) -> tuple[bool, list[str], list[str]]:
    """
    Envía los datos al modelo claude-haiku-4-5-20251001 para validación.
    Devuelve (valido, errores, sugerencias).
    """
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    datos_str = "\n".join(f"{k}: {v}" for k, v in datos.items())

    prompt = f"""Eres un validador de datos para un catálogo de calzado.
Valida los siguientes datos del producto y responde SOLO con JSON válido, sin texto adicional.

Estructura de respuesta:
{{
  "valido": true | false,
  "errores": ["lista de errores, vacía si válido"],
  "sugerencias": ["sugerencias opcionales de mejora"]
}}

Reglas de validación:
- precio: número positivo (sin símbolo de moneda)
- tallas: acepta DOS formatos, ambos son VÁLIDOS:
    Formato 1 - Rango con guión: "35-40" significa tallas del 35 al 40 inclusive
    Formato 2 - Lista con comas: "35,36,37,38" lista específica de tallas
    IMPORTANTE: NO rechaces el formato con guión. "35-40" es completamente válido.
- altura_suela: incluye unidad cm o mm (ej: 3cm)
- plantilla_confort y cordon: si | no
- ocasion: casual | formal | deportivo | elegante | trabajo
- todos los campos son obligatorios y no vacíos

Datos a validar:
{datos_str}"""

    msg = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = msg.content[0].text.strip()
    # Extraer JSON aunque venga envuelto en ```json ... ```
    json_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not json_match:
        return False, ["No se pudo interpretar la respuesta de validación."], []

    result = json.loads(json_match.group())
    valido = bool(result.get("valido", False))
    errores = result.get("errores", [])
    sugerencias = result.get("sugerencias", [])
    return valido, errores, sugerencias


def _crear_carpeta_y_txt(nombre: str, datos: dict, colores: list[str]) -> Path:
    """Crea la carpeta del producto y escribe PROCESAR.txt."""
    carpeta = PRODUCTOS_DIR / nombre
    carpeta.mkdir(parents=True, exist_ok=True)

    colores_str = ",".join(colores)
    lineas = [f"nombre={nombre}"]
    for campo in CAMPOS_REQUERIDOS:
        lineas.append(f"{campo}={datos[campo]}")
    lineas.append(f"colores={colores_str}")

    (carpeta / "PROCESAR.txt").write_text("\n".join(lineas) + "\n", encoding="utf-8")
    return carpeta


def _construir_resumen(ud: dict) -> str:
    nombre = ud["nombre"]
    datos = ud["datos"]
    colores = ud["colores"]

    lineas = [
        "── RESUMEN DEL ESTILO ──",
        f"Nombre: {nombre}",
        f"Colores: {', '.join(colores)}",
        "",
        "Datos del producto:",
    ]
    for k, v in datos.items():
        lineas.append(f"  {k}: {v}")
    lineas += [
        "",
        f"Fotos de color: {len(colores)}",
        "Foto referencia Pinterest: 1",
        "",
        "Confirmas el registro? Responde SI o NO.",
    ]
    return "\n".join(lineas)


# ── Handlers ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Hola! Soy el bot de *Deko Automatización*.\n\n"
        "Vamos a registrar un nuevo estilo.\n\n"
        "Ingresa el *nombre del estilo*:",
        parse_mode="Markdown",
    )
    return NOMBRE


async def recibir_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    nombre = update.message.text.strip().upper().replace(" ", "_")
    if not nombre:
        await update.message.reply_text("El nombre no puede estar vacío. Intenta de nuevo:")
        return NOMBRE

    context.user_data.update(
        nombre=nombre,
        colores=[],
        fotos={},  # color -> file_id
        foto_referencia=None,
        datos={},
    )
    await update.message.reply_text(
        f"Estilo: *{nombre}*\n\n¿Cuántos colores tiene este estilo?",
        parse_mode="Markdown",
    )
    return CANTIDAD_COLORES


async def recibir_cantidad_colores(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texto = update.message.text.strip()
    if not texto.isdigit() or int(texto) < 1:
        await update.message.reply_text("Ingresa un número entero mayor a 0:")
        return CANTIDAD_COLORES

    cantidad = int(texto)
    context.user_data["cantidad_colores"] = cantidad

    formato = "\n".join(f"{c}: " for c in CAMPOS_REQUERIDOS)
    await update.message.reply_text(
        f"Colores: *{cantidad}*\n\n"
        "Ahora ingresa los datos del estilo con este formato:\n\n"
        f"`{formato}`",
        parse_mode="Markdown",
    )
    return DATOS


async def recibir_datos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texto = update.message.text

    datos, faltantes = _parse_datos(texto)
    if faltantes:
        await update.message.reply_text(
            "Faltan los siguientes campos:\n"
            + "\n".join(f"  • {c}" for c in faltantes)
            + "\n\nReenvía el bloque completo."
        )
        return DATOS

    await update.message.reply_text("Validando datos con IA, un momento...")

    try:
        valido, errores, sugerencias = await _validar_con_claude(datos)
    except Exception as exc:
        logger.error("Error llamando a Claude: %s", exc)
        await update.message.reply_text(
            f"Error de validación: `{exc}`\n\nReintenta o usa /cancelar.",
            parse_mode="Markdown",
        )
        return DATOS

    if not valido:
        await update.message.reply_text(
            "Los datos tienen errores:\n"
            + "\n".join(f"  • {e}" for e in errores)
            + "\n\nCorrige y reenvía los datos."
        )
        return DATOS

    context.user_data["datos"] = datos

    cantidad = context.user_data["cantidad_colores"]
    nota_sug = ""
    if sugerencias:
        nota_sug = "\n*Sugerencias:*\n" + "\n".join(f"  • {s}" for s in sugerencias) + "\n"

    await update.message.reply_text(
        f"Datos validados correctamente.\n{nota_sug}\n"
        f"Ahora envía las *{cantidad}* fotos de colores.\n\n"
        "Envía cada foto con el *nombre del color* en el caption.",
        parse_mode="Markdown",
    )
    return FOTOS_COLOR


async def recibir_foto_color(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text(
            "Envía una *foto* con el nombre del color en el caption.",
            parse_mode="Markdown",
        )
        return FOTOS_COLOR

    caption = (update.message.caption or "").strip()
    if not caption:
        await update.message.reply_text(
            "La foto necesita el *nombre del color* en el caption.\n"
            "Reenvía la foto con el color escrito.",
            parse_mode="Markdown",
        )
        return FOTOS_COLOR

    color = caption.upper().replace(" ", "_")
    file_id = update.message.photo[-1].file_id  # mayor resolución

    ud = context.user_data
    if color in ud["fotos"]:
        await update.message.reply_text(
            f"Ya registraste el color *{color}*. Envía uno diferente.",
            parse_mode="Markdown",
        )
        return FOTOS_COLOR

    ud["fotos"][color] = file_id
    ud["colores"].append(color)

    recibidos = len(ud["colores"])
    esperados = ud["cantidad_colores"]

    if recibidos < esperados:
        restantes = esperados - recibidos
        await update.message.reply_text(
            f"Color *{color}* registrado. Faltan *{restantes}* foto(s).\n\n"
            "Envía la siguiente foto con el color en el caption.",
            parse_mode="Markdown",
        )
        return FOTOS_COLOR

    await update.message.reply_text(
        f"Color *{color}* registrado.\n\n"
        "Todas las fotos de colores recibidas!\n\n"
        "Ahora envía la *foto de referencia de Pinterest*.",
        parse_mode="Markdown",
    )
    return FOTO_REFERENCIA


async def recibir_foto_referencia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Envía la foto de referencia de Pinterest.")
        return FOTO_REFERENCIA

    context.user_data["foto_referencia"] = update.message.photo[-1].file_id

    resumen = _construir_resumen(context.user_data)
    keyboard = ReplyKeyboardMarkup([["SI", "NO"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(resumen, reply_markup=keyboard)
    return CONFIRMACION


async def confirmar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    respuesta = update.message.text.strip().upper()

    if respuesta == "NO":
        await update.message.reply_text(
            "Registro cancelado.\n\nUsa /nuevo para empezar de nuevo.",
            reply_markup=ReplyKeyboardRemove(),
        )
        context.user_data.clear()
        return ConversationHandler.END

    if respuesta != "SI":
        await update.message.reply_text("Responde *SI* o *NO*.", parse_mode="Markdown")
        return CONFIRMACION

    await update.message.reply_text("Procesando...", reply_markup=ReplyKeyboardRemove())

    ud = context.user_data
    nombre = ud["nombre"]
    datos = ud["datos"]
    colores = ud["colores"]
    fotos: dict = ud["fotos"]
    foto_ref_id: str = ud["foto_referencia"]
    proveedor = datos.get("proveedor", "PROV").upper().replace(" ", "_")

    try:
        # 1. Crear carpeta y PROCESAR.txt
        carpeta = _crear_carpeta_y_txt(nombre, datos, colores)

        # 2. Guardar fotos de colores → [COLOR]_1.jpg
        for color, file_id in fotos.items():
            tg_file = await context.bot.get_file(file_id)
            await tg_file.download_to_drive(str(carpeta / f"{color}_1.jpg"))

        # 3. Guardar foto referencia Pinterest
        ref_file = await context.bot.get_file(foto_ref_id)
        await ref_file.download_to_drive(str(carpeta / "referencia_pinterest.jpg"))

        # 4. Disparar pipeline monitor_productos.py
        proc = subprocess.run(
            ["python", str(PIPELINE_SCRIPT), nombre],
            capture_output=True,
            text=True,
            timeout=300,
        )

        if proc.returncode == 0:
            await update.message.reply_text(
                f"Estilo *{nombre}* procesado exitosamente!\n\n"
                f"Carpeta: `{carpeta}`\n"
                f"Colores: {', '.join(colores)}\n"
                f"Proveedor: {proveedor}\n\n"
                "El pipeline finalizó sin errores.",
                parse_mode="Markdown",
            )
        else:
            stderr_preview = (proc.stderr or "Sin detalle")[:600]
            await update.message.reply_text(
                f"Estilo *{nombre}* guardado, pero el pipeline reportó errores:\n\n"
                f"`{stderr_preview}`\n\n"
                f"Archivos en: `{carpeta}`",
                parse_mode="Markdown",
            )

    except subprocess.TimeoutExpired:
        await update.message.reply_text(
            f"El pipeline tardó demasiado (>5 min).\n"
            f"Archivos guardados en: `{carpeta}`\n"
            "Ejecuta el pipeline manualmente.",
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.error("Error procesando %s: %s", nombre, exc)
        await update.message.reply_text(
            f"Error al procesar el estilo:\n`{exc}`\n\nContacta al administrador.",
            parse_mode="Markdown",
        )

    context.user_data.clear()
    return ConversationHandler.END


async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Operación cancelada.\n\nUsa /nuevo para empezar un nuevo registro.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Error no controlado:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "Ocurrió un error inesperado. Usa /cancelar y luego /nuevo para reintentar."
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
            NOMBRE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_nombre)
            ],
            CANTIDAD_COLORES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_cantidad_colores)
            ],
            DATOS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_datos)
            ],
            FOTOS_COLOR: [
                MessageHandler(filters.PHOTO, recibir_foto_color)
            ],
            FOTO_REFERENCIA: [
                MessageHandler(filters.PHOTO, recibir_foto_referencia)
            ],
            CONFIRMACION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, confirmar)
            ],
        },
        fallbacks=[CommandHandler("cancelar", cmd_cancelar)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_error_handler(error_handler)

    port = int(os.getenv("PORT", "8000"))
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
