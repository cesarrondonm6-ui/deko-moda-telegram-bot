import os
import json
import time
import urllib.request
import urllib.error
from datetime import date, timedelta

SHOPIFY_TOKEN  = os.getenv("SHOPIFY_TOKEN", "")
SHOPIFY_SHOP   = os.getenv("SHOPIFY_SHOP", "")
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("CHAT_ID", "")


def _shopify_request(method, endpoint, data=None):
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


def _telegram_send(text):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        return
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    body = json.dumps({"chat_id": TELEGRAM_CHAT, "text": text}).encode()
    req  = urllib.request.Request(url, data=body,
                                  headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"  Telegram error: {e}")


def _leer_metafields(product_id):
    """Devuelve dict key→value de metafields namespace=deko."""
    try:
        r = _shopify_request("GET", f"products/{product_id}/metafields.json?namespace=deko")
        return {m["key"]: m for m in r.get("metafields", [])}
    except RuntimeError:
        return {}


def _actualizar_metafield(mf):
    """Pone activo=false en el metafield dado."""
    try:
        _shopify_request("PUT", f"metafields/{mf['id']}.json", {
            "metafield": {"id": mf["id"], "value": "false", "type": "single_line_text_field"}
        })
    except RuntimeError as e:
        print(f"    ERROR actualizando metafield {mf['id']}: {e}")


def revisar_productos():
    hoy = date.today()

    # Obtener todos los productos activos con tag DEKO MODA (paginado)
    productos = []
    url_base = "products.json?limit=250&status=active&fields=id,title,tags"
    try:
        r = _shopify_request("GET", url_base)
        productos = r.get("products", [])
    except RuntimeError as e:
        print(f"  Error obteniendo productos: {e}")
        return

    deko = [p for p in productos if "DEKO MODA" in p.get("tags", "")]
    print(f"  Productos activos DEKO MODA: {len(deko)}")

    vencidos_por_nombre = {}

    for p in deko:
        pid   = p["id"]
        title = p["title"]

        if " - " not in title:
            continue  # producto maestro (titulo sin "NOMBRE - COLOR") — nunca lo toca vigilancia

        mfs = _leer_metafields(pid)

        if "fecha_pub" not in mfs:
            continue  # sin metafield = sin control de vigencia

        if mfs.get("activo", {}).get("value") == "false":
            continue  # ya marcado como inactivo

        fecha_pub_str = mfs["fecha_pub"]["value"]
        dias_activo   = int(mfs.get("dias_activo", {}).get("value", 10))

        try:
            vencimiento = date.fromisoformat(fecha_pub_str) + timedelta(days=dias_activo)
        except ValueError:
            continue

        dias_rest = (vencimiento - hoy).days
        print(f"  [{title}] vence {vencimiento} ({dias_rest}d restantes)")

        if vencimiento <= hoy:
            nombre_base = title.split(" - ")[0].strip()
            vencidos_por_nombre.setdefault(nombre_base, []).append(
                (pid, title, dias_activo, mfs)
            )

    for nombre_base, items in vencidos_por_nombre.items():
        print(f"\n  VENCIDO: {nombre_base} — desactivando {len(items)} productos...")
        errores = []
        for pid, title, dias_activo, mfs in items:
            try:
                _shopify_request("PUT", f"products/{pid}.json",
                                 {"product": {"id": pid, "status": "draft"}})
                print(f"    [{title}] -> draft OK")
                if "activo" in mfs:
                    _actualizar_metafield(mfs["activo"])
            except RuntimeError as e:
                errores.append(str(e))
                print(f"    [{title}] ERROR: {e}")

        if not errores:
            _telegram_send(
                f"⚠️ {nombre_base} desactivado — venció después de {dias_activo} días"
            )
        else:
            _telegram_send(
                f"❌ Error al desactivar {nombre_base}: {len(errores)} productos fallaron"
            )


def main():
    print("Vigilancia Deko Moda iniciada")
    print(f"Shop: {SHOPIFY_SHOP}")
    while True:
        hoy = date.today()
        print(f"\n[{hoy}] Revisando productos...")
        try:
            revisar_productos()
        except Exception as e:
            print(f"Error en revision: {e}")
        print("Proxima revision en 24 horas")
        time.sleep(86400)


if __name__ == "__main__":
    main()
