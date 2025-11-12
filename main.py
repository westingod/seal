#!/usr/bin/env python3
"""
Bot Telegram automatizado para:
- recibir número
- login en SEAL
- registrar el número en el formulario Create
- abrir detalle del suministro
- descargar PDF asociado
- convertir primera página a PNG
- enviar la imagen por Telegram
"""

import os
import asyncio
from pathlib import Path
import logging
import fitz  # PyMuPDF
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# --- Config desde variables de entorno ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN","8312541235:AAEjokyp6fPOOHZUj2ml1HyqJ1HGnMMRwk8")             # token del bot
BOT_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")             # opcional: chat_id objetivo si quieres enviar proactivamente
SEAL_USER = os.getenv("SEAL_USER","gary.bellpaz@gmail.com" )                      # tu usuario (ej: gary.bellpaz@gmail.com)
SEAL_PASS = os.getenv("SEAL_PASS","s4dyc0zt")                      # tu contraseña (guardar en env vars)
SEAL_SUCURSAL = os.getenv("SEAL_SUCURSAL", "1")         # sucursal por defecto (1)

if not BOT_TOKEN or not SEAL_USER or not SEAL_PASS:
    raise SystemExit("Falta configurar variables de entorno: TELEGRAM_BOT_TOKEN, SEAL_USER, SEAL_PASS")

# --- URLs y selectores ---
LOGIN_URL = "https://oficinavirtual.seal.com.pe/Home/Login"
FORM_CREATE_URL = "https://oficinavirtual.seal.com.pe/taClienteSuministroes/Create"
DETAIL_URL_TEMPLATE = "https://oficinavirtual.seal.com.pe/Suministros/Detalle?strCodigoSuministro={num}&strCodigoSucursal={suc}"

# Selector del campo en Create (según lo que compartiste)
SELECTOR_CODIGO_SUMINISTRO = "input#CodigoSuministro"
# Probablemente el formulario tenga un botón submit; intentaremos detectar:
SELECTOR_CREATE_SUBMIT = "form button[type=submit], button.btn-primary, input[type=submit]"

# Carpeta de trabajo
WORKDIR = Path("tmp")
WORKDIR.mkdir(exist_ok=True)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("seal-bot")


# ------------------------------
# Utilidades: descarga y conversión
# ------------------------------
async def try_download_from_page(page, download_hint_name: str) -> Path:
    """
    Intenta detectar y descargar un PDF desde la página abierta con Playwright.
    - Busca enlaces <a> con href que termine en .pdf
    - Si encuentra alguno, lo descarga con page.context.expect_download
    - Si no encuentra enlaces, intenta hacer click en elementos <a> o botones y usar expect_download
    Devuelve la ruta al archivo descargado (Path).
    """
    # 1) Buscar enlaces directos a PDF en hrefs
    anchors = await page.query_selector_all("a[href$='.pdf']")
    if anchors:
        href = await anchors[0].get_attribute("href")
        logger.info("Encontrado enlace directo a PDF: %s", href)
        # Usar expect_download para descargar
        try:
            async with page.expect_download(timeout=15000) as dl_info:
                # click si el href es relativo/absoluto; usar click en el anchor
                await anchors[0].click()
            download = await dl_info.value
            filename = download.suggested_filename or f"{download_hint_name}.pdf"
            target = WORKDIR / filename
            await download.save_as(str(target))
            return target
        except PWTimeout:
            # fallback: obtener URL y descargar con request-like fetch
            logger.warning("expect_download timeout; intentar fetch del href")
            href_full = href if href.startswith("http") else (page.url.rstrip("/") + "/" + href.lstrip("/"))
            # utilizar method page.request to fetch bytes
            r = await page.request.get(href_full)
            if r.ok:
                target = WORKDIR / (download_hint_name + ".pdf")
                with open(target, "wb") as f:
                    f.write(await r.body())
                return target
            else:
                raise RuntimeError(f"No se pudo descargar el PDF (status {r.status})")
    # 2) Intentar clics en botones o enlaces que inicien descarga
    candidates = await page.query_selector_all("a, button")
    for el in candidates:
        text = (await el.inner_text()).strip()[:60]
        # heurística: texto que mencione "PDF", "Descargar", "Imprimir", "Ver"
        if any(k in text.lower() for k in ("pdf", "descargar", "imprimir", "ver", "detalle")):
            try:
                async with page.expect_download(timeout=8000) as dl_info:
                    await el.click()
                download = await dl_info.value
                filename = download.suggested_filename or f"{download_hint_name}.pdf"
                target = WORKDIR / filename
                await download.save_as(str(target))
                return target
            except Exception:
                continue
    raise RuntimeError("No se encontró un enlace/elemento claro para descargar el PDF en la página.")


def pdf_first_page_to_png(pdf_path: Path, out_png: Path, zoom: float = 2.0) -> Path:
    """
    Convierte primera página del PDF a PNG usando PyMuPDF.
    """
    doc = fitz.open(str(pdf_path))
    if doc.page_count < 1:
        doc.close()
        raise RuntimeError("PDF sin páginas.")
    page = doc.load_page(0)
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    pix.save(str(out_png))
    doc.close()
    return out_png


# ------------------------------
# Flujo de Playwright: login, create, detalle, descarga
# ------------------------------
async def process_number_and_get_image(number: str) -> Path:
    """
    Ejecuta el flujo completo y devuelve la ruta de la imagen PNG generada.
    """
    number = number.strip()
    if not number.isnumeric():
        raise ValueError("El número debe contener solo dígitos.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        # 1) Login
        logger.info("Abrir Login: %s", LOGIN_URL)
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        # Rellenar campos: en esa página los inputs pueden tener name/email y password; usaremos selectores comunes
        # Ajustes si fallan: cambiar selectores a los reales inspeccionados.
        # Intentar rellenar 'Email' y 'Password'
        # Comunes:
        possible_user_selectors = ["input#UserName", "input[name='UserName']", "input[name='email']", "input[type='email']", "input[id*='User']"]
        possible_pass_selectors = ["input#Password", "input[name='Password']", "input[type='password']"]
        filled_user = False
        filled_pass = False
        for s in possible_user_selectors:
            try:
                await page.fill(s, SEAL_USER)
                filled_user = True
                break
            except Exception:
                continue
        for s in possible_pass_selectors:
            try:
                await page.fill(s, SEAL_PASS)
                filled_pass = True
                break
            except Exception:
                continue
        if not (filled_user and filled_pass):
            # Intenta campos por placeholder comunes
            logger.warning("No se pudieron llenar con selectores comunes. Intentando heurística.")
            # continuar sin abortar, quizá la forma es distinta; si falla el login, atraparemos luego.
        # Click en botón submit del login
        # intentar botones comunes
        login_btn_selectors = ["button[type=submit]", "input[type=submit]", "button.btn-primary"]
        clicked = False
        for sel in login_btn_selectors:
            try:
                await page.click(sel)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            # fallback: presionar Enter en el campo user
            try:
                await page.press(possible_user_selectors[0], "Enter")
            except Exception:
                pass

        # esperar navegación o carga
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            logger.info("Continuando aunque networkidle timeout (posible SPA).")

        # 2) Ir a Create y llenar CodigoSuministro
        logger.info("Ir a Create: %s", FORM_CREATE_URL)
        await page.goto(FORM_CREATE_URL, wait_until="domcontentloaded")
        # Rellenar el campo CodigoSuministro
        try:
            await page.fill(SELECTOR_CODIGO_SUMINISTRO, number)
        except Exception as e:
            logger.error("No se pudo llenar CodigoSuministro: %s", e)
            # Intentar por name
            try:
                await page.fill("input[name='CodigoSuministro']", number)
            except Exception as e2:
                await browser.close()
                raise RuntimeError("No fue posible rellenar el campo CodigoSuministro.") from e2

        # Enviar el formulario
        submitted = False
        try:
            await page.click(SELECTOR_CREATE_SUBMIT)
            submitted = True
        except Exception:
            # intentar presionar Enter en el campo
            try:
                await page.press(SELECTOR_CODIGO_SUMINISTRO, "Enter")
                submitted = True
            except Exception:
                submitted = False
        if not submitted:
            await browser.close()
            raise RuntimeError("No se pudo enviar el formulario Create; revisa SELECTOR_CREATE_SUBMIT.")

        # Esperar un poco por la creación
        await page.wait_for_timeout(1500)

        # 3) Ir a la página detalle construida
        detail_url = DETAIL_URL_TEMPLATE.format(num=number, suc=SEAL_SUCURSAL)
        logger.info("Ir a Detalle: %s", detail_url)
        await page.goto(detail_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)

        # 4) Intentar descargar el PDF desde la página de detalle
        pdf_path = await try_download_from_page(page, download_hint_name=f"suministro_{number}")
        logger.info("PDF descargado a: %s", pdf_path)

        await browser.close()

    # 5) Convertir a imagen
    out_png = pdf_path.with_suffix(".png")
    logger.info("Convirtiendo a PNG: %s", out_png)
    pdf_first_page_to_png(pdf_path, out_png, zoom=2.0)
    logger.info("Imagen creada: %s", out_png)
    return out_png


# ------------------------------
# Telegram handlers
# ------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hola 👋. Envíame el número de suministro y lo buscaré en SEAL.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"Recibido: {txt}. Iniciando proceso... ⏳")
    try:
        image_path = await process_number_and_get_image(txt)
        # enviar imagen
        await update.message.reply_photo(photo=open(image_path, "rb"))
        await update.message.reply_text("✅ Listo — te envié la imagen del documento.")
    except Exception as e:
        logger.exception("Error en flujo:")
        await update.message.reply_text(f"❌ Ocurrió un error: {e}")

# ------------------------------
# Main
# ------------------------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot iniciado, esperando mensajes...")
    app.run_polling()

if __name__ == "__main__":
    main()
