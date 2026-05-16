import os
import json
import time
import urllib.request
import urllib.error
from datetime import date, timedelta
from pathlib import Path

PRODUCTOS_DIR  = Path(os.getenv("PRODUCTOS_DIR", "/app/data/productos"))
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


def revisar_productos():
    hoy = date.today()
    revisados = 0
    for ids_path in sorted(PRODUCTOS_DIR.glob("*/shopify_ids.json")):
        nombre = ids_path.parent.name
        try:
            ids = json.loads(ids_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [{nombre}] Error leyendo JSON: {e}")
            continue

        if not ids.get("activo", False):
            continue

        fecha_pub   = ids.get("fecha_publicacion")
        dias_activo = int(ids.get("dias_activo", 10))
        if not fecha_pub:
            continue

        vencimiento = date.fromisoformat(fecha_pub) + timedelta(days=dias_activo)
        dias_restantes = (vencimiento - hoy).days
        print(f"  [{nombre}] vence {vencimiento} ({dias_restantes}d restantes)")

        if vencimiento <= hoy:
            print(f"  [{nombre}] VENCIDO — desactivando...")
            maestro_id   = ids.get("maestro")
            individuales = ids.get("individuales", {})
            todos_ids    = ([maestro_id] if maestro_id else []) + list(individuales.values())
            errores = []
            for pid in todos_ids:
                try:
                    _shopify_request("PUT", f"products/{pid}.json",
                                     {"product": {"id": pid, "status": "draft"}})
                    print(f"    {pid} -> draft OK")
                except RuntimeError as e:
                    errores.append(str(e))
                    print(f"    {pid} ERROR: {e}")

            if not errores:
                ids["activo"] = False
                ids_path.write_text(json.dumps(ids, indent=2), encoding="utf-8")
                _telegram_send(
                    f"⚠️ {nombre} desactivado — venció después de {dias_activo} días"
                )
            else:
                print(f"  [{nombre}] Errores: {errores}")

        revisados += 1

    print(f"Revision completada: {revisados} productos activos revisados")


def main():
    print("Vigilancia Deko Moda iniciada")
    print(f"PRODUCTOS_DIR: {PRODUCTOS_DIR}")
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
