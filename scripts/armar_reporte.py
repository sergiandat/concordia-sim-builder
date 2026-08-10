#!/usr/bin/env python3
"""
Arma un reporte legible a partir del log crudo de una simulación.

El log de Concordia tiene todo pero no se puede leer: para veinte pasos son
catorce megas de estructura anidada, en inglés y mezclada con el andamiaje
interno del motor. Esto lee los datos crudos —no el HTML ya generado, que es
lo que hace perder participantes al volver a parsearlo— y escribe un
documento que una persona puede recorrer.

    python scripts/armar_reporte.py salida/raw_log.json -o salida/reporte.html

No usa modelo de lenguaje: es lectura de datos, así que no gasta cuota, no
puede inventar nada y tarda lo que tarda leer el archivo.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- extracción

def texto_de(campo) -> str:
    """Los componentes guardan su contenido en distintas claves según el tipo."""
    if not isinstance(campo, dict):
        return str(campo or "").strip()
    for clave in ("State", "Value", "Summary"):
        v = campo.get(clave)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def limpiar_dicho(texto: str, quien: str) -> str:
    """Saca los prefijos que el motor antepone: 'Event: Nombre: Nombre -- "..."'."""
    t = re.sub(r"^Event:\s*", "", texto or "").strip()
    for _ in range(3):
        t = re.sub(r"^" + re.escape(quien) + r"\s*(?:--|:)\s*", "", t).strip()
    if len(t) > 1 and t[0] == '"' and t[-1] == '"':
        t = t[1:-1]
    return t.strip()


def leer_pasos(crudo) -> list[dict]:
    """Un registro por paso: quién habló, qué dijo, y qué resolvió la mesa."""
    pasos = []
    for bruto in crudo:
        if not isinstance(bruto, dict):
            continue
        numero = bruto.get("Step")
        if numero is None:
            continue

        claves_ent = [k for k in bruto if k.startswith("Entity [")]
        if not claves_ent:
            continue
        quien = claves_ent[0][len("Entity ["):-1]
        comp = bruto[claves_ent[0]] or {}

        dicho = limpiar_dicho(texto_de(comp.get("__act__")), quien)

        # El nombre de la clave de la mesa trae el evento ya resuelto
        evento = ""
        clave_mesa = ""
        for k in bruto:
            if " --- Event:" in k:
                clave_mesa = k
                evento = limpiar_dicho(k.split(" --- Event:", 1)[1], quien)
                break

        pasos.append({
            "n": numero,
            "quien": quien,
            "dicho": dicho,
            "evento": evento,
            "objetivo": texto_de(comp.get("Goal")),
            "situacion": texto_de(comp.get("SituationPerception")),
            "persona": texto_de(comp.get("SelfPerception")),
            "haria": texto_de(comp.get("PersonBySituation")),
            "mesa": leer_mesa(bruto.get(clave_mesa) or {}),
        })
    return sorted(pasos, key=lambda p: p["n"])


def decision_de(componente) -> str:
    """Cada decisión de la mesa vive en el __act__ de su propio componente."""
    if not isinstance(componente, dict):
        return ""
    act = componente.get("__act__")
    return texto_de(act) if isinstance(act, dict) else ""


def leer_mesa(bloque: dict) -> dict:
    """
    Lo que decidió quien modera en este turno: si daba por terminada la
    deliberación, a quién le dio la palabra y con qué consigna. La consigna
    importa más de lo que parece: es lo que enmarca la respuesta de quien habla.
    """
    consigna = decision_de(bloque.get("next_action_spec"))
    pedido = ""
    if consigna:
        try:
            pedido = json.loads(consigna).get("call_to_action", "")
        except (json.JSONDecodeError, AttributeError, TypeError):
            m = re.search(r'"call_to_action":\s*"((?:[^"\\]|\\.)*)"', consigna)
            pedido = m.group(1).replace('\\"', '"').replace("\\n", " ") if m else consigna
    pedido = re.sub(r"\s*(For example|Por ejemplo)[,:].*$", "", pedido, flags=re.S).strip()
    pedido = re.sub(r"\s+", " ", pedido)

    return {
        "termina": decision_de(bloque.get("terminate")),
        "siguiente": decision_de(bloque.get("next_acting")),
        "consigna": pedido[:400],
    }


PAT_VARS = re.compile(r"\[VARIABLES:\s*([^\]]{1,600})\]")


def leer_indicadores(crudo) -> tuple[dict[str, list[tuple[int, float]]],
                                     dict[str, list[tuple[int, str]]]]:
    """
    Devuelve dos conjuntos: los indicadores numéricos, que van al gráfico de
    líneas, y los de estado —categóricos y sí/no—, que se muestran como una
    franja. Estos últimos suelen ser los que registran qué se decidió, así que
    dejarlos afuera del reporte, como pasaba antes, escondía el resultado.

    Los valores viajan dentro del contexto que ve cada participante; se toma el
    último anuncio de cada paso y se descartan los ejemplos de la plantilla.
    """
    numericos: dict[int, dict[str, float]] = {}
    estados: dict[int, dict[str, str]] = {}

    for bruto in crudo:
        if not isinstance(bruto, dict):
            continue
        n = bruto.get("Step")
        if n is None:
            continue
        for encontrado in PAT_VARS.findall(json.dumps(bruto, ensure_ascii=False)):
            if "name1" in encontrado:
                continue
            for par in encontrado.split(","):
                if "=" not in par:
                    continue
                nombre, valor = par.split("=", 1)
                nombre = nombre.strip().strip('"\\ ')
                valor = valor.strip().strip('"\\ ')
                if not nombre or not valor:
                    continue
                try:
                    numericos.setdefault(n, {})[nombre] = float(valor)
                except ValueError:
                    estados.setdefault(n, {})[nombre] = valor

    series: dict[str, list[tuple[int, float]]] = {}
    for n in sorted(numericos):
        for nombre, valor in numericos[n].items():
            series.setdefault(nombre, []).append((n, valor))

    franjas: dict[str, list[tuple[int, str]]] = {}
    for n in sorted(estados):
        for nombre, valor in estados[n].items():
            franjas.setdefault(nombre, []).append((n, valor))
    return series, franjas


def tramos(serie: list[tuple[int, str]], max_paso: int) -> list[tuple[str, int, int]]:
    """Agrupa pasos consecutivos con el mismo valor: (valor, desde, hasta)."""
    if not serie:
        return []
    salida, valor_actual, desde = [], serie[0][1], serie[0][0]
    for paso, valor in serie[1:]:
        if valor != valor_actual:
            salida.append((valor_actual, desde, paso - 1))
            valor_actual, desde = valor, paso
    salida.append((valor_actual, desde, max_paso))
    return salida


def franja(nombre: str, serie: list[tuple[int, str]], max_paso: int) -> str:
    """Línea de estados: cuánto duró cada valor y dónde cambió."""
    partes = tramos(serie, max_paso)
    if not partes:
        return ""
    total = max(1, max_paso)
    valores_unicos = []
    for v, _, _ in partes:
        if v not in valores_unicos:
            valores_unicos.append(v)

    p = ['<div class="franja">']
    p.append(f'<div class="franja-nombre">{html.escape(bonito(nombre))}</div>')
    p.append('<div class="franja-barra">')
    for valor, desde, hasta in partes:
        ancho = max(2.0, 100.0 * (hasta - desde + 1) / total)
        color = COLORES[valores_unicos.index(valor) % len(COLORES)]
        etiqueta = f"{valor} · pasos {desde}–{hasta}" if hasta > desde else f"{valor} · paso {desde}"
        p.append(f'<div class="tramo" style="width:{ancho:.1f}%;background:{color}" '
                 f'title="{html.escape(etiqueta)}">'
                 f'<span>{html.escape(valor)}</span></div>')
    p.append("</div>")
    if len(partes) > 1:
        cambios = ", ".join(f"paso {d}: {html.escape(v)}" for v, d, _ in partes[1:])
        p.append(f'<p class="franja-cambios">Cambió en {cambios}</p>')
    else:
        p.append(f'<p class="franja-cambios">No cambió en toda la deliberación: '
                 f'quedó en <strong>{html.escape(partes[0][0])}</strong>.</p>')
    p.append("</div>")
    return "".join(p)


# ---------------------------------------------------------------- gráfico

COLORES = ["#2d5f5d", "#8a6a1f", "#7a4b6b", "#3d6b8a", "#8f5a3a", "#4a7a4a"]


def grafico(series: dict[str, list[tuple[int, float]]], max_paso: int) -> str:
    """Líneas en SVG. Sin librerías: tiene que abrir en cualquier navegador."""
    if not series or max_paso < 1:
        return ""

    an, al = 720, 260
    izq, der, arr, aba = 46, 16, 16, 34
    ancho, alto = an - izq - der, al - arr - aba

    todos = [v for s in series.values() for _, v in s]
    lo, hi = min(todos), max(todos)
    if hi == lo:
        hi, lo = hi + 1, lo - 1
    margen = (hi - lo) * 0.1
    lo, hi = lo - margen, hi + margen

    def x(paso):
        return izq + (ancho * (paso - 1) / max(1, max_paso - 1))

    def y(valor):
        return arr + alto - (alto * (valor - lo) / (hi - lo))

    p = [f'<svg viewBox="0 0 {an} {al}" role="img" aria-label="Indicadores a lo largo de la deliberación">']

    for i in range(5):  # rejilla horizontal
        v = lo + (hi - lo) * i / 4
        yy = y(v)
        p.append(f'<line x1="{izq}" y1="{yy:.1f}" x2="{an-der}" y2="{yy:.1f}" class="rejilla"/>')
        p.append(f'<text x="{izq-8}" y="{yy+4:.1f}" class="eje-y">{v:.0f}</text>')

    for paso in range(1, max_paso + 1):  # marcas del eje x
        if max_paso <= 12 or paso == 1 or paso == max_paso or paso % 5 == 0:
            p.append(f'<text x="{x(paso):.1f}" y="{al-12}" class="eje-x">{paso}</text>')

    for i, (nombre, serie) in enumerate(sorted(series.items())):
        color = COLORES[i % len(COLORES)]
        puntos = " ".join(f"{x(n):.1f},{y(v):.1f}" for n, v in serie)
        p.append(f'<polyline points="{puntos}" fill="none" stroke="{color}" '
                 f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
        n_fin, v_fin = serie[-1]
        p.append(f'<circle cx="{x(n_fin):.1f}" cy="{y(v_fin):.1f}" r="3.5" fill="{color}"/>')

    p.append("</svg>")

    leyenda = ['<ul class="leyenda">']
    for i, (nombre, serie) in enumerate(sorted(series.items())):
        color = COLORES[i % len(COLORES)]
        ini, fin = serie[0][1], serie[-1][1]
        flecha = "subió" if fin > ini else ("bajó" if fin < ini else "sin cambio")
        leyenda.append(
            f'<li><span class="punto" style="background:{color}"></span>'
            f'<span class="leyenda-nombre">{html.escape(bonito(nombre))}</span>'
            f'<span class="leyenda-dato">{ini:.0f} → {fin:.0f} · {flecha}</span></li>')
    leyenda.append("</ul>")
    return "".join(p) + "".join(leyenda)


def bonito(clave: str) -> str:
    return clave.replace("_", " ").strip().capitalize()


# ---------------------------------------------------------------- documento

def parrafos(texto: str) -> str:
    trozos = [t.strip() for t in re.split(r"\n\s*\n|\n", texto or "") if t.strip()]
    return "".join(f"<p>{html.escape(t)}</p>" for t in trozos) or "<p class='vacio'>—</p>"


def exportar_csv(pasos, series, destino: Path) -> None:
    """Una fila por turno, para abrir en una planilla."""
    import csv
    nombres = sorted(series)
    valores = {}
    for nombre, serie in series.items():
        for n, v in serie:
            valores.setdefault(n, {})[nombre] = v

    with destino.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["paso", "participante", "objetivo", "consigna",
                    "palabras", "dicho"] + nombres)
        for p in pasos:
            dicho = p["dicho"] or p["evento"]
            fila = [p["n"], p["quien"], p["objetivo"],
                    (p.get("mesa") or {}).get("consigna", ""),
                    len(dicho.split()), dicho]
            fila += [valores.get(p["n"], {}).get(n, "") for n in nombres]
            w.writerow(fila)


PROMPT_RESUMEN = """Sos analista y tenés que explicarle a alguien que no participó cómo fue esta deliberación.

CONTEXTO DEL ESCENARIO
{contexto}

PARTICIPANTES Y LO QUE BUSCA CADA UNO
{participantes}

INDICADORES (valor inicial y final)
{indicadores}

LA DELIBERACIÓN, TURNO POR TURNO
{transcripcion}

Escribí un resumen en castellano rioplatense, de cuatro a seis párrafos, que cubra:
1. Qué se discutió y qué se resolvió, si es que se resolvió algo.
2. Qué defendió cada participante y si su posición cambió a lo largo de los turnos.
3. Dónde hubo desacuerdo real y dónde hubo adhesión sin reparos. Sé específico: si alguien aceptó una propuesta sin objetar nada, decilo y señalá en qué turno.
4. Si los indicadores se movieron de forma coherente con lo que efectivamente se dijo.

Reglas estrictas:
- Basate únicamente en lo que aparece en la transcripción. No inventes citas, hechos ni acuerdos.
- Si algo no se resolvió o quedó ambiguo, decilo con todas las letras en lugar de completarlo.
- Nada de vocabulario de manual ni conclusiones infladas. Si la deliberación fue floja, decilo.
- No uses títulos ni viñetas: párrafos corridos.

Sobre el nivel de consenso, si se midió: que llegue al máximo NO es un buen resultado por sí mismo. En una mesa donde los participantes fueron diseñados con intereses en conflicto, un consenso altísimo alcanzado sin objeciones sostenidas indica que los personajes no defendieron sus posiciones, y eso es un problema del escenario que hay que señalar, no un logro que celebrar.

No cierres con un veredicto sobre si la deliberación estuvo bien o mal, ni con una frase de síntesis elogiosa. Terminá con lo que quedó sin resolver o con lo que habría que revisar del escenario."""


def pedir_a_gemini(prompt: str, modelo: str, clave: str, timeout: int = 180) -> str:
    """
    Llamada REST con biblioteca estándar. A propósito no se usa el motor de
    simulación: así este script corre sobre resultados viejos, en una máquina
    sin las dependencias pesadas instaladas, o lo corre otra persona.
    """
    import urllib.error
    import urllib.request

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{modelo}:generateContent?key={clave}")
    cuerpo = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 4000},
    }).encode("utf-8")
    pedido = urllib.request.Request(
        url, data=cuerpo, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(pedido, timeout=timeout) as r:
        datos = json.loads(r.read().decode("utf-8"))
    partes = datos.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in partes).strip()


def redactar_resumen(pasos, series, resumen_datos, premisa: str) -> str:
    """
    Resumen ejecutivo con el modelo. Devuelve cadena vacía si algo falla: el
    reporte tiene que salir igual, porque el resto no depende de esto.
    """
    import os
    clave = os.getenv("GEMINI_API_KEY", "").strip()
    if not clave:
        print("  resumen omitido: falta GEMINI_API_KEY")
        return ""

    objetivos, orden = {}, []
    for p in pasos:
        if p["quien"] not in orden:
            orden.append(p["quien"])
        if p["objetivo"] and p["quien"] not in objetivos:
            objetivos[p["quien"]] = p["objetivo"]

    participantes = "\n".join(f"- {q}: {objetivos.get(q, 'sin objetivo declarado')}" for q in orden)
    indicadores = "\n".join(
        f"- {bonito(n)}: empezó en {s[0][1]:.0f} y terminó en {s[-1][1]:.0f}"
        for n, s in sorted(series.items())) or "(no se midieron indicadores)"
    transcripcion = "\n\n".join(
        f"Turno {p['n']} — {p['quien']}:\n{(p['dicho'] or p['evento'])[:1200]}" for p in pasos)

    prompt = PROMPT_RESUMEN.format(
        contexto=(premisa or "(no se registró la consigna del escenario)")[:2000],
        participantes=participantes,
        indicadores=indicadores,
        transcripcion=transcripcion[:60000],
    )

    # Se reusa el modelo de la corrida para no introducir uno nuevo sin aviso
    crudo_nombre = (resumen_datos or {}).get("modelo_gm") or (resumen_datos or {}).get("modelo") or ""
    modelo = crudo_nombre.split("/")[-1].strip() or "gemini-3.5-flash-lite"
    try:
        return pedir_a_gemini(prompt, modelo, clave)
    except Exception as e:
        detalle = getattr(e, "reason", None) or e
        print(f"  resumen omitido: {type(e).__name__}: {str(detalle)[:160]}")
        return ""


def armar(pasos, series, franjas, resumen, decisiones, texto_resumen="") -> str:
    titulo = "Acta de la deliberación"
    quienes = []
    for p in pasos:
        if p["quien"] not in quienes:
            quienes.append(p["quien"])

    max_paso = max((p["n"] for p in pasos), default=0)
    veces = {q: sum(1 for p in pasos if p["quien"] == q) for q in quienes}
    objetivos = {}
    for p in pasos:
        if p["objetivo"] and p["quien"] not in objetivos:
            objetivos[p["quien"]] = p["objetivo"]

    por_paso = {d["step"]: d.get("event", "") for d in (decisiones or [])}

    def ficha(rot, val):
        return (f'<div class="dato"><dt>{html.escape(rot)}</dt>'
                f'<dd>{html.escape(str(val))}</dd></div>')

    partes = [CABEZA, f"<h1>{titulo}</h1>"]

    # --- resumen de la corrida
    partes.append('<dl class="datos">')
    partes.append(ficha("Participantes", len(quienes)))
    partes.append(ficha("Turnos", max_paso))
    if resumen:
        if resumen.get("duracion_min") is not None:
            partes.append(ficha("Duración", f'{resumen["duracion_min"]} minutos'))
        if resumen.get("completa") is not None:
            partes.append(ficha("Estado", "Completa" if resumen["completa"] else "Incompleta"))
    partes.append("</dl>")

    # --- resumen redactado por el modelo
    if texto_resumen:
        partes.append('<div class="resumen">')
        partes.append('<p class="marca-ia">Resumen generado automáticamente</p>')
        partes.append("<h2>En pocas palabras</h2>")
        partes.append(parrafos(texto_resumen))
        partes.append("</div>")

    # --- indicadores
    if series or franjas:
        partes.append('<section><h2>Cómo evolucionaron los indicadores</h2>')
        if series:
            partes.append('<div class="grafico">' + grafico(series, max_paso) + "</div>")
        if franjas:
            partes.append('<div class="grafico franjas">')
            for nombre, serie in sorted(franjas.items()):
                partes.append(franja(nombre, serie, max_paso))
            partes.append("</div>")
        partes.append("</section>")

    # --- quiénes participaron
    partes.append('<section><h2>Quiénes participaron</h2>')
    for q in quienes:
        partes.append('<article class="participante">')
        partes.append(f'<h3>{html.escape(q)}</h3>')
        partes.append(f'<p class="veces">Habló {veces[q]} '
                      f'{"vez" if veces[q] == 1 else "veces"}</p>')
        if objetivos.get(q):
            partes.append(f'<p class="objetivo">{html.escape(objetivos[q])}</p>')
        partes.append("</article>")
    partes.append("</section>")

    # --- cómo condujo la mesa
    consignas = [p["mesa"]["consigna"] for p in pasos if p.get("mesa", {}).get("consigna")]
    cierres = [p["mesa"]["termina"] for p in pasos if p.get("mesa", {}).get("termina")]
    if consignas or cierres:
        partes.append('<section><h2>Cómo condujo la mesa</h2>')
        distintas = []
        for c in consignas:
            if c not in distintas:
                distintas.append(c)
        if distintas:
            partes.append('<p class="ayuda-sec">La consigna es la pregunta con la que se le '
                          'da la palabra a cada participante. Cuando es siempre la misma, todos '
                          'responden al mismo estímulo; cuando cambia según lo que se viene '
                          'discutiendo, a cada uno se le pide postura sobre algo concreto.</p>')
            if len(distintas) == 1:
                partes.append('<p class="ayuda-sec"><strong>Una sola consigna para los '
                              f'{len(consignas)} turnos.</strong></p>')
            else:
                partes.append(f'<p class="ayuda-sec"><strong>{len(distintas)} consignas '
                              f'distintas</strong> en {len(consignas)} turnos.</p>')
            partes.append('<ul class="consignas">')
            for c in distintas[:8]:
                veces = consignas.count(c)
                partes.append(f'<li><span class="cuantas">{veces}×</span>'
                              f'<span>{html.escape(c)}</span></li>')
            partes.append("</ul>")
            # Sin esta línea el corte era invisible y parecía que no hubo más
            if len(distintas) > 8:
                partes.append(f'<p class="franja-cambios">Y {len(distintas) - 8} consignas '
                              f'más, distintas entre sí.</p>')
        if cierres:
            quiso = sum(1 for c in cierres if c.strip().lower().startswith(("s", "y")))
            partes.append(f'<p class="cierre-nota">Se preguntó en {len(cierres)} turnos si la '
                          f'deliberación había terminado. '
                          + (f'En {quiso} respondió que sí.' if quiso
                             else 'Siempre respondió que no, así que se usaron todos los turnos.')
                          + "</p>")
        partes.append("</section>")

    # --- la deliberación
    partes.append('<section><h2>La deliberación</h2>')
    for p in pasos:
        marca = por_paso.get(p["n"])
        if marca:
            partes.append('<div class="hito"><span class="hito-rotulo">Momento de decisión</span>'
                          f'<p>{html.escape(marca)}</p></div>')
        partes.append('<article class="turno">')
        partes.append(f'<header><span class="paso">{p["n"]}</span>'
                      f'<span class="orador">{html.escape(p["quien"])}</span></header>')
        mesa = p.get("mesa") or {}
        if mesa.get("consigna"):
            partes.append(f'<p class="consigna">{html.escape(mesa["consigna"])}</p>')
        partes.append(f'<div class="dicho">{parrafos(p["dicho"] or p["evento"])}</div>')
        if p["situacion"]:
            partes.append('<details class="interno"><summary>Cómo veía la situación</summary>'
                          f'<div>{parrafos(p["situacion"])}</div></details>')
        partes.append("</article>")
    partes.append("</section>")

    partes.append('<footer class="pie">Generado a partir del registro de la '
                  'simulación. Cada turno reproduce lo que dijo el participante, '
                  'sin resumir ni interpretar.</footer>')
    partes.append("</main></body></html>")
    return "".join(partes)


CABEZA = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Acta de la deliberación</title>
<style>
:root{--ground:#f7f7f5;--surface:#fff;--surface-alt:#f1f2ef;--ink:#17191c;--ink-soft:#5b6168;
--ink-faint:#878d94;--rule:#dfe0dc;--rule-strong:#c9cbc5;--accent:#2d5f5d;--warn:#8a6a1f;
--serif:Georgia,"Iowan Old Style","Times New Roman",serif;
--sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--ground:#131619;--surface:#1a1e22;--surface-alt:#22272c;--ink:#e9ecee;--ink-soft:#9aa2aa;
--ink-faint:#757d85;--rule:#2b3137;--rule-strong:#3b434a;--accent:#6fb3ae;--warn:#d3a84e}}
:root[data-theme="dark"]{--ground:#131619;--surface:#1a1e22;--surface-alt:#22272c;--ink:#e9ecee;
--ink-soft:#9aa2aa;--ink-faint:#757d85;--rule:#2b3137;--rule-strong:#3b434a;--accent:#6fb3ae;--warn:#d3a84e}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
font-size:16px;line-height:1.65}
main{max-width:44rem;margin:0 auto;padding:3rem 1.25rem 5rem}
h1{font-family:var(--serif);font-weight:400;font-size:2.15rem;line-height:1.15;
margin:0 0 1.75rem;text-wrap:balance;letter-spacing:-.01em}
h2{font-family:var(--serif);font-weight:400;font-size:1.4rem;margin:0 0 1.25rem;
padding-bottom:.5rem;border-bottom:1px solid var(--rule)}
h3{font-size:1rem;margin:0 0 .25rem}
section{margin-top:3.25rem}
.datos{display:flex;flex-wrap:wrap;gap:1.75rem;margin:0;padding:1.15rem 1.3rem;
background:var(--surface);border:1px solid var(--rule);border-radius:8px}
.dato dt{font-size:.72rem;letter-spacing:.09em;text-transform:uppercase;color:var(--ink-faint);
font-family:var(--mono)}
.dato dd{margin:.15rem 0 0;font-size:1.15rem;font-variant-numeric:tabular-nums}
.grafico{background:var(--surface);border:1px solid var(--rule);border-radius:8px;
padding:1.1rem 1.2rem;overflow-x:auto}
svg{display:block;width:100%;min-width:34rem;height:auto}
.rejilla{stroke:var(--rule);stroke-width:1}
.eje-y,.eje-x{fill:var(--ink-faint);font-size:11px;font-family:var(--mono)}
.eje-y{text-anchor:end}.eje-x{text-anchor:middle}
.leyenda{list-style:none;margin:1rem 0 0;padding:0;display:flex;flex-direction:column;gap:.4rem}
.leyenda li{display:flex;align-items:center;gap:.6rem;font-size:.88rem}
.punto{width:.7rem;height:.7rem;border-radius:2px;flex:none}
.leyenda-nombre{flex:1;min-width:0}
.leyenda-dato{font-family:var(--mono);color:var(--ink-soft);font-variant-numeric:tabular-nums}
.participante{padding:.9rem 0;border-top:1px solid var(--rule)}
.participante:first-of-type{border-top:none}
.veces{margin:0;font-size:.82rem;color:var(--ink-faint);font-family:var(--mono)}
.objetivo{margin:.5rem 0 0;color:var(--ink-soft);font-size:.94rem}
.turno{background:var(--surface);border:1px solid var(--rule);border-radius:8px;
padding:1.2rem 1.35rem;margin-bottom:1rem}
.turno header{display:flex;align-items:center;gap:.7rem;margin-bottom:.7rem}
.paso{font-family:var(--mono);font-size:.75rem;color:var(--accent);
border:1px solid var(--rule-strong);border-radius:3px;padding:.08rem .4rem;flex:none}
.orador{font-weight:600;font-size:.92rem}
.dicho p{margin:0 0 .8rem}.dicho p:last-child{margin-bottom:0}
.vacio{color:var(--ink-faint)}
.interno{margin-top:.9rem;border-top:1px solid var(--rule);padding-top:.7rem}
.interno summary{cursor:pointer;font-size:.82rem;color:var(--ink-faint);font-family:var(--mono)}
.interno summary:hover{color:var(--accent)}
.interno>div{margin-top:.6rem;font-size:.92rem;color:var(--ink-soft)}
.hito{border-left:3px solid var(--warn);background:var(--surface-alt);
padding:.85rem 1.1rem;border-radius:0 6px 6px 0;margin:1.6rem 0 1rem}
.hito-rotulo{font-family:var(--mono);font-size:.68rem;letter-spacing:.1em;
text-transform:uppercase;color:var(--warn)}
.hito p{margin:.4rem 0 0;font-size:.93rem}
.consigna{margin:0 0 .8rem;padding-left:.7rem;border-left:2px solid var(--rule-strong);
color:var(--ink-faint);font-size:.83rem;font-style:italic}
.ayuda-sec{margin:0 0 1rem;color:var(--ink-soft);font-size:.93rem}
.consignas{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:.55rem}
.consignas li{display:flex;gap:.75rem;align-items:baseline;background:var(--surface);
border:1px solid var(--rule);border-radius:6px;padding:.7rem .9rem;font-size:.9rem}
.cuantas{font-family:var(--mono);color:var(--accent);flex:none;font-variant-numeric:tabular-nums}
.cierre-nota{margin:1.1rem 0 0;color:var(--ink-soft);font-size:.9rem}
.resumen{background:var(--surface);border:1px solid var(--rule);border-left:3px solid var(--accent);
border-radius:0 8px 8px 0;padding:1.3rem 1.5rem;margin-bottom:2rem}
.resumen h2{border:none;margin-bottom:.8rem;padding:0;font-size:1.25rem}
.resumen p{margin:0 0 .85rem}.resumen p:last-child{margin-bottom:0}
.resumen .marca-ia{font-family:var(--mono);font-size:.68rem;letter-spacing:.1em;
text-transform:uppercase;color:var(--ink-faint);margin-bottom:.6rem}
.franjas{display:flex;flex-direction:column;gap:1.2rem;margin-top:1rem}
.franja-nombre{font-size:.86rem;font-weight:600;margin-bottom:.45rem}
.franja-barra{display:flex;gap:2px;border-radius:5px;overflow:hidden}
.tramo{min-width:0;padding:.5rem .6rem;color:#fff;font-size:.78rem;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.franja-cambios{margin:.45rem 0 0;font-size:.84rem;color:var(--ink-soft)}
.pie{margin-top:3.5rem;padding-top:1.25rem;border-top:1px solid var(--rule);
color:var(--ink-faint);font-size:.86rem}
@media (max-width:34rem){h1{font-size:1.7rem}main{padding-top:2rem}}
</style></head><body><main>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("crudo", type=Path, help="raw_log.json de la corrida")
    ap.add_argument("-o", "--salida", type=Path, default=None)
    ap.add_argument("--escenario", type=Path, default=None,
                    help="JSON del escenario, para marcar los momentos de decisión")
    ap.add_argument("--resumen", action="store_true",
                    help="agregar un resumen ejecutivo redactado por el modelo")
    ap.add_argument("--csv", action="store_true",
                    help="exportar tambien un CSV con una fila por turno")
    args = ap.parse_args()

    if not args.crudo.is_file():
        print(f"ERROR: no existe {args.crudo}", file=sys.stderr)
        return 1

    crudo = json.loads(args.crudo.read_text(encoding="utf-8"))
    pasos = leer_pasos(crudo)
    if not pasos:
        print("ERROR: el registro no tiene pasos legibles", file=sys.stderr)
        return 1

    series, franjas = leer_indicadores(crudo)

    resumen = {}
    ruta_res = args.crudo.parent / "resumen.json"
    if ruta_res.is_file():
        try:
            resumen = json.loads(ruta_res.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    decisiones, premisa = [], ""
    if args.escenario and args.escenario.is_file():
        try:
            esc = json.loads(args.escenario.read_text(encoding="utf-8"))
            cfg = esc.get("config", esc)
            decisiones = (cfg.get("game_master") or {}).get("critical_decision_points") or []
            premisa = cfg.get("premise", "")
        except (json.JSONDecodeError, AttributeError):
            pass

    texto_resumen = ""
    if args.resumen:
        print("  redactando el resumen...")
        texto_resumen = redactar_resumen(pasos, series, resumen, premisa)

    salida = args.salida or args.crudo.parent / "reporte.html"
    salida.write_text(armar(pasos, series, franjas, resumen, decisiones, texto_resumen), encoding="utf-8")

    print(f"reporte      : {salida}")
    print(f"turnos       : {len(pasos)}")
    print(f"participantes: {len({p['quien'] for p in pasos})}")
    print(f"indicadores  : {len(series)}" + (f" ({', '.join(sorted(series))})" if series else ""))
    print(f"decisiones   : {len(decisiones)}")
    print(f"resumen      : {'si' if texto_resumen else 'no'}")

    if args.csv:
        ruta_csv = salida.with_suffix(".csv")
        exportar_csv(pasos, series, ruta_csv)
        print(f"csv          : {ruta_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
