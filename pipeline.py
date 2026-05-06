import os
import sys
import json
import re
import time
import math
import base64
import urllib.request
import urllib.error
import requests
from io import BytesIO
from pathlib import Path

import google.genai as genai
import anthropic
import PIL.Image
import PIL.ImageChops
import PIL.ImageDraw
import PIL.ImageFont
import PIL.ImageFilter

# ── Detección de entorno ──────────────────────────────────────────────────────
EN_RAILWAY = os.getenv("RAILWAY_ENVIRONMENT") is not None

if EN_RAILWAY:
    BASE_DIR      = "/app/data"
    GEMINI_KEY    = os.getenv("GEMINI_KEY", "")
    # Acepta ANTHROPIC_KEY o ANTHROPIC_API_KEY (mismo token, distintos nombres)
    ANTHROPIC_KEY = os.getenv("ANTHROPIC_KEY") or os.getenv("ANTHROPIC_API_KEY", "")
    SHOPIFY_TOKEN = os.getenv("SHOPIFY_TOKEN", "")
    SHOPIFY_SHOP  = os.getenv("SHOPIFY_SHOP", "")
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN", "")
    TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT") or os.getenv("CHAT_ID", "")
else:
    BASE_DIR = "C:/Deko_Automatizacion"
    _creds_path = Path(BASE_DIR) / "credentials.json"
    _creds = json.loads(_creds_path.read_text(encoding="utf-8")) if _creds_path.exists() else {}
    GEMINI_KEY    = _creds.get("GEMINI_KEY",      os.getenv("GEMINI_KEY", ""))
    ANTHROPIC_KEY = _creds.get("ANTHROPIC_API_KEY", os.getenv("ANTHROPIC_KEY", ""))

    _shopify_path = Path(BASE_DIR) / "shopify_config.json"
    _scfg = json.loads(_shopify_path.read_text(encoding="utf-8")) if _shopify_path.exists() else {}
    SHOPIFY_TOKEN = _scfg.get("access_token", "")
    SHOPIFY_SHOP  = _scfg.get("shop_name", "")

    TELEGRAM_TOKEN = _creds.get("telegram_bot_token", os.getenv("TELEGRAM_TOKEN", ""))
    TELEGRAM_CHAT  = str(_creds.get("telegram_chat_id", os.getenv("TELEGRAM_CHAT", "")))

# Derivadas comunes
SHOPIFY_BASE_URL = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01" if SHOPIFY_SHOP else ""
PRODUCTOS_DIR    = Path(BASE_DIR) / "productos"

# Fuentes: rutas según SO
COLLAGE_FONT_PATHS = (
    [
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ] if EN_RAILWAY else [
        "C:/Windows/Fonts/georgiab.ttf",
        "C:/Windows/Fonts/calibrib.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
)

# ── Clientes API ──────────────────────────────────────────────────────────────
gemini_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

if not GEMINI_KEY:
    print("ERROR: GEMINI_KEY no configurado — no se generaran imagenes")
if not ANTHROPIC_KEY:
    print("ERROR: ANTHROPIC_KEY / ANTHROPIC_API_KEY no configurado — no se generaran descripciones ni analisis")
if not SHOPIFY_TOKEN:
    print("AVISO: SHOPIFY_TOKEN no configurado — no se publicara en Shopify")

_gemini_calls = 0  # contador de llamadas a Gemini por producto

# ── Constantes ────────────────────────────────────────────────────────────────
SHOPIFY_TALLAS = [str(t) for t in range(35, 43)]
INFO_FIELDS    = {"material", "altura_suela", "plantilla_confort", "ocasion", "tipo_calzado", "proveedor"}

COLLAGE_HEADER    = 110
COLLAGE_TARGET_W  = 560
COLLAGE_FONT_SIZE = 92
COLLAGE_RATIO     = 5 / 4
COLLAGE_GOLD      = (201, 168, 76)
COLLAGE_HEADER_BG = (245, 240, 235)

# ── Prompts ───────────────────────────────────────────────────────────────────
PROMPT_MAESTRO = """Analiza la imagen y genera PROMPT FINAL para Nanobanana.
Imagen de cuerpo completo, mostrando la figura entera de la modelo desde la cabeza hasta los pies.
NO describir el zapato.

PROMPT PARA GENERAR IMAGEN
Imagen de cuerpo completo, mostrando la figura entera de la modelo desde la cabeza hasta los pies

Ambiente general:
Fondo y entorno:
Composicion de camara: Encuadre vertical, cuerpo completo desde cabeza hasta pies, perfil lateral, ambos zapatos visibles, espacio inferior amplio
Posicion del cuerpo:
Vestuario visible:
Piso:
Iluminacion:
INSTRUCCION: El calzado sera proporcionado posteriormente. NO describir, NO modificar."""

PROMPT_MAESTRO_CLOSE = """Analiza la imagen y genera PROMPT FINAL para Nanobanana - PLANO CERRADO.
Mantener el mismo ambiente, fondo, entorno, piso e iluminacion de la escena pero con encuadre cerrado en los pies.
NO describir el zapato.

PROMPT PARA GENERAR IMAGEN - PLANO CERRADO

Ambiente general:
Fondo y entorno:
Composicion de camara: Plano medio-cerrado desde debajo de la pantorrilla hacia abajo, camara a nivel del suelo ligeramente elevada, perfil lateral 100%, ambos zapatos visibles en todo momento, espacio inferior amplio en la composicion
Posicion del cuerpo: Modelo en posicion completamente lateral perfil 100%, solo visible desde debajo de la pantorrilla hacia abajo, perfil lateral completo del calzado visible
Vestuario visible:
Piso:
Iluminacion:
No incluir bolsos, carteras ni accesorios de mano en la imagen.
INSTRUCCION: El calzado sera proporcionado posteriormente. NO describir, NO modificar."""

CRITERIOS_QA = """Analiza estas DOS imagenes:
IMAGEN 1: Zapato ORIGINAL (referencia)
IMAGEN 2: Imagen generada

Responde SOLO JSON sin markdown:
{
  "aprobada": true,
  "criterios": {"zapato_visible": true, "plano_cerrado": true, "zapato_fiel_al_original": true},
  "motivo_rechazo": "",
  "zapato_original": {
    "tipo_calzado": "bota / botin / sandalia / baleta / mocasin / taco / plataforma / deportivo / otro",
    "tipo_suela": "plataforma / cuna / plana / taco / taco fino / taco bloque",
    "tipo_cierre": "sin cierre / hebilla / cremallera / elastico / lacado / velcro",
    "detalles_decorativos": "descripcion breve de costuras texturas adornos acabados"
  }
}

CRITERIO MAS IMPORTANTE: zapato_fiel_al_original
Extrae zapato_original SOLO desde IMAGEN 1."""

PROMPT_WEB_LATERAL = """LIMPIEZA, CENTRADO Y ORIENTACION FIJA (CATALOGO DEKO)
Objetivo: Eliminar fondo, centrar y alinear el zapato en vista lateral, manteniendo todos los detalles originales y una orientacion fija de izquierda a derecha (punta hacia la derecha).
Instrucciones:
No incluir letras en ninguna parte del zapato.
No modificar el zapato en ningun aspecto.
Conservar exactamente los materiales, texturas, costuras, colores, proporciones y brillos.
Orientacion fija: punta hacia la derecha, talon hacia la izquierda.
Fondo blanco puro #FFFFFF.
Sombra base muy suave debajo del zapato.
El zapato centrado y ocupando 70% del ancho.
Margenes blancos uniformes 15% superior e inferior.
Imagen de catalogo profesional tipo e-commerce."""

PROMPT_WEB_DIAGONAL = PROMPT_WEB_LATERAL + "\nMostrar los 2 zapatos en vista diagonal, uno detras del otro, perspectiva 3/4."

CRITERIOS_QA_WEB = """Analiza estas DOS imagenes:
IMAGEN 1: Zapato ORIGINAL
IMAGEN 2: Imagen generada

Responde SOLO JSON sin markdown:
{"aprobada": true, "criterios": {"zapato_fiel_al_original": true, "fondo_blanco": true, "zapato_centrado": true}, "motivo_rechazo": ""}

CRITERIOS:
- zapato_fiel_al_original: el zapato mantiene exactamente materiales, texturas, colores y proporciones del original
- fondo_blanco: el fondo es blanco puro sin elementos adicionales
- zapato_centrado: el zapato esta centrado en la imagen"""

PROMPT_VISION_ZAPATO = """Analiza esta imagen de zapato y responde SOLO JSON sin markdown:
{
  "tipo_calzado": "una sola categoria: bota / botin / sandalia / baleta / mocasin / taco / plataforma / deportivo / otro",
  "caracteristicas": "descripcion breve de caracteristicas visuales: forma, terminados, materiales visibles, detalles decorativos, tipo de cierre, cordon si/no"
}"""

PROMPT_SHOPIFY = """Eres un experto en descripciones de productos de moda para Shopify. Genera una descripcion profesional, persuasiva y optimizada para SEO en espanol.

Datos del producto:
- Material: {material}
- Altura suela: {altura_suela}
- Plantilla confort: {plantilla_confort}
- Ocasion: {ocasion}
- Precio: ${precio}
- Tipo de calzado: {tipo_calzado}
- Proveedor: {proveedor}

Caracteristicas del zapato (de la imagen): {caracteristicas}
Colores disponibles: {colores}
Tallas: 35 a 42

Genera una descripcion que incluya beneficios, materiales, comodidad, ocasion de uso. Maximo 250 palabras. Optimiza para conversion."""


# ── Análisis de referencia ────────────────────────────────────────────────────
def _analizar_con_prompt(referencia_path, prompt_template, label):
    print(f"  Analizando referencia ({label})...")
    ext = referencia_path.suffix.lower().replace(".", "")
    media_type = "image/png" if ext == "png" else "image/jpeg"
    img_b64 = base64.standard_b64encode(open(referencia_path, "rb").read()).decode()
    response = claude_client.messages.create(
        model="claude-opus-4-6", max_tokens=2000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
            {"type": "text", "text": prompt_template}
        ]}]
    )
    return response.content[0].text

def analizar_referencia(referencia_path):
    return _analizar_con_prompt(referencia_path, PROMPT_MAESTRO, "cuerpo completo")

def analizar_referencia_close(referencia_path):
    return _analizar_con_prompt(referencia_path, PROMPT_MAESTRO_CLOSE, "plano cerrado")


# ── Generación de imágenes ────────────────────────────────────────────────────
def generar_imagen(prompt, zapato_img):
    global _gemini_calls
    if gemini_client is None:
        raise RuntimeError("GEMINI_KEY no configurado")
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash-image", contents=[prompt, zapato_img])
    _gemini_calls += 1
    for part in response.candidates[0].content.parts:
        if hasattr(part, "inline_data") and part.inline_data:
            img_bytes = part.inline_data.data
            if isinstance(img_bytes, str): img_bytes = base64.b64decode(img_bytes)
            return img_bytes
    return None

def _leer_caracteristicas(producto_dir):
    path = producto_dir / "caracteristicas.txt"
    if not path.exists():
        return {}
    datos = {}
    for linea in open(path, encoding="utf-8"):
        if ":" in linea:
            k, _, v = linea.partition(":")
            datos[k.strip().lower()] = v.strip()
    return datos

def _guardar_caracteristicas(producto_dir, zapato_data):
    path = producto_dir / "caracteristicas.txt"
    if path.exists():
        return
    lineas = [
        f"tipo_calzado: {zapato_data.get('tipo_calzado', '')}",
        f"tipo_suela: {zapato_data.get('tipo_suela', '')}",
        f"tipo_cierre: {zapato_data.get('tipo_cierre', '')}",
        f"detalles_decorativos: {zapato_data.get('detalles_decorativos', '')}",
    ]
    open(path, "w", encoding="utf-8").write("\n".join(lineas) + "\n")
    print("  caracteristicas.txt guardado")

def verificar_imagen(original_path, img_bytes):
    ext = original_path.suffix.lower().replace(".", "")
    media_type = "image/png" if ext == "png" else "image/jpeg"
    original_b64 = base64.standard_b64encode(open(original_path, "rb").read()).decode()
    generada_b64 = base64.standard_b64encode(img_bytes).decode()
    response = claude_client.messages.create(
        model="claude-opus-4-6", max_tokens=800,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": original_b64}},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": generada_b64}},
            {"type": "text", "text": CRITERIOS_QA}
        ]}]
    )
    texto = response.content[0].text
    return json.loads(texto[texto.find("{"):texto.rfind("}")+1])

def verificar_web(original_path, img_bytes):
    ext = original_path.suffix.lower().replace(".", "")
    media_type = "image/png" if ext == "png" else "image/jpeg"
    original_b64 = base64.standard_b64encode(open(original_path, "rb").read()).decode()
    generada_b64 = base64.standard_b64encode(img_bytes).decode()
    response = claude_client.messages.create(
        model="claude-opus-4-6", max_tokens=500,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": original_b64}},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": generada_b64}},
            {"type": "text", "text": CRITERIOS_QA_WEB}
        ]}]
    )
    texto = response.content[0].text
    return json.loads(texto[texto.find("{"):texto.rfind("}")+1])

def _generar_variante(prompt, zapato_img, archivo_original, output_dir, nombre_base, sufijo):
    output = output_dir / f"{nombre_base}{sufijo}.jpg"
    if output.exists():
        print(f"  [{sufijo}] Ya existe, saltando: {output.name}")
        return
    img_bytes = None
    aprobada  = False
    for intento in range(1, 4):
        print(f"  [{sufijo}] Intento {intento}/3...")
        try:
            img_bytes = generar_imagen(prompt, zapato_img)
            if not img_bytes:
                continue
            qa = verificar_imagen(archivo_original, img_bytes)
            c  = qa.get("criterios", {})
            aprobada = qa.get("aprobada", False)
            print(f"  [{sufijo}] Zapato:{c.get('zapato_visible')} Plano:{c.get('plano_cerrado')} "
                  f"Fiel:{c.get('zapato_fiel_al_original')}")
            if aprobada:
                output = output_dir / f"{nombre_base}{sufijo}.jpg"
                open(output, "wb").write(img_bytes)
                print(f"  [{sufijo}] APROBADA: {output.name}")
                zapato_data = qa.get("zapato_original", {})
                if zapato_data:
                    _guardar_caracteristicas(output_dir.parent, zapato_data)
                break
            else:
                print(f"  [QA] RECHAZADA {sufijo}: {qa}")
                _telegram_send(f"⚠️ QA rechazó imagen {sufijo}:\n{qa.get('motivo_rechazo', 'Sin motivo')}")
        except Exception as e:
            print(f"  [{sufijo}] Error: {e}")
    if not aprobada and img_bytes:
        output = output_dir / f"{nombre_base}{sufijo}_REVISAR.jpg"
        open(output, "wb").write(img_bytes)
        print(f"  [{sufijo}] REVISAR: {output.name}")


# ── Collage ───────────────────────────────────────────────────────────────────
def _apunta_izquierda(img):
    gray  = img.convert("L").filter(PIL.ImageFilter.GaussianBlur(radius=3))
    edges = gray.filter(PIL.ImageFilter.FIND_EDGES)
    w, h  = edges.size
    pixels = edges.load()
    left_sum = right_sum = 0
    mid = w // 2
    for y in range(0, h, 5):
        for x in range(0, w, 5):
            v = pixels[x, y]
            if x < mid: left_sum += v
            else:        right_sum += v
    return left_sum > right_sum

def _fill_cell(img, cell_w, cell_h):
    iw, ih = img.size
    scale  = max(cell_w / iw, cell_h / ih)
    new_w, new_h = round(iw * scale), round(ih * scale)
    img_s = img.resize((new_w, new_h), PIL.Image.LANCZOS)
    left  = (new_w - cell_w) // 2
    top   = (new_h - cell_h) // 2
    return img_s.crop((left, top, left + cell_w, top + cell_h))

def _estimar_cobertura_zapato(img):
    gray  = img.convert("L").filter(PIL.ImageFilter.GaussianBlur(radius=2))
    edges = gray.filter(PIL.ImageFilter.FIND_EDGES)
    w, h  = edges.size
    pixels = edges.load()
    count = sum(1 for y in range(0, h, 3) for x in range(0, w, 3) if pixels[x, y] > 20)
    total = (h // 3) * (w // 3)
    return count / total if total > 0 else 0

def _encontrar_zapato_original(producto_dir, color):
    patron = re.compile(rf'^{re.escape(color)}_\d+\.(jpg|jpeg|png)$', re.IGNORECASE)
    for f in sorted(producto_dir.iterdir()):
        if patron.match(f.name):
            return f
    return None

def _verificar_consistencia_close(imagenes, producto_dir, nombre, output_dir):
    if len(imagenes) < 2:
        return imagenes
    coberturas = {p: _estimar_cobertura_zapato(PIL.Image.open(p)) for p in imagenes}
    promedio   = sum(coberturas.values()) / len(coberturas)
    umbral     = promedio * 0.6
    prompt_close_path = producto_dir / "prompt_nanobanana_close.txt"
    if not prompt_close_path.exists():
        return imagenes
    prompt_close = open(prompt_close_path, encoding="utf-8").read()
    for img_path in imagenes:
        cob = coberturas[img_path]
        print(f"  Cobertura {img_path.name}: {cob:.3f} (promedio: {promedio:.3f})")
        if cob < umbral:
            print(f"  REGENERANDO {img_path.name} (cobertura baja)")
            m = re.search(rf'^{re.escape(nombre)}_([A-Za-z]+)_(\d+)_close\.jpg$',
                          img_path.name, re.IGNORECASE)
            if not m:
                continue
            color      = m.group(1).upper()
            zapato_path = _encontrar_zapato_original(producto_dir, color)
            if not zapato_path:
                continue
            zapato_img = PIL.Image.open(zapato_path)
            for intento in range(1, 4):
                print(f"    Intento {intento}/3...")
                try:
                    img_bytes = generar_imagen(prompt_close, zapato_img)
                    if not img_bytes:
                        continue
                    qa = verificar_imagen(zapato_path, img_bytes)
                    if qa.get("aprobada", False):
                        open(img_path, "wb").write(img_bytes)
                        print("    Regenerada y aprobada")
                        break
                    else:
                        print(f"    Rechazada: {qa.get('motivo_rechazo','')}")
                except Exception as e:
                    print(f"    Error: {e}")
    return imagenes

def generar_collage(nombre, output_dir, producto_dir=None):
    patron   = re.compile(rf'^{re.escape(nombre)}_([A-Za-z]+)_\d+_close(?:_REVISAR)?\.jpg$', re.IGNORECASE)
    por_color = {}
    for f in sorted(output_dir.iterdir()):
        m = patron.match(f.name)
        if m:
            color = m.group(1).upper()
            if color not in por_color:
                por_color[color] = f

    imagenes = [por_color[c] for c in sorted(por_color)]
    n = len(imagenes)
    if n == 0:
        print("  Collage: sin imagenes _close aprobadas")
        return

    if producto_dir and n >= 2:
        print("  Verificando consistencia de imagenes _close...")
        imagenes = _verificar_consistencia_close(imagenes, producto_dir, nombre, output_dir)

    cols       = 2 if n <= 4 else 3
    rows       = math.ceil(n / cols)
    total_cells = rows * cols
    es_impar   = (n % cols != 0)

    cell_w   = COLLAGE_TARGET_W
    cell_h   = round(cell_w * COLLAGE_RATIO)
    canvas_w = cols * cell_w
    canvas_h = rows * cell_h if es_impar else COLLAGE_HEADER + rows * cell_h

    canvas = PIL.Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
    draw   = PIL.ImageDraw.Draw(canvas)

    font = None
    for fp in COLLAGE_FONT_PATHS:
        try:
            font = PIL.ImageFont.truetype(fp, COLLAGE_FONT_SIZE)
            break
        except Exception:
            pass
    if font is None:
        font = PIL.ImageFont.load_default()

    if es_impar:
        y_offset = 0
        for cell_i in range(n, total_cells):
            col_i, row_i = cell_i % cols, cell_i // cols
            draw.rectangle([(col_i * cell_w, row_i * cell_h),
                             (col_i * cell_w + cell_w, row_i * cell_h + cell_h)],
                           fill=COLLAGE_HEADER_BG)
        last_col = (total_cells - 1) % cols
        last_row = (total_cells - 1) // cols
        bt = draw.textbbox((0, 0), nombre.upper(), font=font)
        tw, th = bt[2] - bt[0], bt[3] - bt[1]
        draw.text((last_col * cell_w + (cell_w - tw) // 2,
                   last_row * cell_h + (cell_h - th) // 2),
                  nombre.upper(), fill=COLLAGE_GOLD, font=font)
    else:
        y_offset = COLLAGE_HEADER
        draw.rectangle([(0, 0), (canvas_w, COLLAGE_HEADER)], fill=COLLAGE_HEADER_BG)
        bt = draw.textbbox((0, 0), nombre.upper(), font=font)
        tw, th = bt[2] - bt[0], bt[3] - bt[1]
        draw.text(((canvas_w - tw) // 2, (COLLAGE_HEADER - th) // 2),
                  nombre.upper(), fill=COLLAGE_GOLD, font=font)

    for i, img_path in enumerate(imagenes):
        row_i, col_i = i // cols, i % cols
        img = PIL.Image.open(img_path).convert("RGB")
        volteado = _apunta_izquierda(img)
        if volteado:
            img = img.transpose(PIL.Image.FLIP_LEFT_RIGHT)
        img_cell = _fill_cell(img, cell_w, cell_h)
        canvas.paste(img_cell, (col_i * cell_w, y_offset + row_i * cell_h))
        print(f"  [{col_i},{row_i}] {img_path.name}{' [VOLTEADO]' if volteado else ''}")

    output = output_dir / f"{nombre}_collage.jpg"
    canvas.save(output, "JPEG", quality=92)
    print(f"  Collage: {output.name} ({n} colores, {rows}x{cols}, {canvas_w}x{canvas_h}px)")


# ── Imágenes web ──────────────────────────────────────────────────────────────
def _post_procesar_web(img_bytes):
    img = PIL.Image.open(BytesIO(img_bytes)).convert("RGB")
    w, h = img.size
    pixels = img.load()
    corners = [pixels[0,0], pixels[w-1,0], pixels[0,h-1], pixels[w-1,h-1]]
    bg_r = sum(c[0] for c in corners) // 4
    bg_g = sum(c[1] for c in corners) // 4
    bg_b = sum(c[2] for c in corners) // 4
    diff = PIL.ImageChops.difference(img, PIL.Image.new("RGB", (w, h), (bg_r, bg_g, bg_b)))
    dr, dg, db = diff.split()
    max_diff = PIL.ImageChops.lighter(PIL.ImageChops.lighter(dr, dg), db)
    mask = max_diff.point(lambda x: 255 if x > 20 else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return img_bytes
    cropped = img.crop(bbox)
    cw, ch  = cropped.size
    canvas_size = 1000
    margin      = int(canvas_size * 0.10)
    max_dim     = canvas_size - 2 * margin
    scale       = min(max_dim / cw, max_dim / ch)
    new_w, new_h = round(cw * scale), round(ch * scale)
    shoe   = cropped.resize((new_w, new_h), PIL.Image.LANCZOS)
    canvas = PIL.Image.new("RGB", (canvas_size, canvas_size), (255, 255, 255))
    canvas.paste(shoe, ((canvas_size - new_w) // 2, (canvas_size - new_h) // 2))
    buf = BytesIO()
    canvas.save(buf, "JPEG", quality=95)
    return buf.getvalue()

def _generar_variante_web(prompt, zapato_img, archivo_original, output_dir, nombre_base, sufijo):
    output = output_dir / f"{nombre_base}{sufijo}.jpg"
    if output.exists():
        print(f"  [{sufijo}] Ya existe, saltando: {output.name}")
        return
    img_bytes = None
    aprobada  = False
    for intento in range(1, 4):
        print(f"  [{sufijo}] Intento {intento}/3...")
        try:
            img_bytes = generar_imagen(prompt, zapato_img)
            if not img_bytes:
                continue
            qa = verificar_web(archivo_original, img_bytes)
            c  = qa.get("criterios", {})
            aprobada = qa.get("aprobada", False)
            print(f"  [{sufijo}] Fiel:{c.get('zapato_fiel_al_original')} "
                  f"Fondo:{c.get('fondo_blanco')} Centrado:{c.get('zapato_centrado')}")
            if aprobada:
                output = output_dir / f"{nombre_base}{sufijo}.jpg"
                open(output, "wb").write(_post_procesar_web(img_bytes))
                print(f"  [{sufijo}] APROBADA: {output.name}")
                break
            else:
                print(f"  [{sufijo}] RECHAZADA: {qa.get('motivo_rechazo','')}")
        except Exception as e:
            print(f"  [{sufijo}] Error: {e}")
    if not aprobada and img_bytes:
        output = output_dir / f"{nombre_base}{sufijo}_REVISAR.jpg"
        open(output, "wb").write(_post_procesar_web(img_bytes))
        print(f"  [{sufijo}] REVISAR: {output.name}")

def generar_web(nombre, producto_dir, output_dir):
    print("\n  Generando imagenes web (fondo blanco)...")
    patron = re.compile(r'^([A-Za-z]+)_1\.(png|jpg|jpeg)$', re.IGNORECASE)
    colores_1 = sorted(
        [(m.group(1).upper(), f)
         for f in producto_dir.iterdir()
         if (m := patron.match(f.name))],
        key=lambda x: x[0]
    )
    if not colores_1:
        print("  Web: no se encontraron archivos COLOR_1")
        return
    for color, archivo in colores_1:
        print(f"\n  --- Web {color} ---")
        zapato_img  = PIL.Image.open(archivo)
        nombre_base = f"{nombre}_{color}"
        _generar_variante_web(PROMPT_WEB_LATERAL,   zapato_img, archivo, output_dir, nombre_base, "_web_lateral")
        _generar_variante_web(PROMPT_WEB_DIAGONAL,  zapato_img, archivo, output_dir, nombre_base, "_web_diagonal")


# ── Lectura de archivos de producto ───────────────────────────────────────────
def leer_info_txt(producto_dir):
    info_path = producto_dir / "info.txt"
    if not info_path.exists():
        return {}
    datos = {}
    for linea in open(info_path, encoding="utf-8"):
        if ":" in linea:
            clave, _, valor = linea.partition(":")
            datos[clave.strip().lower()] = valor.strip()
    return datos

def leer_procesar_txt(producto_dir):
    """Acepta separadores '=' (bot Telegram) y ':' (formato legado)."""
    datos = {}
    for linea in open(producto_dir / "PROCESAR.txt", encoding="utf-8", errors="ignore"):
        linea = linea.strip()
        if not linea:
            continue
        if "=" in linea:
            clave, _, valor = linea.partition("=")
        elif ":" in linea:
            clave, _, valor = linea.partition(":")
        else:
            continue
        datos[clave.strip().lower()] = valor.strip()
    return datos

def actualizar_info_txt(producto_dir, nuevos_datos):
    info = leer_info_txt(producto_dir)
    info.update({k: v for k, v in nuevos_datos.items() if k in INFO_FIELDS})
    open(producto_dir / "info.txt", "w", encoding="utf-8").write(
        "\n".join(f"{k}: {v}" for k, v in info.items()) + "\n"
    )

def _actualizar_precio(producto_dir, precio):
    salida = producto_dir / "descripcion_shopify.txt"
    if not salida.exists():
        print(f"  Precio: no hay descripcion donde insertar {precio}")
        return
    lineas = open(salida, encoding="utf-8").readlines()
    nueva  = f"PRECIO: {precio}\n"
    for i, l in enumerate(lineas):
        if l.startswith("PRECIO:"):
            lineas[i] = nueva
            break
    else:
        for i, l in enumerate(lineas):
            if l.startswith("="):
                lineas.insert(i + 1, nueva)
                break
    open(salida, "w", encoding="utf-8").writelines(lineas)
    print(f"  Precio actualizado en descripcion: {precio}")


# ── Descripción Shopify ───────────────────────────────────────────────────────
def generar_descripcion_shopify(nombre, referencia_path, colores, producto_dir, precio="N/D"):
    salida = producto_dir / "descripcion_shopify.txt"
    if salida.exists():
        print("  Descripcion Shopify: ya existe, saltando")
        return
    print("  Generando descripcion Shopify...")
    info              = leer_info_txt(producto_dir)
    material          = info.get("material",          "Cuero genuino")
    altura_suela      = info.get("altura_suela",      "N/D")
    plantilla_confort = info.get("plantilla_confort", "Si")
    ocasion           = info.get("ocasion",           "Casual")
    tipo_calzado      = info.get("tipo_calzado",      "")
    proveedor         = info.get("proveedor",         "DEKO MODA")
    colores_str       = " / ".join(colores)
    try:
        caract = _leer_caracteristicas(producto_dir)
        if caract:
            print("  Caracteristicas: desde caracteristicas.txt")
            if not tipo_calzado:
                tipo_calzado = caract.get("tipo_calzado", "calzado")
            caracteristicas = (
                f"Tipo suela: {caract.get('tipo_suela', '')}. "
                f"Cierre: {caract.get('tipo_cierre', '')}. "
                f"Detalles: {caract.get('detalles_decorativos', '')}"
            ).strip()
        else:
            print("  Caracteristicas: analizando imagen (Vision)...")
            ext = referencia_path.suffix.lower().replace(".", "")
            media_type = "image/png" if ext == "png" else "image/jpeg"
            img_b64 = base64.standard_b64encode(open(referencia_path, "rb").read()).decode()
            vision_resp = claude_client.messages.create(
                model="claude-opus-4-6", max_tokens=400,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                    {"type": "text", "text": PROMPT_VISION_ZAPATO}
                ]}]
            )
            vision_txt  = vision_resp.content[0].text
            vision_data = json.loads(vision_txt[vision_txt.find("{"):vision_txt.rfind("}")+1])
            if not tipo_calzado:
                tipo_calzado = vision_data.get("tipo_calzado", "calzado")
            caracteristicas = vision_data.get("caracteristicas", "")

        prompt = PROMPT_SHOPIFY.format(
            material=material, altura_suela=altura_suela,
            plantilla_confort=plantilla_confort, ocasion=ocasion,
            precio=precio, tipo_calzado=tipo_calzado,
            proveedor=proveedor, caracteristicas=caracteristicas,
            colores=colores_str,
        )
        desc_resp = claude_client.messages.create(
            model="claude-opus-4-6", max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        descripcion = desc_resp.content[0].text.strip()
        lineas = [
            f"PRODUCTO: {nombre}", "=" * 60, f"PRECIO: {precio}", "",
            "DESCRIPCION:", descripcion, "",
            "DETALLES TECNICOS:",
            f"- Material: {material}",          f"- Tipo de calzado: {tipo_calzado}",
            f"- Altura suela: {altura_suela}",   f"- Plantilla de confort: {plantilla_confort}",
            f"- Ocasion: {ocasion}",             f"- Proveedor: {proveedor}",
            f"- Tallas: 35 al 42",               f"- Colores: {colores_str}",
        ]
        open(salida, "w", encoding="utf-8").write("\n".join(lineas))
        print(f"  Descripcion Shopify guardada: {salida.name}")
    except Exception as e:
        print(f"  Error generando descripcion Shopify: {e}")


# ── Telegram ──────────────────────────────────────────────────────────────────
def _telegram_send(texto):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    body = json.dumps({"chat_id": TELEGRAM_CHAT, "text": texto}).encode("utf-8")
    req  = urllib.request.Request(url, data=body, method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            r.read()
    except Exception as e:
        print(f"  Telegram: fallo al enviar: {e}")

def _telegram_ok(nombre, colores, precio_raw, producto_dir, tallas):
    try:
        precio_fmt = f"${int(precio_raw):,}".replace(",", ".") if precio_raw.isdigit() else precio_raw
    except Exception:
        precio_fmt = precio_raw
    ids_path    = producto_dir / "shopify_ids.json"
    url_maestro = "N/D"
    n_ind       = len(colores)
    if ids_path.exists():
        ids = json.loads(ids_path.read_text(encoding="utf-8"))
        mid = ids.get("maestro")
        if mid:
            url_maestro = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/products/{mid}"
        n_ind = len(ids.get("individuales", colores))
    tallas_str  = f"{min(tallas)}-{max(tallas)}" if tallas else "N/D"
    colores_str = ", ".join(c.upper() for c in colores)
    _telegram_send(
        f"DEKO MODA - Producto publicado\n\n"
        f"Estilo: {nombre}\n"
        f"Colores: {colores_str}\n"
        f"Precio: {precio_fmt}\n"
        f"URL: {url_maestro}\n\n"
        f"Productos individuales: {n_ind}\n"
        f"Tallas: {tallas_str}"
    )

def _telegram_error(nombre, error_msg):
    _telegram_send(f"DEKO MODA - Error en pipeline\n\nEstilo: {nombre}\nError: {error_msg}")

def enviar_imagen_telegram(image_path, caption, parse_mode=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("  Telegram: TOKEN o CHAT no configurado, saltando envio de imagen")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        with open(image_path, "rb") as f:
            data = {"chat_id": TELEGRAM_CHAT, "caption": caption}
            if parse_mode:
                data["parse_mode"] = parse_mode
            r = requests.post(url, data=data, files={"photo": f}, timeout=60)
        if r.ok:
            print(f"  Telegram foto: enviada OK")
        else:
            print(f"  Telegram foto: error {r.status_code} — {r.text[:200]}")
    except Exception as e:
        print(f"  Telegram foto: {e}")


def _telegram_send_album(imagenes, caption):
    """Envía un álbum (sendMediaGroup) con las imágenes dadas; caption en el último item."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("  Telegram álbum: no configurado, saltando")
        return
    print(f"  Telegram álbum: {len(imagenes)} imágenes encontradas")
    if not imagenes:
        if caption:
            print("  Telegram álbum: sin fotos, enviando caption como texto")
            _telegram_send(caption)
        return
    url   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMediaGroup"
    media = []
    files = {}
    for i, img_path in enumerate(imagenes):
        key  = f"photo{i}"
        item = {"type": "photo", "media": f"attach://{key}"}
        if i == len(imagenes) - 1 and caption:
            item["caption"] = caption
        media.append(item)
        files[key] = open(img_path, "rb")
    try:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT, "media": json.dumps(media)}, files=files, timeout=60)
        if r.ok:
            print(f"  Telegram álbum: enviado OK ({len(imagenes)} fotos)")
        else:
            print(f"  Telegram álbum: error {r.status_code} — {r.text[:300]}")
            if caption:
                _telegram_send(caption)
    except Exception as e:
        print(f"  Telegram álbum: {e}")
        if caption:
            _telegram_send(caption)
    finally:
        for f in files.values():
            f.close()


def _shopify_subir_collage_files(collage_path):
    """Sube el collage a Shopify Files via GraphQL y retorna la URL pública."""
    if not SHOPIFY_TOKEN:
        print("  Shopify Files: no configurado, saltando")
        return None
    gql_url = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/graphql.json"
    headers = {"Content-Type": "application/json", "X-Shopify-Access-Token": SHOPIFY_TOKEN}
    # 1) Staged upload
    try:
        r1 = requests.post(gql_url, headers=headers, json={
            "query": """
            mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
              stagedUploadsCreate(input: $input) {
                stagedTargets { url resourceUrl parameters { name value } }
                userErrors { field message }
              }
            }""",
            "variables": {"input": [{
                "filename": collage_path.name, "mimeType": "image/jpeg",
                "httpMethod": "POST", "resource": "IMAGE",
                "fileSize": str(collage_path.stat().st_size),
            }]}
        })
        d1 = r1.json()
        targets = d1.get("data", {}).get("stagedUploadsCreate", {}).get("stagedTargets", [])
        if not targets:
            print(f"  Shopify Files staged: sin targets — {d1}")
            return None
        t      = targets[0]
        params = {p["name"]: p["value"] for p in t["parameters"]}
    except Exception as e:
        print(f"  Shopify Files staged upload: {e}")
        return None
    # 2) Upload a S3
    try:
        with open(collage_path, "rb") as f:
            r2 = requests.post(t["url"], data=params, files={"file": (collage_path.name, f, "image/jpeg")})
        if r2.status_code not in (200, 201, 204):
            print(f"  Shopify Files S3: error {r2.status_code} — {r2.text[:200]}")
            return None
    except Exception as e:
        print(f"  Shopify Files S3 upload: {e}")
        return None
    # 3) fileCreate
    try:
        r3 = requests.post(gql_url, headers=headers, json={
            "query": """
            mutation fileCreate($files: [FileCreateInput!]!) {
              fileCreate(files: $files) {
                files {
                  id
                  ... on MediaImage { image { url } }
                  ... on GenericFile { url }
                }
                userErrors { field message }
              }
            }""",
            "variables": {"files": [{
                "originalSource": t["resourceUrl"], "contentType": "IMAGE",
                "alt": collage_path.stem,
            }]}
        })
        d3 = r3.json()
        errs = d3.get("data", {}).get("fileCreate", {}).get("userErrors", [])
        if errs:
            print(f"  Shopify Files fileCreate errors: {errs}")
        file_id = None
        for fc in d3.get("data", {}).get("fileCreate", {}).get("files", []):
            url = fc.get("url") or (fc.get("image") or {}).get("url")
            if url:
                return url
            file_id = fc.get("id")
    except Exception as e:
        print(f"  Shopify Files fileCreate: {e}")
        return None
    # 4) Polling — Shopify procesa de forma asíncrona
    if not file_id:
        print("  Shopify Files: sin id, no se puede hacer polling")
        return None
    print("  Shopify Files: esperando que el archivo esté disponible...")
    for intento in range(5):
        time.sleep(3)
        try:
            r_poll = requests.post(gql_url, headers=headers, json={
                "query": """
                query getFile($id: ID!) {
                  node(id: $id) {
                    ... on MediaImage { image { url } }
                    ... on GenericFile { url }
                  }
                }""",
                "variables": {"id": file_id}
            })
            node = r_poll.json().get("data", {}).get("node", {})
            url  = node.get("url") or (node.get("image") or {}).get("url")
            if url:
                print(f"  Shopify Files: URL disponible (intento {intento + 1})")
                return url
        except Exception as e:
            print(f"  Shopify Files polling {intento + 1}: {e}")
    print("  Shopify Files: URL no disponible tras 15 seg")
    return None


def _enviar_notificacion_telegram(nombre, producto_dir, precio_raw, colores_todos, tallas=""):
    output_dir   = producto_dir / "imagenes_generadas"
    collage_path = output_dir / f"{nombre}_collage.jpg"

    # Subir collage a Shopify primero para incluir URL en MENSAJE 1
    url_collage = None
    if collage_path.exists() and SHOPIFY_TOKEN:
        print("  Subiendo collage a Shopify Files...")
        url_collage = _shopify_subir_collage_files(collage_path)
        if url_collage:
            print(f"  Shopify Files URL: {url_collage}")
        else:
            print("  Shopify Files: URL no disponible")

    try:
        # MENSAJE 1: álbum con todas las _close + URL Shopify
        patron_close   = re.compile(rf'^{re.escape(nombre)}_([A-Za-z][A-Za-z0-9_]*)_\d+_close(?:_REVISAR)?\.jpg$', re.IGNORECASE)
        imagenes_close = sorted(p for p in output_dir.iterdir() if patron_close.match(p.name))
        print(f"  Imágenes _close encontradas: {len(imagenes_close)}")
        lineas_album   = [
            f"✅ {nombre} — Imágenes generadas",
            f"📸 Imágenes creadas: {_gemini_calls}",
            f"🤖 Llamadas Gemini: {_gemini_calls}",
        ]
        if url_collage:
            lineas_album.append(f"🔗 Collage en Shopify:\n{url_collage}")
        _telegram_send_album(imagenes_close, "\n".join(lineas_album))

        # MENSAJE 2: collage con datos del producto
        info_not     = leer_info_txt(producto_dir)
        material     = info_not.get("material", "")
        material_fmt = f"{material} 🐮" if "cuero" in material.lower() else material
        try:
            precio_fmt = f"${int(precio_raw):,}".replace(",", ".") if str(precio_raw).isdigit() else precio_raw
        except Exception:
            precio_fmt = precio_raw
        colores_str    = " | ".join(c.replace("_", " ").title() for c in sorted(colores_todos))
        lineas_caption = [f"*Ref. {nombre.title()}*"]
        if material_fmt:
            lineas_caption.append(f"Material: *{material_fmt}*")
        lineas_caption.append(colores_str)
        lineas_caption.append("")
        if precio_fmt:
            lineas_caption.append(f"*{precio_fmt} 🚚 Envío gratis*")
        lineas_caption.append("Pedidos 📲 300 319 1553")
        caption_collage = "\n".join(lineas_caption)

        print("  Enviando MENSAJE 2 (collage)...")
        if collage_path.exists():
            enviar_imagen_telegram(collage_path, caption=caption_collage, parse_mode="Markdown")
        else:
            print("  Collage no encontrado, enviando como texto")
            _telegram_send(caption_collage)
    except Exception as e:
        print(f"  Error en notificacion Telegram: {e}")
        import traceback; traceback.print_exc()

def esperar_respuesta_telegram(timeout=1800):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("  Telegram: no configurado — continuando automaticamente (SI)")
        return "SI"
    url_updates = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    offset = None
    try:
        req = urllib.request.Request(f"{url_updates}?limit=1&offset=-1")
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            if data.get("result"):
                offset = data["result"][-1]["update_id"] + 1
    except Exception:
        pass
    deadline = time.time() + timeout
    print(f"  Esperando SI/NO en Telegram (timeout {timeout // 60} min)...")
    while time.time() < deadline:
        time.sleep(10)
        try:
            params = f"?timeout=9{('&offset=' + str(offset)) if offset else ''}"
            req = urllib.request.Request(url_updates + params)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                if str(msg.get("chat", {}).get("id", "")) == str(TELEGRAM_CHAT):
                    text = msg.get("text", "").strip().upper()
                    if text in ("SI", "SÍ", "S", "YES"):
                        print("  Respuesta recibida: SI — continuando")
                        return "SI"
                    elif text == "NO":
                        print("  Respuesta recibida: NO — deteniendo")
                        return "NO"
        except Exception as e:
            print(f"  Telegram polling error: {e}")
    print("  Timeout sin respuesta — deteniendo")
    return "TIMEOUT"


# ── Shopify ───────────────────────────────────────────────────────────────────
def _shopify_request(method, endpoint, payload=None):
    if not SHOPIFY_TOKEN:
        raise RuntimeError("SHOPIFY_TOKEN no configurado")
    url  = f"{SHOPIFY_BASE_URL}/{endpoint}"
    body = json.dumps(payload).encode("utf-8") if payload else None
    req  = urllib.request.Request(url, data=body, method=method, headers={
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
    })
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()}")

def _shopify_subir_imagen(product_id, img_path, alt="", variant_ids=None):
    data    = base64.b64encode(open(img_path, "rb").read()).decode("utf-8")
    payload = {"image": {"attachment": data, "filename": img_path.name, "alt": alt}}
    if variant_ids:
        payload["image"]["variant_ids"] = variant_ids
    return _shopify_request("POST", f"products/{product_id}/images.json", payload)["image"]["id"]

def crear_en_shopify(nombre, producto_dir, colores, precio, output_dir):
    ids_path = producto_dir / "shopify_ids.json"
    if ids_path.exists():
        print("  Shopify: ya publicado, saltando")
        return
    desc_raw = open(producto_dir / "descripcion_shopify.txt", encoding="utf-8").read()
    m    = re.search(r'DESCRIPCION HTML:\n(.+?)(?:\n\n|\nCARACTERISTICAS:)', desc_raw, re.DOTALL)
    html = m.group(1).strip() if m else ""
    ids  = {"maestro": None, "individuales": {}}

    print("  Shopify: creando producto maestro...")
    variantes = [
        {"option1": c.capitalize(), "option2": t, "price": precio, "inventory_management": None}
        for c in colores for t in SHOPIFY_TALLAS
    ]
    res_maestro = _shopify_request("POST", "products.json", {"product": {
        "title": nombre, "body_html": html, "status": "draft",
        "tags": f"DEKO MODA, {nombre}, cuero",
        "options": [{"name": "Color"}, {"name": "Talla"}],
        "variants": variantes,
    }})
    maestro    = res_maestro["product"]
    maestro_id = maestro["id"]
    ids["maestro"] = maestro_id
    print(f"  Shopify: maestro ID {maestro_id} ({len(maestro['variants'])} variantes)")

    variantes_por_color = {}
    for v in maestro["variants"]:
        variantes_por_color.setdefault(v["option1"].upper(), []).append(v["id"])

    print("  Shopify: creando productos individuales...")
    for color in colores:
        res = _shopify_request("POST", "products.json", {"product": {
            "title": f"{nombre} - {color.capitalize()}",
            "body_html": html, "status": "draft",
            "tags": f"DEKO MODA, {nombre}, {color.capitalize()}, cuero",
            "options": [{"name": "Talla"}],
            "variants": [{"option1": t, "price": precio, "inventory_management": None}
                         for t in SHOPIFY_TALLAS],
        }})
        pid = res["product"]["id"]
        ids["individuales"][color] = pid
        print(f"    [{color}] ID {pid}")

    print("  Shopify: subiendo imagenes...")
    for color in colores:
        img = output_dir / f"{nombre}_{color}_web_lateral.jpg"
        if not img.exists():
            print(f"    [{color}] imagen no encontrada, saltando")
            continue
        alt_txt = f"{nombre} {color.capitalize()}"
        try:
            ind_id = ids["individuales"].get(color)
            if ind_id:
                _shopify_subir_imagen(ind_id, img, alt=alt_txt)
            vids = variantes_por_color.get(color, [])
            _shopify_subir_imagen(maestro_id, img, alt=alt_txt, variant_ids=vids)
            print(f"    [{color}] imagenes subidas")
        except RuntimeError as e:
            print(f"    [{color}] ERROR imagen: {e}")

    ids_path.write_text(json.dumps(ids, indent=2), encoding="utf-8")
    print(f"  Shopify: IDs guardados — maestro {maestro_id}")


# ── Procesamiento principal ───────────────────────────────────────────────────
def procesar_producto(producto_dir):
    nombre = producto_dir.name
    global _gemini_calls
    _gemini_calls = 0
    print("=" * 70)
    print(f"PROCESANDO: {nombre}")
    print("=" * 70)

    procesar_data = leer_procesar_txt(producto_dir)
    campos_info   = {k: v for k, v in procesar_data.items() if k in INFO_FIELDS}
    tiene_precio  = "precio" in procesar_data
    if procesar_data:
        print(f"  PROCESAR.txt: {list(procesar_data.keys())}")
    if campos_info:
        actualizar_info_txt(producto_dir, campos_info)
        print(f"  info.txt actualizado: {list(campos_info.keys())}")

    # Referencia: acepta referencia_pinterest.jpg (generada por el bot)
    referencia = None
    for nombre_ref in ["referencia.jpg", "referencia.jpeg", "referencia.png",
                       "referencia_pinterest.jpg", "referencia_pinterest.png"]:
        if (producto_dir / nombre_ref).exists():
            referencia = producto_dir / nombre_ref
            break
    if not referencia:
        print("ERROR: No se encontro referencia")
        (producto_dir / "PROCESAR.txt").unlink(missing_ok=True)
        return

    output_dir = producto_dir / "imagenes_generadas"
    output_dir.mkdir(exist_ok=True)

    patron = re.compile(r'^([A-Za-z]+)_(\d+)\.(png|jpg|jpeg)$', re.IGNORECASE)
    archivos = sorted(
        [(m.group(1).upper(), m.group(2), f)
         for f in producto_dir.iterdir()
         if (m := patron.match(f.name))],
        key=lambda x: (x[0], x[1])
    )
    colores_todos  = sorted(set(c for c, _, _ in archivos))
    faltantes      = [(c, n, f) for c, n, f in archivos
                      if not (output_dir / f"{nombre}_{c}_{n}_close.jpg").exists()
                      and not (output_dir / f"{nombre}_{c}_{n}_close_REVISAR.jpg").exists()]
    colores_nuevos = sorted(set(c for c, _, _ in faltantes))

    descripcion_existe = (producto_dir / "descripcion_shopify.txt").exists()
    info_completa      = all(k in leer_info_txt(producto_dir) for k in INFO_FIELDS)
    hay_faltantes      = bool(faltantes)

    print(f"  Colores disponibles: {colores_todos}")
    if colores_nuevos:
        print(f"  Colores sin imagenes: {colores_nuevos}")

    if hay_faltantes:
        prompt_close_path = producto_dir / "prompt_nanobanana_close.txt"
        if prompt_close_path.exists():
            prompt_close = open(prompt_close_path, encoding="utf-8").read()
            print("  Prompt plano cerrado: reutilizando")
        else:
            prompt_close = analizar_referencia_close(referencia)
            open(prompt_close_path, "w", encoding="utf-8").write(prompt_close)
            print("  Prompt plano cerrado: generado")
            _telegram_send(f"📋 Prompt _close generado para {nombre}:\n\n{prompt_close}")

        print(f"\n  Generando: {[f'{c}_{n}' for c,n,_ in faltantes]}")
        for color, numero, archivo in faltantes:
            print(f"\n  --- {color}_{numero} ---")
            zapato_img  = PIL.Image.open(archivo)
            nombre_base = f"{nombre}_{color}_{numero}"
            # Escena completa desactivada — solo _close
            _generar_variante(prompt_close, zapato_img, archivo, output_dir, nombre_base, sufijo="_close")

        print("\n  Regenerando collage...")
        collage_path = output_dir / f"{nombre}_collage.jpg"
        if collage_path.exists():
            collage_path.unlink()
        generar_collage(nombre, output_dir, producto_dir)

    elif campos_info and descripcion_existe:
        print("  Regenerando descripcion (info actualizada)...")
        (producto_dir / "descripcion_shopify.txt").unlink()
        generar_descripcion_shopify(nombre, referencia, colores_todos, producto_dir,
                                    precio=procesar_data.get("precio", "N/D"))
    elif not descripcion_existe and info_completa:
        generar_descripcion_shopify(nombre, referencia, colores_todos, producto_dir,
                                    precio=procesar_data.get("precio", "N/D"))
    elif not tiene_precio:
        print("  Todo al dia, nada que procesar")

    if tiene_precio:
        _actualizar_precio(producto_dir, procesar_data["precio"])

    precio_shopify = procesar_data.get("precio", "0")

    # DESACTIVADO TEMPORALMENTE — requiere autorización Telegram
    # desc_ok = (producto_dir / "descripcion_shopify.txt").exists()
    # web_ok  = any((output_dir / f"{nombre}_{c}_web_lateral.jpg").exists() for c in colores_todos)
    # if desc_ok and web_ok and SHOPIFY_TOKEN:
    #     try:
    #         crear_en_shopify(nombre, producto_dir, colores_todos, precio_shopify, output_dir)
    #     except RuntimeError as e:
    #         print(f"  Shopify ERROR: {e}")
    #         _telegram_error(nombre, f"Shopify: {e}")

    (producto_dir / "PROCESAR.txt").unlink(missing_ok=True)
    print(f"\n{nombre} completado!")
    _enviar_notificacion_telegram(nombre, producto_dir, precio_shopify, colores_todos, tallas=procesar_data.get("tallas", ""))


# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Modo bot: python pipeline.py NOMBRE_ESTILO
        nombre_arg    = sys.argv[1]
        producto_dir  = PRODUCTOS_DIR / nombre_arg
        if not producto_dir.exists():
            print(f"ERROR: carpeta no encontrada: {producto_dir}")
            sys.exit(1)
        if not (producto_dir / "PROCESAR.txt").exists():
            print(f"ERROR: PROCESAR.txt no encontrado en {producto_dir}")
            sys.exit(1)
        procesar_producto(producto_dir)
    else:
        # Modo monitor: escanea continuamente
        print("=" * 70)
        print("DEKO MODA - Monitor de productos")
        print(f"Entorno: {'Railway' if EN_RAILWAY else 'Local'}")
        print(f"Productos: {PRODUCTOS_DIR}")
        print("Ctrl+C para detener")
        print("=" * 70)
        while True:
            for carpeta in PRODUCTOS_DIR.iterdir():
                if carpeta.is_dir() and (carpeta / "PROCESAR.txt").exists():
                    procesar_producto(carpeta)
            time.sleep(10)
