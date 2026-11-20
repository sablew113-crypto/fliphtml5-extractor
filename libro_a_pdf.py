#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
libro_a_pdf.py — convierte un libro de FlipHTML5 en un PDF con OCR (Tesseract), directamente.

Obtiene el índice de páginas del visor de FlipHTML5, descarga cada página en memoria,
la convierte a JPEG, ejecuta una pasada de OCR con Tesseract para que el texto sea
seleccionable y monta el PDF final sin dejar imágenes sueltas.

Uso:
    python libro_a_pdf.py <url_del_libro> <salida.pdf>
    python libro_a_pdf.py <url> <salida.pdf> --desde 1 --hasta 415
    python libro_a_pdf.py <url> <salida.pdf> --sin-ocr

Ejemplo:
    python libro_a_pdf.py https://online.fliphtml5.com/vbxdd/azcu/ "Amanecer en la cosecha.pdf"
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

SNAP_JS = r"""
() => {
  const out = [];
  const grab = (id, re) => {
    const m = re.exec(id);
    if (!m) return;
    const el = document.getElementById(id);
    if (!el) return;
    const img = el.querySelector('img[src*="/files/large/"]');
    if (img && img.src) out.push([parseInt(m[1], 10), img.src]);
  };
  for (const el of document.querySelectorAll('div[id^="page"]')) {
    if (/^page\d+$/.test(el.id)) grab(el.id, /^page(\d+)$/);
  }
  for (const el of document.querySelectorAll('div[id^="pageBack"]')) {
    if (/^pageBack\d+$/.test(el.id)) grab(el.id, /^pageBack(\d+)$/);
  }
  return out;
}
"""


def log(m):
    print(m, flush=True)


def asegurar_tessdata(idioma="spa"):
    """Asegura que Tesseract tenga el archivo traineddata del idioma pedido."""
    carpeta_local = Path(__file__).resolve().parent / "tessdata"
    sistema = Path("/usr/share/tesseract-ocr/5/tessdata")
    if sistema.exists() and (sistema / f"{idioma}.traineddata").exists():
        return str(sistema)

    carpeta_local.mkdir(parents=True, exist_ok=True)
    if sistema.exists():
        for item in sistema.iterdir():
            dest = carpeta_local / item.name
            if not dest.exists():
                if item.is_dir():
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)

    modelo = carpeta_local / f"{idioma}.traineddata"
    if not modelo.exists():
        url = f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/{idioma}.traineddata"
        log(f"Descargando modelo OCR ({idioma}) para Tesseract…")
        with urllib.request.urlopen(url, timeout=60) as r:
            modelo.write_bytes(r.read())
    return str(carpeta_local)


def total_paginas(pagina, por_defecto=415):
    try:
        n = pagina.evaluate(
            "() => (window.htmlConfig && htmlConfig.meta && htmlConfig.meta.pageCount) || "
            "(Array.isArray(window.fliphtml5_pages) && window.fliphtml5_pages.length) || 0"
        )
        if isinstance(n, int) and n > 0:
            return n
    except Exception:
        pass
    return por_defecto


def extraer_urls_directo(pagina, base_url):
    """Lee el arreglo descifrado window.fliphtml5_pages directamente desde el visor."""
    try:
        pagina.wait_for_function(
            "() => Array.isArray(window.fliphtml5_pages) && window.fliphtml5_pages.length > 0",
            timeout=30_000,
        )
        raw = pagina.evaluate(
            """() => window.fliphtml5_pages.map((p, idx) => {
                let src = Array.isArray(p.n) ? p.n[0] : p.n;
                if (!src) return null;
                if (!src.startsWith('http') && !src.startsWith('files/') && !src.startsWith('./files/')) {
                    src = 'files/large/' + src;
                }
                return [idx + 1, src];
            }).filter(Boolean)"""
        )
        if raw:
            base = base_url if base_url.endswith("/") else base_url + "/"
            return {int(n): urllib.parse.urljoin(base, src) for n, src in raw}
    except Exception:
        pass
    return {}


def rastrear(pagina, total, base_url):
    """Obtiene {n: url} desde la configuración descifrada o avanzando página a página."""
    directas = extraer_urls_directo(pagina, base_url)
    if len(directas) >= total:
        return directas

    vistas, fallos, teclado = dict(directas), 0, False
    while len(vistas) < total and fallos < 15:
        antes = len(vistas)
        for n, url in (pagina.evaluate(SNAP_JS) or []):
            if 1 <= n <= total and n not in vistas:
                vistas[n] = url
        fallos = 0 if len(vistas) > antes else fallos + 1
        if len(vistas) >= total:
            break
        if fallos:
            log(f"  páginas detectadas: {len(vistas)}/{total}")
        if fallos >= 3:
            teclado = True
        if teclado:
            pagina.keyboard.press("ArrowRight")
        else:
            vp = pagina.viewport_size
            pagina.mouse.click(vp["width"] * 0.86, vp["height"] * 0.5)
        pagina.wait_for_timeout(850)
    return vistas


def descargar_una(n, url, temporales, calidad=78):
    """Descarga una página en memoria y la guarda como JPEG temporal optimizado."""
    from PIL import Image

    destino = temporales / f"{n:04d}.jpg"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://online.fliphtml5.com/"},
    )
    for intento in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                datos = resp.read()
            with Image.open(io.BytesIO(datos)) as im:
                escala = 0.70 if n == 1 else 0.65
                w, h = int(im.width * escala), int(im.height * escala)
                modo = "RGB" if n == 1 else "L"
                q = calidad if n == 1 else 62
                im.convert(modo).resize((w, h), Image.Resampling.LANCZOS).save(
                    destino, "JPEG", quality=q, optimize=True
                )
            return n, destino, None
        except Exception as e:
            if intento == 3:
                return n, None, str(e)
            time.sleep(1.5 * (intento + 1))
    return n, None, "Reintentos agotados"


def bajar(urls, temporales, desde, hasta, hilos=12):
    """Descarga y convierte cada página a JPEG en disco temporal en paralelo."""
    objetivos = [(n, urls[n]) for n in sorted(urls) if desde <= n <= hasta]
    total = len(objetivos)
    resultados = {}
    completadas = 0

    with ThreadPoolExecutor(max_workers=hilos) as pool:
        futuros = {pool.submit(descargar_una, n, u, temporales): n for n, u in objetivos}
        for fut in as_completed(futuros):
            n, ruta, err = fut.result()
            completadas += 1
            if ruta is not None:
                resultados[n] = ruta
                if completadas % 25 == 0 or completadas == total:
                    log(f"  descargadas {completadas}/{total} páginas (última: {n})")
            else:
                log(f"  ! página {n} fallida: {err}")

    return [resultados[n] for n in sorted(resultados)]


def ocr_una_pagina(jpg_path, idioma, env):
    """Ejecuta Tesseract sobre un JPEG temporal y devuelve la ruta del PDF de 1 página con capa de texto."""
    base_out = jpg_path.with_suffix("")
    pdf_out = jpg_path.with_suffix(".pdf")
    cmd = [
        "tesseract",
        str(jpg_path),
        str(base_out),
        "-l",
        idioma,
        "--oem",
        "1",
        "--psm",
        "6",
        "-c",
        "tessedit_do_invert=0",
        "pdf",
    ]
    subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return pdf_out


def armar_pdf_con_ocr(jpgs, salida, idioma="spa", hilos=2):
    """Pasa cada JPEG por Tesseract OCR y une las páginas en un único PDF con texto seleccionable."""
    import pikepdf

    tessdata_dir = asegurar_tessdata(idioma)
    env = os.environ.copy()
    env["TESSDATA_PREFIX"] = tessdata_dir
    env["OMP_THREAD_LIMIT"] = "1"

    total = len(jpgs)
    pdfs_pagina = {}
    hechas = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=hilos) as pool:
        futuros = {pool.submit(ocr_una_pagina, jpg, idioma, env): idx for idx, jpg in enumerate(jpgs, 1)}
        for fut in as_completed(futuros):
            idx = futuros[fut]
            pdfs_pagina[idx] = fut.result()
            hechas += 1
            if hechas % 25 == 0 or hechas == total:
                elap = time.time() - t0
                log(f"  OCR Tesseract: {hechas}/{total} páginas ({elap:.1f}s)")

    log(f"Uniendo y optimizando {total} páginas con capa de texto OCR en {salida.name}…")
    import zlib
    from PIL import Image

    final_pdf = pikepdf.Pdf.new()
    for idx in sorted(pdfs_pagina):
        with pikepdf.Pdf.open(pdfs_pagina[idx]) as p:
            for page in p.pages:
                if idx > 1:
                    for _, raw_img in list(page.images.items()):
                        pil_img = pikepdf.PdfImage(raw_img).as_pil_image().convert("L")
                        w, h = pil_img.size
                        bw = pil_img.point(lambda x: 255 if x > 185 else 0, mode="1")
                        raw_img.write(zlib.compress(bw.tobytes(), level=9), filter=pikepdf.Name("/FlateDecode"))
                        raw_img["/Width"] = w
                        raw_img["/Height"] = h
                        raw_img["/ColorSpace"] = pikepdf.Name("/DeviceGray")
                        raw_img["/BitsPerComponent"] = 1
                final_pdf.pages.append(page)
    final_pdf.save(salida, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("salida")
    ap.add_argument("--desde", type=int, default=1)
    ap.add_argument("--hasta", type=int, default=0)
    ap.add_argument("--idioma-ocr", default="spa")
    ap.add_argument("--sin-ocr", action="store_true", help="Generar PDF solo de imágenes sin pasada de Tesseract")
    a = ap.parse_args()

    salida = Path(a.salida).with_suffix(".pdf")
    temporales = Path(tempfile.mkdtemp(prefix="fliphtml5_"))
    cache = Path(str(salida) + ".urls.json")

    try:
        if cache.exists():
            log("Reutilizando el listado de URLs guardado.")
            urls = {int(k): v for k, v in json.loads(cache.read_text()).items()}
            total = max(urls) if urls else 415
            hasta = a.hasta or total
        else:
            with sync_playwright() as p:
                ejecutable = "/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None
                headless = not bool(os.environ.get("DISPLAY"))
                nav = p.chromium.launch(headless=headless, executable_path=ejecutable)
                ctx = nav.new_context(
                    viewport={"width": 1000, "height": 1100},
                    locale="es-ES",
                    ignore_https_errors=True,
                )
                pag = ctx.new_page()
                pag.on("dialog", lambda d: d.dismiss())

                log("Abriendo el libro…")
                pag.goto(a.url, wait_until="load", timeout=120_000)
                pag.wait_for_function(
                    "() => (Array.isArray(window.fliphtml5_pages) && window.fliphtml5_pages.length > 0) || "
                    "document.querySelectorAll('img[src*=\"/files/large/\"]').length > 0",
                    timeout=120_000,
                )
                pag.evaluate("""
                    () => {
                      document.getElementById('tmpLoading')?.remove();
                      document.getElementById('miniFullscreen')?.remove();
                      document.getElementById('tmpContainer')?.click();
                    }
                """)
                pag.wait_for_timeout(1000)

                total = total_paginas(pag)
                hasta = a.hasta or total
                log(f"Páginas del libro: {total} · rango a extraer: {a.desde}-{hasta}")

                log("Obteniendo las URLs de las páginas…")
                urls = rastrear(pag, total, a.url)
                cache.write_text(json.dumps({str(k): v for k, v in urls.items()}, indent=2))
                ctx.close()
                nav.close()

        log(f"URLs obtenidas: {len(urls)}")
        log("Descargando páginas…")
        jpgs = bajar(urls, temporales, a.desde, hasta)

        if not jpgs:
            log("No se descargó ninguna página.")
            sys.exit(1)

        if a.sin_ocr:
            log(f"Generando PDF de imágenes con {len(jpgs)} páginas…")
            import img2pdf
            salida.write_bytes(img2pdf.convert([str(r) for r in jpgs]))
        else:
            log(f"Aplicando pasada de OCR con Tesseract ({a.idioma_ocr}) sobre {len(jpgs)} páginas…")
            hilos_ocr = max(1, os.cpu_count() or 2)
            armar_pdf_con_ocr(jpgs, salida, idioma=a.idioma_ocr, hilos=hilos_ocr)

        log(f"Listo: {salida.resolve()} ({salida.stat().st_size / (1024 * 1024):.2f} MB)")
    finally:
        shutil.rmtree(temporales, ignore_errors=True)


if __name__ == "__main__":
    main()
