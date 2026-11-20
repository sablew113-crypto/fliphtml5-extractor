#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compaginar_cuadernillos.py — impone un PDF en cuadernillos (signatures) para
imprimir en hojas A4 apaisadas (2 páginas por carilla, frente y dorso),
doblar al medio, coser cada cuadernillo y pegarlos juntos.

Por defecto:
- Omite la página 1 si es la portada a color (--desde-pagina 2).
- Cuadernillos de 4 hojas A4 dobladas al medio = 16 páginas de libro por cuadernillo.
- Orientación A4 apaisada (841.89 x 595.28 pt) pensada para impresión doble faz
  con giro por borde corto (estándar en A4 horizontal). Si tu impresora está
  configurada en "voltear por borde largo", pasá --borde-largo para rotar 180°
  las caras pares (dorsos).
"""

import argparse
from pathlib import Path
import pikepdf

A4_ANCHO_PT = 841.89
A4_ALTO_PT = 595.28
MITAD_ANCHO_PT = A4_ANCHO_PT / 2.0


def orden_cuadernillo(total_paginas_cuadernillo):
    """
    Devuelve la lista de pares (izquierda, derecha) de índices 0..N-1
    para las 2*(N/4) caras (frente y dorso de cada hoja A4 del cuadernillo).
    """
    hojas = total_paginas_cuadernillo // 4
    pares = []
    for h in range(hojas):
        # Frente de la hoja h (exterior cuando se dobla)
        izq_frente = total_paginas_cuadernillo - 1 - 2 * h
        der_frente = 2 * h
        pares.append((izq_frente, der_frente, False))
        # Dorso de la hoja h (interior cuando se dobla)
        izq_dorso = 2 * h + 1
        der_dorso = total_paginas_cuadernillo - 2 - 2 * h
        pares.append((izq_dorso, der_dorso, True))
    return pares


def colocar_pagina(destino_pdf, hoja_a4, pagina_origen, lado_derecho):
    """Coloca pagina_origen centrada en la mitad izquierda o derecha de hoja_a4."""
    if pagina_origen is None:
        return
    form_xobj = destino_pdf.copy_foreign(pagina_origen.as_form_xobject())
    mbox = pagina_origen.mediabox
    orig_w = float(mbox[2]) - float(mbox[0])
    orig_h = float(mbox[3]) - float(mbox[1])

    escala = min(MITAD_ANCHO_PT / orig_w, A4_ALTO_PT / orig_h)
    nuevo_w = orig_w * escala
    nuevo_h = orig_h * escala

    offset_x_base = MITAD_ANCHO_PT if lado_derecho else 0.0
    # Centrar verticalmente y pegar levemente hacia el lomo central para compensar el doblez
    x = offset_x_base + (MITAD_ANCHO_PT - nuevo_w) / 2.0
    y = (A4_ALTO_PT - nuevo_h) / 2.0

    recursos = hoja_a4.get("/Resources")
    if recursos is None:
        hoja_a4["/Resources"] = pikepdf.Dictionary()
        recursos = hoja_a4["/Resources"]
    xobjs = recursos.get("/XObject")
    if xobjs is None:
        recursos["/XObject"] = pikepdf.Dictionary()
        xobjs = recursos["/XObject"]

    nombre = f"/P{'R' if lado_derecho else 'L'}"
    xobjs[nombre] = form_xobj

    comando = f"q {escala:.6f} 0 0 {escala:.6f} {x:.2f} {y:.2f} cm {nombre} Do Q\n"
    hoja_a4.contents_add(destino_pdf.make_stream(comando.encode("ascii")))


def compaginar(
    ruta_entrada,
    ruta_salida,
    hojas_por_cuadernillo=4,
    desde_pagina=2,
    borde_largo=False,
):
    paginas_por_cuadernillo = hojas_por_cuadernillo * 4
    with pikepdf.Pdf.open(ruta_entrada) as src:
        # Páginas del interior (1-indexed -> 0-indexed)
        paginas_libro = [src.pages[i] for i in range(desde_pagina - 1, len(src.pages))]
        total_interior = len(paginas_libro)

        # Rellenar con páginas en blanco (None) al final hasta múltiplo de paginas_por_cuadernillo
        resto = total_interior % paginas_por_cuadernillo
        blancas = (paginas_por_cuadernillo - resto) % paginas_por_cuadernillo
        secuencia = paginas_libro + [None] * blancas
        num_cuadernillos = len(secuencia) // paginas_por_cuadernillo

        print(
            f"Interior: {total_interior} páginas (desde pág. {desde_pagina} del PDF original) "
            f"+ {blancas} en blanco al final = {len(secuencia)} páginas."
        )
        print(
            f"Armando {num_cuadernillos} cuadernillos de {hojas_por_cuadernillo} hojas A4 "
            f"({paginas_por_cuadernillo} páginas c/u) → {num_cuadernillos * hojas_por_cuadernillo} hojas A4 "
            f"({num_cuadernillos * hojas_por_cuadernillo * 2} carillas en el PDF)…"
        )

        out = pikepdf.Pdf.new()
        plantilla = orden_cuadernillo(paginas_por_cuadernillo)

        for c in range(num_cuadernillos):
            base = c * paginas_por_cuadernillo
            bloque = secuencia[base : base + paginas_por_cuadernillo]
            for izq_idx, der_idx, es_dorso in plantilla:
                hoja = out.add_blank_page(page_size=(A4_ANCHO_PT, A4_ALTO_PT))
                colocar_pagina(out, hoja, bloque[izq_idx], lado_derecho=False)
                colocar_pagina(out, hoja, bloque[der_idx], lado_derecho=True)
                if es_dorso and borde_largo:
                    hoja["/Rotate"] = 180

        out.save(
            ruta_salida,
            compress_streams=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
        )
        print(f"Listo: {ruta_salida} ({Path(ruta_salida).stat().st_size / (1024 * 1024):.2f} MB)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("entrada", nargs="?", default="Amanecer en la cosecha.pdf")
    ap.add_argument(
        "salida",
        nargs="?",
        default="Amanecer en la cosecha - Cuadernillos A4 (4 hojas).pdf",
    )
    ap.add_argument("--hojas-cuadernillo", type=int, default=4)
    ap.add_argument("--desde-pagina", type=int, default=2)
    ap.add_argument("--borde-largo", action="store_true")
    a = ap.parse_args()
    compaginar(a.entrada, a.salida, a.hojas_cuadernillo, a.desde_pagina, a.borde_largo)


if __name__ == "__main__":
    main()
