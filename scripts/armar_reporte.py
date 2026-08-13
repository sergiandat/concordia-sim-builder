#!/usr/bin/env python3
"""
Arma un reporte legible a partir del log crudo de una simulación.

El log de Concordia tiene todo pero no se puede leer: para veinte pasos son
catorce megas de estructura anidada, en inglés y mezclada con el andamiaje
interno del motor. Esto lee los datos crudos —no el HTML ya generado, que es
lo que hace perder actores al volver a parsearlo— y escribe un
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
    """Un registro por paso: quién habló, qué dijo, y qué resolvió el narrador."""
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
            # Las tres preguntas que Concordia le hace a un actor antes
            # de actuar. Mostrar solo una dejaba afuera dos tercios del
            # razonamiento con el que decide.
            "persona": texto_de(comp.get("SelfPerception")),
            "situacion": texto_de(comp.get("SituationPerception")),
            "haria": texto_de(comp.get("PersonBySituation")),
            "razonamiento": {
                campo: (comp.get(campo) or {}).get("Chain of thought") or []
                for campo in ("SelfPerception", "SituationPerception", "PersonBySituation")
            },
            "memoria": leer_memoria(comp.get("relevant_memories")),
            "mesa": leer_mesa(bruto.get(clave_mesa) or {}),
        })
    return sorted(pasos, key=lambda p: p["n"])


def leer_memoria(campo) -> dict:
    """
    Qué recordó el actor antes de hablar. Concordia busca en su memoria
    asociativa con una consulta y trae los recuerdos más cercanos; ver cuáles
    fueron explica por qué respondió lo que respondió, y por qué a veces ignora
    algo que sí se dijo.
    """
    if not isinstance(campo, dict):
        return {}
    recuerdos = []
    for linea in str(campo.get("Value", "")).split("\n"):
        t = re.sub(r"^\[observation\]\s*(\[\w+\]\s*)?", "", linea.strip()).strip()
        t = re.sub(r"^Event:\s*(\*\*Event:\*\*)?\s*", "", t).strip()
        if len(t) > 15:
            recuerdos.append(t)
    return {
        "consulta": re.sub(r"\s+", " ", str(campo.get("Query", ""))).strip(),
        "recuerdos": recuerdos,
    }


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

    # A cada actor le escribe una observación distinta, redactada desde
    # su punto de vista. Es la parte del ciclo donde el narrador sí interviene.
    observaciones = {}
    mo = bloque.get("make_observation")
    if isinstance(mo, dict):
        for quien, comp in mo.items():
            if isinstance(comp, dict):
                t = decision_de(comp)
                if t:
                    observaciones[quien] = t

    return {
        "termina": decision_de(bloque.get("terminate")),
        "siguiente": decision_de(bloque.get("next_acting")),
        "consigna": pedido[:400],
        "observaciones": observaciones,
    }


PAT_VARS = re.compile(r"\[VARIABLES:\s*([^\]]{1,600})\]")


def leer_indicadores(crudo) -> tuple[dict[str, list[tuple[int, float]]],
                                     dict[str, list[tuple[int, str]]]]:
    """
    Devuelve dos conjuntos: los indicadores numéricos, que van al gráfico de
    líneas, y los de estado —categóricos y sí/no—, que se muestran como una
    franja. Estos últimos suelen ser los que registran qué se decidió, así que
    dejarlos afuera del reporte, como pasaba antes, escondía el resultado.

    Los valores viajan dentro del contexto que ve cada actor; se toma el
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


def grafico(series: dict[str, list[tuple[int, float]]], max_paso: int, hitos=None) -> str:
    """Líneas en SVG. Sin librerías: tiene que abrir en cualquier navegador."""
    if not series or max_paso < 1:
        return ""
    hitos = hitos or []

    an, al = 720, 260
    izq, der, arr, aba = 46, 16, 16, 34
    ancho, alto = an - izq - der, al - arr - aba

    todos = [v for s in series.values() for _, v in s]
    lo_dato, hi_dato = min(todos), max(todos)

    # Escala fija de 0 a 100 cuando los indicadores son porcentajes. Ajustar el
    # eje a los datos —como se hacía antes— exagera visualmente los cambios y
    # llena el eje de números sin sentido: para valores de 30 a 100 el eje
    # arrancaba en 23 y terminaba en 107.
    if 0 <= lo_dato and hi_dato <= 100:
        lo, hi = 0.0, 100.0
        marcas = [0, 25, 50, 75, 100]
    else:
        if hi_dato == lo_dato:
            hi_dato, lo_dato = hi_dato + 1, lo_dato - 1
        margen = (hi_dato - lo_dato) * 0.1
        lo, hi = lo_dato - margen, hi_dato + margen
        marcas = [lo + (hi - lo) * i / 4 for i in range(5)]

    def x(paso):
        return izq + (ancho * (paso - 1) / max(1, max_paso - 1))

    def y(valor):
        return arr + alto - (alto * (valor - lo) / (hi - lo))

    p = [f'<svg viewBox="0 0 {an} {al}" role="img" aria-label="Indicadores a lo largo de la deliberación">']

    for v in marcas:  # rejilla horizontal
        yy = y(v)
        p.append(f'<line x1="{izq}" y1="{yy:.1f}" x2="{an-der}" y2="{yy:.1f}" class="rejilla"/>')
        p.append(f'<text x="{izq-8}" y="{yy+4:.1f}" class="eje-y">{v:.0f}</text>')

    # Hitos críticos: permiten ver si los indicadores se movieron ahí
    for paso_hito in hitos:
        if 1 <= paso_hito <= max_paso:
            xx = x(paso_hito)
            p.append(f'<line x1="{xx:.1f}" y1="{arr}" x2="{xx:.1f}" y2="{arr+alto}" class="hito-linea"/>')
            p.append(f'<text x="{xx:.1f}" y="{arr-4}" class="hito-marca">decisión</text>')

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
        leyenda.append(
            f'<li><span class="punto" style="background:{color}"></span>'
            f'<span class="leyenda-nombre">{html.escape(bonito(nombre))}</span>'
            f'<span class="leyenda-dato">{html.escape(recorrido(serie))}</span></li>')
    leyenda.append("</ul>")
    return "".join(p) + "".join(leyenda)


def recorrido(serie: list[tuple[int, float]]) -> str:
    """
    Describe la forma de la curva, no solo sus extremos. Un indicador que trepa
    al máximo y después se derrumba cuenta algo muy distinto de uno que sube
    despacio, y con «inicio → final» los dos se leen igual.
    """
    ini, fin = serie[0][1], serie[-1][1]
    paso_alto, alto = max(serie, key=lambda x: x[1])
    paso_bajo, bajo = min(serie, key=lambda x: x[1])

    if alto > max(ini, fin):
        return f"{ini:.0f} → pico {alto:.0f} en el turno {paso_alto} → {fin:.0f}"
    if bajo < min(ini, fin):
        return f"{ini:.0f} → cayó a {bajo:.0f} en el turno {paso_bajo} → {fin:.0f}"
    if fin > ini:
        return f"{ini:.0f} → {fin:.0f} · subió"
    if fin < ini:
        return f"{ini:.0f} → {fin:.0f} · bajó"
    return f"{ini:.0f} · sin cambios"


def bonito(clave: str) -> str:
    """
    Nombre legible a partir del identificador. Los acentos no se pueden
    recuperar: si el escenario definió «diseno_final», acá sale sin la eñe.
    El constructor ya los conserva, así que esto solo afecta a los viejos.
    """
    return clave.replace("_", " ").strip().capitalize()


def valor_bonito(v: str) -> str:
    """Un valor de estado como «fondo_mixto_con_reserva» se lee mal tal cual."""
    t = str(v).replace("_", " ").strip()
    return t[:1].upper() + t[1:] if t else t


# ---------------------------------------------------------------- escenario

NOMBRES_MESA = {
    "dialogic__GameMaster": "conversación",
    "generic__GameMaster": "general",
    "game_theoretic_and_dramaturgic__GameMaster": "juego con pagos",
    "interviewer__GameMaster": "entrevista guiada",
    "marketplace__GameMaster": "mercado",
}
NOMBRES_ORDEN = {"fixed": "fijo", "random": "al azar", "game_master_choice": "lo elige el narrador"}
NOMBRES_MOTOR = {"sequential": "por turnos", "simultaneous": "simultáneo",
                 "asynchronous": "asincrónico", "interview": "entrevista", "survey": "encuesta"}
NOMBRES_TIPO = {"percentage": "porcentaje", "number": "número", "numeric": "número",
                "categorical": "categorías", "choice": "categorías", "boolean": "sí / no"}


def leer_escenario(ruta: Path | None) -> dict:
    """Lo que se configuró antes de correr. Sin esto no se puede interpretar nada."""
    if not ruta or not ruta.is_file():
        return {}
    try:
        d = json.loads(ruta.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    cfg = d.get("config", d)
    gm = cfg.get("game_master") or {}
    return {
        "premisa": cfg.get("premise", ""),
        "datos": cfg.get("shared_memories") or [],
        "agentes": cfg.get("agents") or [],
        "decisiones": gm.get("critical_decision_points") or [],
        "variables": gm.get("grounded_variables") or [],
        "mesa_nombre": gm.get("name", ""),
        "mesa_prefab": gm.get("prefab", ""),
        "orden": gm.get("acting_order", ""),
        "cierre": gm.get("allow_early_termination"),
        "parametros": gm.get("parameters") or {},
        "motor": cfg.get("engine_type", ""),
        "pasos": cfg.get("max_steps"),
        "modelo": (d.get("llm_settings") or {}).get("model_name", ""),
        "modelo_gm": (d.get("gm_llm_settings") or {}).get("model_name", ""),
        "temp": (d.get("llm_settings") or {}).get("temperature"),
        "temp_gm": (d.get("gm_llm_settings") or {}).get("temperature"),
    }


def senales(pasos, series, franjas, esc, resumen) -> list[tuple[str, str]]:
    """
    Observaciones calculadas sobre el resultado. La idea es enseñar qué mirar:
    un consenso clavado en el techo o un indicador que nunca se movió dicen
    más sobre el diseño del escenario que sobre la deliberación.
    """
    obs: list[tuple[str, str]] = []

    for nombre, serie in sorted(series.items()):
        ini, fin = serie[0][1], serie[-1][1]
        paso_alto, alto = max(serie, key=lambda x: x[1])
        es_consenso = "consenso" in nombre.lower()

        if es_consenso and fin >= 100:
            obs.append(("alerta",
                        f"El consenso terminó en {fin:.0f}, el máximo posible. En una mesa "
                        "con intereses en conflicto eso suele indicar que los actores "
                        "no sostuvieron sus posiciones, más que un acuerdo trabajado."))
        elif es_consenso and alto >= 100 and fin < alto:
            # Que trepe al techo y se derrumbe es la firma de una deliberación
            # de verdad: hubo acuerdo aparente y algo lo rompió.
            obs.append(("buena",
                        f"El consenso llegó a {alto:.0f} en el turno {paso_alto} y después "
                        f"cayó hasta {fin:.0f}. Un acuerdo que se arma y se rompe indica que "
                        "apareció algo que los actores no estaban dispuestos a aceptar; "
                        f"conviene mirar qué pasó a partir del turno {paso_alto}."))
        elif fin == ini:
            # La guía del proyecto trata este caso como problema conocido y da
            # tres causas concretas; la tercera —que el nombre no coincida— es
            # invisible salvo que a uno se lo digan.
            obs.append(("neutra",
                        f"«{bonito(nombre)}» no se movió en toda la deliberación: quedó en "
                        f"{fin:.0f}. La guía del proyecto señala tres causas para esto: que "
                        "los puntos de decisión no digan explícitamente qué valores cambian, "
                        "que la regla de cambio no sea lo bastante accionable, o que el "
                        "nombre del indicador no coincida exactamente con el que usan los "
                        "puntos de decisión."))

    permitidos = {v.get("name"): (v.get("allowed_values") or [])
                  for v in esc.get("variables", []) if v.get("allowed_values")}
    for nombre, serie in franjas.items():
        valores = {v for _, v in serie}
        if nombre in permitidos:
            fuera = [v for v in valores if v not in permitidos[nombre]]
            if fuera:
                obs.append(("alerta",
                            f"«{bonito(nombre)}» tomó el valor «{fuera[0]}», que no está entre "
                            "los que definiste. El narrador no verifica esa lista, así que conviene "
                            "leer el valor como texto libre."))
        # Que no cambie solo es un problema si se quedó en su valor de partida:
        # sostener un valor ya decidido es lo esperable, no una señal de nada.
        inicial = next((str(v.get("default_value", "")) for v in esc.get("variables", [])
                        if v.get("name") == nombre), None)
        unico = next(iter(valores)) if len(valores) == 1 else None
        if unico is not None and inicial is not None and unico == inicial:
            obs.append(("alerta",
                        f"«{bonito(nombre)}» se quedó en «{unico}», su valor de partida, "
                        "durante toda la deliberación. Si ahí se registraba qué se decidió, "
                        "la decisión no llegó a tomarse."))
        elif unico is not None and inicial is not None:
            obs.append(("neutra",
                        f"«{bonito(nombre)}» quedó en «{unico}» desde el turno "
                        f"{serie[0][0]} y no volvió a moverse."))

    consignas = [p["mesa"]["consigna"] for p in pasos if p.get("mesa", {}).get("consigna")]
    distintas = len(set(consignas))
    if consignas and distintas == 1:
        obs.append(("alerta",
                    f"A los {len(consignas)} turnos se les dio la palabra con la misma pregunta. "
                    "Cuando la consigna no cambia ni menciona lo que se viene discutiendo, "
                    "invita a seguir la conversación más que a fijar postura."))
    elif distintas > 1:
        obs.append(("buena",
                    f"Hubo {distintas} consignas distintas en {len(consignas)} turnos: a cada "
                    "actor se le pidió postura sobre algo concreto de lo que se venía "
                    "discutiendo."))

    if resumen and resumen.get("completa") is False:
        falta = (resumen.get("pasos_pedidos") or 0) - (resumen.get("pasos_completados") or 0)
        obs.append(("alerta",
                    f"La corrida quedó incompleta: faltaron {falta} turnos. Lo que sigue es "
                    "todo lo que alcanzó a pasar, así que puede no haber cierre."))

    quienes = {}
    for p in pasos:
        quienes[p["quien"]] = quienes.get(p["quien"], 0) + 1
    if len(quienes) > 1:
        veces = sorted(quienes.values())
        if veces[-1] >= veces[0] * 2:
            mas = max(quienes, key=quienes.get)
            obs.append(("neutra",
                        f"La palabra quedó repartida de forma despareja: {mas} habló "
                        f"{veces[-1]} veces y alguien lo hizo solo {veces[0]}."))
    return obs


def seccion_ficha(pasos, esc, resumen) -> str:
    """
    Identificación de la corrida, arriba de todo y en una sola tabla.

    Los datos ya estaban, repartidos entre el encabezado y la ficha técnica del
    final. Quien abre el informe necesita ubicarse antes de leer nada.
    """
    hechos = resumen.get("pasos_completados") or len(pasos)
    pedidos = resumen.get("pasos_pedidos") or esc.get("pasos")
    completa = resumen.get("completa")

    filas = [("Estado", "Completa" if completa else "Incompleta")]
    if pedidos:
        filas.append(("Progreso", f"{hechos} de {pedidos} turnos"))
    else:
        filas.append(("Turnos", str(hechos)))
    if esc.get("mesa_nombre"):
        filas.append(("Ámbito simulado", esc["mesa_nombre"]))
    filas.append(("Actores", str(len(esc.get("agentes") or []) or len({p["quien"] for p in pasos}))))
    filas.append(("Dinámica", NOMBRES_MOTOR.get(esc.get("motor", ""), esc.get("motor") or "—")))
    if resumen.get("modelo"):
        filas.append(("Modelo de los actores", str(resumen["modelo"]).split("/")[-1]))
    if resumen.get("modelo_gm"):
        filas.append(("Modelo del entorno", str(resumen["modelo_gm"]).split("/")[-1]))
    if resumen.get("duracion_min"):
        filas.append(("Duración", f"{resumen['duracion_min']} minutos"))
    if resumen.get("terminada"):
        filas.append(("Fecha", str(resumen["terminada"])[:10]))

    p = ['<section id="ficha"><h2>Ficha de la corrida</h2>',
         f'<p class="ayuda-sec">{marca("medido")} Sale del registro de la corrida.</p>',
         '<table class="config ficha-corrida"><tbody>']
    for k, v in filas:
        p.append(f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>")
    p.append("</tbody></table></section>")
    return "".join(p)


def seccion_diseno(pasos, series, esc) -> str:
    """
    Qué se simuló y qué no pretende ser, antes de mostrar ningún resultado.

    El informe describía lo que pasó sin decir nunca qué clase de cosa es. Para
    alguien con formación metodológica esa es la primera pregunta, y sin
    responderla los números de más abajo quedan sin encuadre.
    """
    p = ['<section id="diseno"><h2>Diseño de la simulación</h2>']
    p.append('<p class="encuadre">Esta simulación representa una deliberación entre actores '
             "con intereses, restricciones y criterios distintos. No busca predecir una "
             "decisión real, sino explorar cómo podrían evolucionar tensiones, acuerdos y "
             "bloqueos bajo un conjunto explícito de supuestos.</p>")

    filas = []
    # Dos campos que el esquema no tiene todavía. Se muestran igual, y en falta:
    # ocultarlos daría por completo un encuadre al que le faltan las dos
    # definiciones que más lo ordenan.
    filas.append(("Unidad de análisis",
                  esc.get("mesa_nombre") or "<em>no declarada en el escenario</em>", True))
    nombres = [a.get("name", "") for a in (esc.get("agentes") or [])]
    if nombres:
        filas.append(("Actores", ", ".join(nombres), False))
    filas.append(("Entorno (narrador)",
                  NOMBRES_MESA.get(esc.get("mesa_prefab", ""), esc.get("mesa_prefab") or "—")
                  + " — da la palabra, registra lo ocurrido y asigna las variables",
                  False))
    filas.append(("Dinámica de interacción",
                  NOMBRES_MOTOR.get(esc.get("motor", ""), esc.get("motor") or "—")
                  + " · " + NOMBRES_ORDEN.get(esc.get("orden", ""), esc.get("orden") or "—"),
                  False))
    filas.append(("Producto esperado", "<em>no declarado en el escenario</em>", True))
    if series or esc.get("variables"):
        obs = [bonito(v.get("name", "")) for v in (esc.get("variables") or [])] or \
              [bonito(n) for n in sorted(series)]
        filas.append(("Variables observadas", ", ".join(obs), False))
    if esc.get("decisiones"):
        filas.append(("Hitos críticos",
                      f"{len(esc['decisiones'])}, en los turnos "
                      + ", ".join(str(d.get("step")) for d in esc["decisiones"]), False))

    p.append('<table class="config"><tbody>')
    for k, v, crudo in filas:
        celda = v if crudo else html.escape(str(v))
        p.append(f"<tr><th>{html.escape(k)}</th><td>{celda}</td></tr>")
    p.append("</tbody></table>")

    if esc.get("premisa"):
        p.append('<details class="escenario"><summary>La situación planteada, textual</summary>'
                 '<div class="cuerpo-esc">' + parrafos(esc["premisa"]) + "</div></details>")
    p.append("</section>")
    return "".join(p)


# Qué implica cada tipo de narrador, medido sobre corridas propias. Se dice el
# efecto observado y no una descripción del prefab, porque es lo que cambia
# cómo se lee el resultado.
EFECTO_NARRADOR = {
    "generic__GameMaster":
        "Redacta una consigna distinta en cada turno y pregunta por posiciones concretas. "
        "En nuestra comparación, con el mismo escenario, produjo desacuerdo sostenido: el "
        "consenso subió al máximo en el turno siete y después cayó a 60.",
    "dialogic__GameMaster":
        "Repite la misma consigna genérica —«¿qué diría probablemente esta persona?»— en "
        "todos los turnos. En nuestra comparación, con el mismo escenario, el consenso "
        "llegó al máximo y no volvió a bajar: nadie sostuvo una objeción.",
    "game_theoretic_and_dramaturgic__GameMaster":
        "Admite escenas con opciones cerradas y pagos, así que las respuestas quedan "
        "codificables en vez de texto libre.",
    "interviewer__GameMaster": "Conduce como entrevista: pregunta y repregunta a uno por vez.",
    "marketplace__GameMaster": "Resuelve ofertas y demandas entre los actores.",
}


def tabla_actores(esc, veces=None) -> str:
    """
    Los actores en una fila cada uno, para poder compararlos de un vistazo.

    Viene de una propuesta del equipo que pedía cuatro columnas nuevas —rol,
    interés, restricción y orientación—. Tres se pueden llenar con lo que el
    escenario ya guarda: la orientación son los valores configurados, el rol lo
    enuncia el primer recuerdo por convención, y el interés es el objetivo. La
    cuarta, la restricción, no se puede separar del objetivo de forma confiable
    —partirlo por palabra clave devuelve fragmentos sin sentido—, así que se
    deja afuera en vez de completarla mal.

    Cada columna dice de dónde sale: una tabla que parece la ficha de diseño y
    en realidad mezcla campos con inferencias es peor que no tenerla.
    """
    agentes = esc.get("agentes") or []
    if len(agentes) < 2:
        return ""
    veces = veces or {}

    filas = []
    for a in agentes:
        nombre = a.get("name", "")
        # El rol suele ser la primera frase del primer recuerdo. Es una
        # convención de cómo se escriben los escenarios, no un campo, así que
        # se toma solo si el recuerdo habla del actor.
        rol = ""
        mems = a.get("memories") or []
        if mems:
            primera = re.split(r"(?<=[.;])\s", mems[0].strip())[0]
            corto = nombre.split(" — ")[0]
            if corto and corto.split()[0] in primera:
                # Se saca el nombre, el verbo y la preposición que a veces lo
                # sigue: sin esto «representa a las autoridades» quedaba como
                # «a las autoridades», empezando por la preposición.
                rol = re.sub(
                    rf"^{re.escape(corto)}\s+"
                    r"(?:es|era|integra|representa|forma parte de|pertenece a|dirige|"
                    r"coordina|preside)\s+(?:a\s+)?",
                    "", primera).strip(" .")
                rol = rol[:1].upper() + rol[1:] if rol else ""
        meta = (a.get("goal") or "").strip()
        if meta:
            meta = re.split(r"(?<=[.;])\s", meta)[0]
        val = ((a.get("components") or {}).get("values") or {}).get("core_values")
        orient = ", ".join(val) if isinstance(val, list) else (val or "")
        filas.append((nombre, rol, meta, orient, veces.get(nombre)))

    if not any(r or o for _, r, _, o, _ in filas):
        return ""

    p = ['<table class="config tabla-actores"><thead><tr><th>Actor</th><th>Rol</th>'
         "<th>Qué busca</th><th>Orientación</th>"
         + ("<th>Turnos</th>" if veces else "") + "</tr></thead><tbody>"]
    for nombre, rol, meta, orient, n in filas:
        p.append(f"<tr><th>{html.escape(nombre)}</th>"
                 f"<td>{html.escape(rol) or '—'}</td>"
                 f"<td>{html.escape(meta) or '—'}</td>"
                 f"<td>{html.escape(orient) or '—'}</td>"
                 + (f"<td>{n if n is not None else '—'}</td>" if veces else "")
                 + "</tr>")
    p.append("</tbody></table>")
    p.append('<p class="nota-fuente">De dónde sale cada columna: <strong>rol</strong>, de la '
             "primera frase del primer recuerdo —es una convención de redacción, no un campo—; "
             "<strong>qué busca</strong>, de la primera oración del objetivo; "
             "<strong>orientación</strong>, de los valores configurados. La restricción de cada "
             "uno —qué no está dispuesto a aceptar— no figura como campo aparte: está mezclada "
             "dentro del objetivo, y separarla automáticamente no da un resultado "
             "confiable.</p>")
    return "".join(p)


def seccion_narrador(pasos, esc, resumen, series=None, franjas=None) -> str:
    """
    Quién condujo la mesa: lo que se configuró y lo que hizo.

    Es el componente más influyente de todos —cambiarlo, con el mismo escenario,
    dio 100 fijo contra 100 y caída a 60— y tenía una palabra en una fila de
    tabla. Además es quien asigna los valores de todas las variables y quien
    decide qué queda registrado como ocurrido, así que su margen de intervención
    condiciona la lectura de todo lo demás.
    """
    series, franjas = series or {}, franjas or {}
    prefab = esc.get("mesa_prefab") or ""
    p = ['<section id="narrador"><h2>El narrador</h2>']
    p.append('<p class="ayuda-sec">Concordia lo define como una entidad especial que simula '
             "el entorno. No es un actor más: le da la palabra, decide qué queda "
             "registrado como ocurrido, reparte lo que se entera cada uno y asigna el valor "
             "de todas las variables.</p>")

    filas = [("Tipo", NOMBRES_MESA.get(prefab, prefab or "—"))]
    if esc.get("mesa_nombre"):
        filas.insert(0, ("Nombre", esc["mesa_nombre"]))
    filas.append(("Orden de la palabra",
                  NOMBRES_ORDEN.get(esc.get("orden", ""), esc.get("orden") or "—")))
    if resumen.get("modelo_gm"):
        filas.append(("Modelo", str(resumen["modelo_gm"]).split("/")[-1]))
    if esc.get("temp_gm") is not None:
        filas.append(("Temperatura", str(esc["temp_gm"])))
    filas.append(("Puede cerrar antes", "sí" if esc.get("cierre") else "no"))
    p.append('<table class="config"><tbody>')
    for k, v in filas:
        p.append(f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>")
    p.append("</tbody></table>")

    efecto = EFECTO_NARRADOR.get(prefab)
    if efecto:
        p.append(f'<p class="encuadre">{html.escape(efecto)}</p>')

    # Cuánto intervino, medido: si copia textual lo que dijo el actor, no
    # está mediando nada, y los hechos inventados entran sin filtro.
    con_ambos = [x for x in pasos if x.get("dicho") and x.get("evento")]
    if con_ambos:
        textuales = sum(1 for x in con_ambos if parecido(x["dicho"], x["evento"]) > 0.95)
        p.append("<h3>Cuánto intervino, medido</h3>")
        if textuales == len(con_ambos):
            p.append(f'<p class="nota-datos">{marca("medido")} En los {len(con_ambos)} turnos '
                     "registró <strong>textualmente</strong> lo que dijo el actor, sin "
                     "cambiarle nada. Es decir: lo que alguien afirma pasa a ser un hecho del "
                     "mundo sin que nada lo verifique, y los demás razonan sobre eso.</p>")
        elif textuales:
            p.append(f'<p class="nota-datos">{marca("medido")} En {textuales} de '
                     f"{len(con_ambos)} turnos registró textualmente lo dicho; en los "
                     f"{len(con_ambos) - textuales} restantes reformuló o agregó.</p>")
        else:
            p.append(f'<p class="nota-datos">{marca("medido")} Reformuló lo dicho en los '
                     f"{len(con_ambos)} turnos: no hay copia textual.</p>")

    consignas = [x["mesa"]["consigna"] for x in pasos if x.get("mesa", {}).get("consigna")]

    if consignas:
        distintas = len(set(consignas))
        if distintas == 1:
            p.append(f'<p class="nota-datos aviso-txt">{marca("medido")} Usó '
                     f"<strong>una sola consigna</strong> para los {len(consignas)} turnos: "
                     "todos respondieron al mismo estímulo, sin que se les preguntara por lo "
                     "que estaba en discusión en ese momento.</p>")
        else:
            p.append(f'<p class="nota-datos">{marca("medido")} Redactó '
                     f"<strong>{distintas} consignas distintas</strong> en {len(consignas)} "
                     "turnos. Están todas en la ficha técnica.</p>")


    # Todo lo que produce el narrador estaba repartido dentro de los turnos
    # plegados, uno por uno, y nada decía cuánto era ni si variaba. Contado acá,
    # se ve de qué tamaño es su intervención y desde dónde mirarla.
    p.append("<h3>Qué produjo en cada turno</h3>")
    p.append('<ul class="produjo">')

    con_sig = sum(1 for x in pasos if (x.get("mesa") or {}).get("siguiente"))
    aciertos = sum(1 for x in pasos
                   if (x.get("mesa") or {}).get("siguiente") == x["quien"])
    if con_sig:
        p.append(f"<li><strong>Anotó a quién le tocaba</strong> en {con_sig} de {len(pasos)} "
                 f"turnos, y habló ese mismo en {aciertos}. "
                 "<span class='donde'>Se ve en «Cómo se repartió la palabra».</span></li>")

    if consignas:
        p.append(f"<li><strong>Escribió la consigna</strong> con que dio la palabra: "
                 f"{len(set(consignas))} distintas en {len(consignas)} turnos. "
                 "<span class='donde'>Cada una, en su turno; todas juntas, en la ficha "
                 "técnica.</span></li>")

    obs_por_paso = [(x.get("mesa") or {}).get("observaciones") or {} for x in pasos]
    total_obs = sum(len(o) for o in obs_por_paso)
    if total_obs:
        distintos = sum(1 for o in obs_por_paso if len(set(o.values())) > 1)
        detalle = ("cada actor recibió una versión distinta en "
                   f"{distintos} de {len(pasos)} turnos"
                   if distintos else
                   "todos recibieron el mismo texto, así que no hubo información privada")
        p.append(f"<li><strong>Repartió las observaciones</strong>: {total_obs} en total, y "
                 f"{detalle}. <span class='donde'>Dentro de cada turno, en «Qué se enteró "
                 "cada uno».</span></li>")

    valores = sum(len(s) for s in series.values()) + sum(len(s) for s in franjas.values())
    if valores:
        cuantas = len(series) + len(franjas)
        p.append(f"<li><strong>Asignó los valores</strong> de las variables: {valores} "
                 f"lecturas sobre {cuantas}. <span class='donde'>En «Cómo evolucionó» y en "
                 "«Variables de seguimiento».</span></li>")

    cierres = [(x.get("mesa") or {}).get("termina") for x in pasos
               if (x.get("mesa") or {}).get("termina")]
    if cierres:
        quiso = sum(1 for c in cierres if str(c).strip().lower().startswith(("s", "y")))
        p.append(f"<li><strong>Contestó si la deliberación había terminado</strong>, "
                 f"{len(cierres)} veces. "
                 + (f"Dijo que sí en {quiso}." if quiso else "Siempre dijo que no.") + "</li>")

    # El campo Summary del registro es el mismo evento con el número de paso
    # adelante, no un resumen aparte. Conviene decirlo: buscarlo es razonable.
    p.append("<li><strong>No escribe un resumen propio.</strong> El registro trae un campo "
             "«Summary» por paso, pero es el hecho registrado repetido, no una síntesis "
             "aparte. El resumen en castellano de este informe lo produce otro modelo "
             "después de terminada la corrida.</li>")
    p.append("</ul>")

    instr = ((esc.get("parametros") or {}).get("moderation_instructions") or "").strip()
    if instr:
        p.append('<details class="escenario"><summary>Instrucciones que se le dieron'
                 '</summary><div class="cuerpo-esc">' + parrafos(instr) + "</div></details>")
    p.append("</section>")
    return "".join(p)


# Huellas del texto con que el motor inyecta el perfil. Son las frases que
# arman las fábricas de componentes: si el perfil entró como recuerdo y fue
# recuperado en un turno, alguna de estas aparece en lo recuperado.
HUELLAS_PERFIL = (
    "tends to seek information confirming existing beliefs",
    "gives disproportionate weight to easily recalled examples",
    "relies heavily on initial information when making decisions",
    "continues activities due to past investment",
    "overestimates the accuracy of their judgments",
    "Personality traits:",
    "(strength:",
)


PAL_ING = re.compile(r"\b(the|and|of|for|with|that|this|from|would|stated|declared|"
                     r"assembly|meeting|before|during|which|should|about)\b", re.I)
PAL_ESP = re.compile(r"\b(el|la|los|las|de|que|para|con|debe|sobre|como|declaró|mesa|"
                     r"una|del|por|más|entre)\b", re.I)


def idioma_de(texto: str) -> str:
    """Grueso pero suficiente: solo hay que distinguir castellano de inglés."""
    if not texto or not texto.strip():
        return ""
    i, e = len(PAL_ING.findall(texto)), len(PAL_ESP.findall(texto))
    if not i and not e:
        return ""
    return "inglés" if i > e * 1.3 else "castellano" if e > i * 1.3 else "mezcla"


def seccion_controles(pasos, esc) -> str:
    """
    Verificaciones mecánicas sobre lo que hizo el motor.

    No dicen nada sobre la deliberación: dicen si la maquinaria se comportó como
    debía. Van juntas porque cada una, suelta, parece un detalle, y leídas
    seguidas responden si se puede confiar en lo que se leyó más arriba. Todas
    salen del registro, sin pedirle nada a ningún modelo.
    """
    if not pasos:
        return ""
    filas = []

    # Los prefabs de Concordia escriben sus consignas en inglés y el modelo a
    # veces las devuelve así. Importa porque el idioma de la pregunta arrastra
    # el de la respuesta, y el escenario está escrito en castellano.
    consignas = [(p["n"], (p.get("mesa") or {}).get("consigna")) for p in pasos]
    en_ingles = [n for n, c in consignas if c and idioma_de(c) == "inglés"]
    if any(c for _, c in consignas):
        total = sum(1 for _, c in consignas if c)
        if en_ingles:
            filas.append(("aviso", "Idioma de las consignas",
                          f"{len(en_ingles)} de {total} se le hicieron en inglés "
                          f"(turnos {', '.join(map(str, en_ingles[:8]))}). El escenario está "
                          "en castellano: el idioma de la pregunta arrastra al de la "
                          "respuesta, y eso introduce una diferencia entre turnos que no "
                          "configuró nadie."))
        else:
            filas.append(("ok", "Idioma de las consignas",
                          f"Las {total} se hicieron en castellano."))

    mezclados = [p["n"] for p in pasos
                 if p.get("evento") and idioma_de(p["evento"]) in ("inglés", "mezcla")]
    if mezclados:
        filas.append(("aviso", "Idioma de lo registrado",
                      f"En {len(mezclados)} de {len(pasos)} turnos el hecho quedó registrado "
                      f"en inglés o mezclado (turnos {', '.join(map(str, mezclados[:8]))})."))

    # Que las tres preguntas se repitan idénticas turno a turno es un modo de
    # falla conocido: el actor deja de actualizar y responde en piloto.
    pares = repetidos = 0
    for q in {p["quien"] for p in pasos}:
        for campo in ("persona", "situacion", "haria"):
            suyos = [p[campo] for p in pasos if p["quien"] == q and p.get(campo)]
            for a, b in zip(suyos, suyos[1:]):
                pares += 1
                repetidos += parecido(a, b) > 0.9
    if pares:
        if repetidos:
            filas.append(("aviso", "Los actores se actualizan",
                          f"{repetidos} de {pares} respuestas consecutivas a «quién soy», "
                          "«dónde estoy» y «qué haría» quedaron casi idénticas: en esos "
                          "turnos el actor no incorporó lo que había pasado."))
        else:
            filas.append(("ok", "Los actores se actualizan",
                          f"Las {pares} respuestas consecutivas a «quién soy», «dónde estoy» "
                          "y «qué haría» cambiaron entre turno y turno: nadie quedó "
                          "contestando en piloto automático."))

    # Si cada uno recibe el hecho tal cual, no hay información privada y el
    # escenario pierde las asimetrías sobre las que se apoya.
    tot = dif_publico = 0
    dif_entre = 0
    for p in pasos:
        obs = (p.get("mesa") or {}).get("observaciones") or {}
        if len(set(obs.values())) > 1:
            dif_entre += 1
        for o in obs.values():
            tot += 1
            if parecido(o, p.get("evento") or "") < 0.9:
                dif_publico += 1
    if tot:
        if dif_publico == tot and dif_entre == len(pasos):
            filas.append(("ok", "Cada uno se entera de algo distinto",
                          f"Las {tot} observaciones difieren del hecho público, y en los "
                          f"{len(pasos)} turnos difieren también entre actores. Hay "
                          "información privada de verdad, no el mismo texto repartido."))
        else:
            filas.append(("aviso", "Cada uno se entera de algo distinto",
                          f"{dif_publico} de {tot} observaciones difieren del hecho público; "
                          f"entre actores difieren en {dif_entre} de {len(pasos)} "
                          "turnos. Donde coinciden no hay información privada."))

    # Recuperar siempre los mismos recuerdos significaría razonar sobre una
    # rebanada fija de la memoria, sin importar lo que pase.
    fijos = []
    for q in {p["quien"] for p in pasos}:
        sets = [set((p.get("memoria") or {}).get("recuerdos") or [])
                for p in pasos if p["quien"] == q]
        sets = [s for s in sets if s]
        if len(sets) > 1 and len(set.union(*sets)) == len(set.intersection(*sets)):
            fijos.append(q)
    if any((p.get("memoria") or {}).get("recuerdos") for p in pasos):
        if fijos:
            filas.append(("aviso", "La memoria se consulta de nuevo cada turno",
                          "Recuperaron siempre los mismos recuerdos: "
                          + html.escape(", ".join(fijos))
                          + ". Están razonando sobre una porción fija de su memoria."))
        else:
            filas.append(("ok", "La memoria se consulta de nuevo cada turno",
                          "Todos trajeron recuerdos distintos según el momento, no una "
                          "porción fija."))

    if not filas:
        return ""
    p = ['<section id="controles"><h2>Controles sobre el motor</h2>']
    p.append(f'<p class="ayuda-sec">{marca("medido")} No dicen nada sobre la deliberación: '
             "dicen si la maquinaria hizo lo que tenía que hacer. Salen del registro, sin "
             "intervención de ningún modelo.</p>")
    p.append('<ul class="controles">')
    for estado, titulo, detalle in filas:
        p.append(f'<li class="c-{estado}"><span class="c-marca" aria-hidden="true">'
                 + ("✓" if estado == "ok" else "!") + "</span>"
                 f'<div><p class="c-titulo">{html.escape(titulo)}</p>'
                 f'<p class="c-detalle">{detalle}</p></div></li>')
    p.append("</ul></section>")
    return "".join(p)


def llegada_del_perfil(pasos, esc) -> str:
    """
    Si el perfil configurado llegó o no a la acción del actor.

    Es la pregunta que quedaba abierta detrás de todo lo psicológico. Fuera del
    tipo «Mínimo» el perfil no es un componente presente en cada acción: entra
    al banco de memoria y tiene que ser recuperado para pesar en algo. Que esté
    cargado no garantiza nada, y hasta ahora eso se decía como advertencia sin
    medirlo nunca.

    Se cuenta en cuántos turnos apareció efectivamente entre los recuerdos que
    el actor trajo antes de hablar.
    """
    con_perfil = [a for a in (esc.get("agentes") or [])
                  if perfil_psicologico(a.get("components") or {})]
    if not con_perfil or not pasos:
        return ""

    minimos = [a for a in con_perfil if a.get("prefab") == "minimal__Entity"]
    indirectos = [a for a in con_perfil if a.get("prefab") != "minimal__Entity"]

    p = ['<h3>¿Llegó el perfil a la acción?</h3>']
    if minimos and not indirectos:
        p.append('<p class="nota-datos">Todos son del tipo «Mínimo», así que el perfil entra '
                 "como componente presente en cada acción: no depende de que se lo recupere. "
                 "No hay nada que verificar acá.</p>")
        return "".join(p)

    nombres = {a.get("name") for a in indirectos}
    turnos = {n: [0, 0] for n in nombres}          # [con perfil, totales]
    for x in pasos:
        if x["quien"] not in turnos:
            continue
        rec = " ".join((x.get("memoria") or {}).get("recuerdos") or [])
        turnos[x["quien"]][1] += 1
        if any(hh in rec for hh in HUELLAS_PERFIL):
            turnos[x["quien"]][0] += 1

    con = sum(v[0] for v in turnos.values())
    tot = sum(v[1] for v in turnos.values())
    if not tot:
        return ""

    p.append(f'<p class="nota-datos">{marca("medido")} Estos {len(indirectos)} actores '
             "no son del tipo «Mínimo», así que su perfil entró al banco de memoria y tenía "
             "que ser recuperado para influir en algo. Se buscó su texto entre los recuerdos "
             "que cada uno trajo antes de hablar.</p>")

    if con == 0:
        p.append(f'<p class="nota-datos aviso-txt"><strong>No se recuperó en ninguno de los '
                 f"{tot} turnos.</strong> El perfil quedó cargado y nunca entró en el contexto "
                 "con el que se decidió la acción. Lo que se observe en la conducta se explica "
                 "por el objetivo y los recuerdos, no por el perfil: para esta corrida, los "
                 "sesgos configurados no operaron.</p>")
        p.append('<p class="nota-datos">Para que pesen hay dos caminos: usar el tipo «Mínimo», '
                 "que los recibe como componente fijo, o escribir el rasgo dentro de los "
                 "recuerdos del actor, redactado en los términos del caso para que la "
                 "búsqueda lo encuentre.</p>")
    elif con < tot:
        p.append(f'<p class="nota-datos">{marca("medido")} Se recuperó en <strong>{con} de '
                 f"{tot}</strong> turnos. En los otros {tot - con} el actor decidió sin "
                 "tenerlo a la vista, así que una conducta sin rastro del sesgo en esos turnos "
                 "no dice nada sobre el sesgo.</p>")
    else:
        p.append(f'<p class="nota-datos">{marca("medido")} Se recuperó en los {tot} turnos: '
                 "estuvo disponible cada vez que le tocó actuar.</p>")

    if con < tot:
        p.append('<ul class="lista-datos">')
        for n, (c, t) in sorted(turnos.items()):
            p.append(f"<li>{html.escape(n)}: {c} de {t} turnos</li>")
        p.append("</ul>")
    return "".join(p)


def seccion_variables(series, franjas, esc) -> str:
    """
    Qué observa cada variable, en qué escala y con qué regla se movía.

    El informe mostraba nombres y curvas. La descripción, la escala y sobre todo
    la regla de actualización estaban escritas en el escenario y no se mostraban
    en ninguna parte, así que «federalización efectiva: 40 → 65» no se podía
    juzgar: faltaba saber qué tenía que hacer subir ese número. Es además la
    información que el entorno tenía y el lector no.
    """
    variables = esc.get("variables") or []
    if not variables:
        return ""

    p = ['<section id="variables"><h2>Variables de seguimiento</h2>']
    p.append(f'<p class="ayuda-sec">{marca("estimado")} Ninguna se mide: las asigna el entorno '
             "interpretando la deliberación, turno a turno. Sirven para comparar momentos "
             "dentro de esta corrida, no como magnitud absoluta ni como dato empírico. "
             "La regla es lo que el entorno tenía que aplicar; contrastarla con la curva es "
             "lo que permite ver si la aplicó.</p>")
    p.append('<table class="config tabla-vars"><thead><tr><th>Variable</th><th>Qué observa</th>'
             "<th>Escala</th><th>Regla de cambio</th></tr></thead><tbody>")
    for v in variables:
        nombre = bonito(v.get("name", ""))
        tipo = v.get("variable_type", "")
        if v.get("allowed_values"):
            escala = " · ".join(str(x) for x in v["allowed_values"])
        elif tipo == "percentage":
            escala = "0 a 100"
        elif v.get("min_value") is not None or v.get("max_value") is not None:
            escala = f"{v.get('min_value', '?')} a {v.get('max_value', '?')}"
        else:
            escala = NOMBRES_TIPO.get(tipo, tipo or "—")
        if v.get("default_value") is not None:
            escala += f" · empieza en {v['default_value']}"
        p.append(f"<tr><th>{html.escape(nombre)}</th>"
                 f"<td>{html.escape(str(v.get('description') or '—'))}</td>"
                 f"<td>{html.escape(escala)}</td>"
                 f"<td>{html.escape(str(v.get('update_rule') or '—'))}</td></tr>")
    p.append("</tbody></table></section>")
    return "".join(p)


def seccion_hitos(pasos, esc, resumen) -> str:
    """
    Los hitos críticos como lista, antes de la transcripción.

    Estaban marcados dentro de la deliberación, donde uno se los cruza en el
    turno nueve. Enumerados antes, estructuran la lectura: se sabe de entrada
    dónde el escenario forzaba una definición.
    """
    decisiones = esc.get("decisiones") or []
    if not decisiones:
        return ""
    hechos = resumen.get("pasos_completados") or len(pasos)

    p = ['<section id="hitos"><h2>Hitos críticos</h2>']
    p.append(f'<p class="ayuda-sec">{marca("medido")} Hechos que el escenario inyecta en un '
             "turno determinado para forzar una definición. Funcionan como manipulación: "
             "cambia una cosa en un momento conocido y el resto queda igual.</p>")
    p.append('<ol class="hitos">')
    for d in decisiones:
        n = d.get("step")
        alcanzado = isinstance(n, int) and n <= hechos
        texto = str(d.get("event", ""))
        # El titulo viene en mayusculas antes de los dos puntos. Se toma la
        # tirada entera y no la primera parte: cortando en el primer separador,
        # los cinco hitos quedaban titulados «Punto de decision critico» y lo
        # que los distingue caia dentro del cuerpo.
        m = re.match(r"\s*([A-ZÁÉÍÓÚÑ0-9\s\-–]{6,}):\s*(.*)", texto, re.S)
        titulo, cuerpo = (m.group(1).strip(), m.group(2).strip()) if m else ("", texto)
        titulo = re.sub(r"^puntos?\s+de\s+decisi[oó]n(?:es)?\s+cr[ií]tico?s?\s*[-–:]?\s*",
                        "", titulo, flags=re.I).strip()
        p.append('<li class="' + ("alcanzado" if alcanzado else "no-alcanzado") + '">')
        p.append(f'<span class="turno-hito">turno {html.escape(str(n))}</span>')
        p.append("<div>")
        if titulo:
            p.append(f'<p class="titulo-hito">{html.escape(titulo.capitalize())}</p>')
        p.append(f'<p class="cuerpo-hito">{html.escape(cuerpo[:400])}</p>')
        if not alcanzado:
            p.append('<p class="cuerpo-hito aviso-txt">La corrida terminó antes de este turno: '
                     "no llegó a ocurrir.</p>")
        p.append("</div></li>")
    p.append("</ol></section>")
    return "".join(p)


def seccion_datos(archivos, resumen) -> str:
    """
    De dónde salió esto y cómo volver a los datos crudos.

    Los enlaces vivían en el comentario del issue: quien guardaba el HTML perdía
    el rastro. Solo se enlaza lo que existe, para no dejar enlaces muertos.
    """
    QUE_ES = {
        "reporte.csv": "Los turnos y los valores de cada variable, para planilla.",
        "raw_log.json": "El registro completo de la corrida, tal cual lo emitió el motor.",
        "sim_structured.json": "El mismo registro en el formato estructurado de Concordia.",
        "resumen.json": "Estado, duración, modelos y consumo, en pocos campos.",
        "measurements.json": "Los canales de medición que haya emitido el motor.",
    }
    p = ['<section id="datos"><h2>Datos y trazabilidad</h2>']
    if archivos:
        p.append('<p class="ayuda-sec">Archivos de esta corrida, en la misma carpeta que este '
                 "informe.</p><ul class='lista-datos archivos'>")
        for nombre in archivos:
            p.append(f'<li><a href="{html.escape(nombre)}">{html.escape(nombre)}</a> — '
                     f"{html.escape(QUE_ES.get(nombre, 'archivo de la corrida'))}</li>")
        p.append("</ul>")

    p.append("<h3>Reproducibilidad</h3>")
    p.append('<p class="nota-datos">Volver a correr este mismo escenario no da este mismo '
             "resultado: el modelo muestrea sus respuestas y el motor fija por su cuenta la "
             "temperatura de los actores. Lo que se puede reproducir es el <em>procedimiento</em> "
             "—la configuración está completa en la ficha técnica y en el JSON del escenario—, "
             "no la corrida. Por eso conviene correr varias veces y mirar el rango.</p>")
    if resumen.get("consumo"):
        total = sum(d.get("llamadas", 0) for d in resumen["consumo"].values())
        p.append(f'<p class="nota-datos">Costó <strong>{total} pedidos</strong> al modelo, '
                 "repartidos por modelo en la ficha técnica.</p>")
    p.append("</section>")
    return "".join(p)


def ayuda_interpretacion(pasos, series, franjas, esc, resumen) -> str:
    """
    Qué se puede concluir de esto y qué no.

    El informe venía diciendo qué pasó, sin decir con qué alcance leerlo. La
    omisión más seria era la primera: una corrida es una muestra, y la guía del
    proyecto pide de tres a cinco para conocer el rango de resultados. Sin esa
    advertencia es natural leer un número puntual como si fuera el resultado.

    Lo demás son diagnósticos que solo aparecen cuando el síntoma está presente,
    con la causa y el arreglo que documenta el proyecto. Una lista de problemas
    posibles que no ocurrieron es ruido.
    """
    esc = esc or {}
    resumen = resumen or {}
    p = ['<section id="interpretacion"><h2>Cómo leer estos resultados</h2>']
    p.append('<p class="ayuda-sec">Qué permite afirmar esta corrida y qué no.</p>')
    p.append('<div class="tarjetas">')

    # Esta va siempre: es la condición de lectura de todo lo demás.
    p.append('<article class="tarjeta"><h3>Una corrida es una muestra</h3>'
             "<p>El comportamiento varía entre corridas porque el modelo muestrea "
             "sus respuestas. La guía del proyecto recomienda correr el mismo "
             "escenario <strong>de tres a cinco veces</strong> para conocer el rango "
             "de resultados posibles; esta es una. Un valor puntual —«el consenso "
             "terminó en 60»— describe esta corrida, no el escenario. Lo que sí es "
             "informativo de una sola es la <em>forma</em>: si hubo ruptura, cuándo, "
             "y quién la produjo.</p></article>")

    # Los valores no se miden: los asigna un modelo leyendo la discusión.
    if series or franjas:
        p.append('<article class="tarjeta"><h3>Los indicadores son juicios</h3>'
                 "<p>No se miden: los asigna el narrador interpretando la "
                 "deliberación, y por eso van marcados como estimados. Sirven para "
                 "comparar momentos <em>dentro</em> de una corrida —subió después de "
                 "tal intervención— y no como magnitud absoluta. Que un indicador "
                 "valga 60 no significa que algo esté al 60&nbsp;% de nada.</p></article>")

    # La guía advierte sobre seguir demasiadas variables a la vez.
    variables = esc.get("variables") or []
    if len(variables) > 3:
        p.append('<article class="tarjeta"><h3>Son muchos indicadores</h3>'
                 f"<p>Este escenario definió <strong>{len(variables)}</strong>. La guía "
                 "del proyecto recomienda dos o tres: con más, al narrador le cuesta "
                 "seguirlos a todos con precisión y algunos terminan moviéndose "
                 "juntos o quedándose planos, no porque eso ocurriera sino porque no "
                 "los distinguió. Si al mirar el gráfico varias curvas van "
                 "paralelas, esa es la causa probable.</p></article>")

    # Los puntos de decisión entran en la premisa, pero la consigna del turno la
    # redacta el narrador: verificamos que un recorte inyectado en el paso 15 no
    # llegó a la consigna de ese turno y nadie lo respondió.
    decisiones = esc.get("decisiones") or []
    if decisiones and pasos:
        sin_eco = []
        for d in decisiones:
            n = d.get("step")
            paso = next((x for x in pasos if x.get("n") == n), None)
            if not paso:
                continue
            consigna = (paso.get("mesa", {}) or {}).get("consigna") or ""
            evento = str(d.get("event", ""))
            # Se buscan las palabras largas del evento en la consigna del turno.
            clave = [w.lower() for w in re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{7,}", evento)][:12]
            if clave and not any(w in consigna.lower() for w in clave):
                sin_eco.append(n)
        if sin_eco:
            lista = ", ".join(str(n) for n in sin_eco)
            p.append('<article class="tarjeta aviso"><h3>Decisiones que no llegaron a la '
                     "consigna</h3>"
                     f"<p>En los turnos <strong>{html.escape(lista)}</strong> se inyectó un "
                     "punto de decisión, pero ninguna de sus palabras aparece en la "
                     "pregunta con que el narrador dio la palabra. El evento entra en la "
                     "premisa; la consigna la redacta el narrador y puede no recogerlo. "
                     "Si nadie respondió a esa decisión, esta es la explicación: no "
                     "que la ignoraran, sino que no se la preguntaron. Se corrige "
                     "redactando el evento con el nombre de quien debe responder y qué "
                     "tiene que responder.</p></article>")

    # Nada filtra lo que un actor afirma: pasa a ser parte del mundo.
    p.append('<article class="tarjeta"><h3>Los hechos nuevos no se verifican</h3>'
             "<p>Si alguien menciona un dato que no estaba configurado, el motor no "
             "lo distingue de los que sí: pasa a formar parte de la situación y los "
             "demás razonan sobre él. Antes de citar un dato del texto, comprobá que "
             "esté en «Datos que todos conocían» o en las memorias de alguien. Si no "
             "está, lo inventó la corrida.</p></article>")

    if resumen.get("completa") is False:
        p.append('<article class="tarjeta aviso"><h3>Quedó incompleta</h3>'
                 "<p>Los turnos que faltan no son neutrales: los escenarios suelen "
                 "poner la decisión final al cierre, así que una corrida cortada "
                 "tiende a mostrar posiciones sin resolución. No leas la falta de "
                 "acuerdo como resultado.</p></article>")

    p.append("</div></section>")
    return "".join(p)


# ---------------------------------------------------------------- documento

MARCA_NAV = "<!--INDICE-->"

# Títulos largos para la página, cortos para el índice: «Cómo leer estos
# resultados» no entra en una barra junto a otros ocho.
ROTULO_INDICE = {
    "resultado": "Resultado",
    "incompleta": "Por qué se cortó",
    "senales": "Qué mirar",
    "interpretacion": "Cómo leerlo",
    "ficha": "Ficha",
    "diseno": "Diseño",
    "variables": "Variables",
    "narrador": "Narrador",
    "reparto": "Reparto de la palabra",
    "hitos": "Hitos",
    "datos": "Datos",
    "hallazgos": "Hallazgos",
    "controles": "Controles",
    "variaciones": "Qué probar",
    "perfiles": "Perfiles",
    "sintesis": "En pocas palabras",
    "acuerdos": "Acuerdos",
    "evolucion": "Evolución",
    "actores": "Actores",
    "ciclo": "Cómo funciona",
    "deliberacion": "Deliberación",
    "tecnica": "Ficha técnica",
}


# El informe pasó de nueve secciones a catorce y en una sola página quedan una
# atrás de otra sin jerarquía: la síntesis narrativa aparecía después del
# análisis metodológico, y la transcripción —que es el 87% del peso— se
# atravesaba en el medio. Agrupadas, el orden es: qué pasó, cómo se dio, la
# evidencia en crudo, qué dice del armado, y el material de referencia.
GRUPOS = [
    ("g-que-es", "Qué es esto", ["ficha", "diseno", "interpretacion"]),
    ("g-armo", "Cómo se armó", ["actores", "narrador", "variables", "hitos"]),
    ("g-paso", "Qué pasó", ["sintesis", "resultado", "acuerdos", "incompleta",
                            "evolucion", "perfiles"]),
    # El ciclo del turno encabeza la transcripcion: su primer paso es quien
    # recibe la palabra, y los turnos desplegados de abajo muestran esa misma
    # secuencia. Separado de ellos era una explicacion sin su ejemplo.
    ("g-delib", "La deliberación", ["ciclo", "reparto", "deliberacion"]),
    ("g-armado", "Qué dice del armado", ["senales", "controles", "hallazgos", "variaciones"]),
    ("g-datos", "Datos", ["datos", "tecnica"]),
]


def armar_pestanas(doc: str) -> str:
    """
    Agrupa las secciones en pestañas y las reordena.

    Las secciones se leen del documento ya armado, no de una lista escrita a
    mano: así una sección nueva entra sola y una que no se generó no puede
    quedar enlazada —el índice anterior enlazaba «Acuerdos» aunque no existiera—.

    Sin JavaScript el resultado sigue siendo una página corrida con todo
    visible y las pestañas funcionando como enlaces internos. El script las
    convierte en pestañas de verdad; si no corre, no se pierde nada.
    """
    ini = doc.find('<main class="ancho">')
    if ini == -1:
        return doc.replace(MARCA_NAV, "")
    fin = doc.find("</main>", ini)
    cuerpo = doc[ini + len('<main class="ancho">'):fin]

    # Las secciones son planas: no hay ninguna adentro de otra.
    bloques, titulos = {}, {}
    for m in re.finditer(r'<section id="([^"]+)"[^>]*>.*?</section>', cuerpo, re.S):
        ident = m.group(1)
        bloques[ident] = m.group(0)
        t = re.search(r"<h2>(.*?)</h2>", m.group(0))
        titulos[ident] = re.sub(r"<[^>]+>", "", t.group(1)) if t else ident
    if not bloques:
        return doc.replace(MARCA_NAV, "")

    # Lo que no esté en ningún grupo va al último, para que agregar una sección
    # y olvidarse de agruparla no la haga desaparecer del informe.
    agrupadas = {i for _, _, ids in GRUPOS for i in ids}
    sueltas = [i for i in bloques if i not in agrupadas]

    pestanas, paneles, primera = [], [], None
    for k, (gid, rotulo, ids) in enumerate(GRUPOS):
        presentes = [i for i in ids if i in bloques]
        if k == len(GRUPOS) - 1:
            presentes += sueltas
        if not presentes:
            continue
        if primera is None:
            primera = gid
        pestanas.append(
            f'<a class="pestana" href="#{presentes[0]}" role="tab" '
            f'data-grupo="{gid}" aria-controls="{gid}">{html.escape(rotulo)}</a>')
        paneles.append(f'<div class="grupo" id="{gid}" role="tabpanel">'
                       + "".join(bloques[i] for i in presentes) + "</div>")

    # Dentro de <main> no hay solo secciones: también están el pie y los dos
    # scripts. Se conserva todo lo que no sea una sección y se vuelve a poner
    # después de los paneles; re-emitiendo únicamente las secciones se perdían
    # los scripts, y las pestañas quedaban sin nada que las hiciera funcionar.
    resto = re.sub(r'<section id="[^"]+"[^>]*>.*?</section>', "", cuerpo, flags=re.S)

    barra = ('<nav class="pestanas" role="tablist"><div class="ancho barra">'
             + "".join(pestanas) + "</div></nav>")
    doc = (doc[:ini] + '<main class="ancho">' + "".join(paneles) + resto + doc[fin:])
    return doc.replace(MARCA_NAV, barra)


def parrafos(texto: str) -> str:
    trozos = [t.strip() for t in re.split(r"\n\s*\n|\n", texto or "") if t.strip()]
    return "".join(f"<p>{html.escape(t)}</p>" for t in trozos) or "<p class='vacio'>—</p>"


# Las mismas etiquetas que muestra el constructor. Si alguien configura un
# sesgo leyendo «Anclaje — se aferra al primer dato», el informe no puede
# devolverle "anchoring_bias" y obligarlo a traducir de vuelta.
SESGOS = {
    "anchoring_bias": "Anclaje — se aferra al primer dato que recibió",
    "confirmation_bias": "Confirmación — busca lo que le da la razón",
    "availability_heuristic": "Disponibilidad — pesa de más lo que recuerda fácil",
    "sunk_cost_fallacy": "Costo hundido — sigue por lo ya invertido",
    "in_group_bias": "Endogrupo — favorece a los de su propio grupo",
}
FUERZAS = {"weak": "leve", "moderate": "moderado", "strong": "fuerte"}
RASGOS = {
    "openness": "Apertura",
    "conscientiousness": "Responsabilidad",
    "extraversion": "Extraversión",
    "agreeableness": "Amabilidad",
    "neuroticism": "Inestabilidad emocional",
}


def perfil_psicologico(comp: dict) -> str:
    """
    El perfil que se le cargó al actor, en palabras.

    Estos componentes recién quedaron conectados al motor, y hasta ahora el
    informe no los mostraba: se configuraba un sesgo fuerte de anclaje y en la
    salida no quedaba rastro de que existiera. Sin esto no hay forma de atribuir
    una conducta a lo que se configuró.
    """
    if not isinstance(comp, dict):
        return ""
    filas = []

    sesgo = comp.get("cognitive_bias") or {}
    if sesgo.get("bias_type"):
        tipo = SESGOS.get(sesgo["bias_type"], bonito(sesgo["bias_type"]))
        fuerza = FUERZAS.get(sesgo.get("bias_strength", ""), sesgo.get("bias_strength", ""))
        filas.append(("Sesgo", f"{tipo}" + (f" · {fuerza}" if fuerza else "")))

    rasgos = (comp.get("personality_traits") or {}).get("traits") or {}
    if not rasgos:
        # Algunos escenarios los guardan sueltos, sin el nivel 'traits'.
        rasgos = {k: v for k, v in (comp.get("personality_traits") or {}).items()
                  if k in RASGOS}
    if rasgos:
        # De 1 a 5; se muestra el número porque la escala importa para comparar.
        filas.append(("Rasgos", " · ".join(
            f"{RASGOS.get(k, bonito(k))} {v}" for k, v in rasgos.items())))

    ident = comp.get("social_identity") or {}
    if ident.get("group_membership"):
        g = ident["group_membership"]
        g = ", ".join(g) if isinstance(g, list) else str(g)
        f = FUERZAS.get(ident.get("identification_strength", ""), "")
        filas.append(("Identidad", g + (f" · pertenencia {f}" if f else "")))

    emo = comp.get("emotion") or {}
    if emo.get("current_emotion"):
        i = emo.get("emotion_intensity")
        filas.append(("Emoción", str(emo["current_emotion"])
                      + (f" · intensidad {i}" if i else "")))

    val = comp.get("values") or {}
    nucleo = val.get("core_values")
    if nucleo:
        nucleo = ", ".join(nucleo) if isinstance(nucleo, list) else str(nucleo)
        conflicto = val.get("value_conflict")
        filas.append(("Valores", nucleo
                      + (f" · en tensión con {conflicto}" if conflicto else "")))

    tpb = comp.get("theory_of_planned_behavior") or {}
    if tpb.get("behavior"):
        detalle = " · ".join(
            f"{etq} {tpb[k]}" for k, etq in
            (("attitude", "actitud"), ("subjective_norm", "norma"),
             ("perceived_control", "control")) if tpb.get(k))
        filas.append(("Conducta prevista", str(tpb["behavior"])
                      + (f" ({detalle})" if detalle else "")))

    if not filas:
        return ""
    p = ['<table class="perfil"><tbody>']
    for k, v in filas:
        p.append(f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>")
    p.append("</tbody></table>")
    return "".join(p)


def seccion_escenario(esc: dict, orden_reales: list[str]) -> str:
    """Lo que se configuró. Va plegado: hace falta para interpretar, no para leer."""
    if not esc:
        return ""
    p = ['<details class="escenario"><summary>Cómo estaba armado el escenario</summary>',
         '<div class="cuerpo-esc">']

    # La premisa y la ficha de cada actor ya tienen su lugar propio —en
    # «Diseño de la simulación» y en «Quiénes participaron»—. Repetirlas acá
    # obligaba a comparar dos versiones de lo mismo para saber cuál mandaba.
    if esc.get("datos"):
        p.append("<h3>Datos que todos conocían</h3><ul class='lista-datos'>")
        for d in esc["datos"]:
            p.append(f"<li>{html.escape(d)}</li>")
        p.append("</ul>")

    filas = [
        ("Tipo de narrador", NOMBRES_MESA.get(esc.get("mesa_prefab", ""), esc.get("mesa_prefab", "—"))),
        ("Orden de la palabra", NOMBRES_ORDEN.get(esc.get("orden", ""), esc.get("orden", "—"))),
        ("Motor", NOMBRES_MOTOR.get(esc.get("motor", ""), esc.get("motor", "—"))),
        ("Turnos pedidos", esc.get("pasos") or "—"),
        ("Puede cerrar antes", "sí" if esc.get("cierre") else "no"),
        ("Modelo de actores", esc.get("modelo") or "—"),
        ("Modelo del narrador", esc.get("modelo_gm") or "—"),
    ]
    # La temperatura del narrador sí opera; la de los actores la fija el
    # motor por su cuenta en cada acción. Se dicen las dos, con esa aclaración,
    # porque de otro modo se atribuye a este valor una variabilidad que no
    # controla.
    if esc.get("temp_gm") is not None:
        filas.append(("Temperatura del narrador", esc["temp_gm"]))
    if esc.get("temp") is not None:
        filas.append(("Temperatura de actores",
                      f"{esc['temp']} (el motor la fija por su cuenta; no se aplica)"))
    p.append("<h3>Configuración</h3><table class='config'><tbody>")
    for k, v in filas:
        p.append(f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>")
    p.append("</tbody></table>")

    p.append("</div></details>")
    return "".join(p)


def tira_participacion(pasos, orden: list[str]) -> str:
    """Quién habló en cada turno, de un vistazo. Deja ver la rotación o su ausencia."""
    if not pasos:
        return ""
    p = ['<div class="tira">']
    for paso in pasos:
        i = orden.index(paso["quien"]) if paso["quien"] in orden else 0
        color = COLORES[i % len(COLORES)]
        p.append(f'<span class="celda" style="background:{color}" '
                 f'title="Turno {paso["n"]}: {html.escape(paso["quien"])}">{paso["n"]}</span>')
    p.append("</div>")
    p.append('<ul class="leyenda leyenda-tira">')
    for i, q in enumerate(orden):
        cuantas = sum(1 for x in pasos if x["quien"] == q)
        p.append(f'<li><span class="punto" style="background:{COLORES[i % len(COLORES)]}"></span>'
                 f'<span class="leyenda-nombre">{html.escape(q)}</span>'
                 f'<span class="leyenda-dato">{cuantas} turnos</span></li>')
    p.append("</ul>")
    return "".join(p)


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
        w.writerow(["paso", "actor", "objetivo", "consigna",
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
{actores}

INDICADORES (valor inicial y final)
{indicadores}

PERFILES PSICOLÓGICOS CONFIGURADOS
{perfiles}

LA DELIBERACIÓN, TURNO POR TURNO
{transcripcion}

Devolvé un JSON con exactamente esta forma, sin texto alrededor ni bloques de código:

{{
  "resumen": "cuatro a seis párrafos corridos, separados por \\n\\n",
  "acuerdos": ["punto sobre el que hubo acuerdo explícito", "..."],
  "pendientes": ["cuestión que quedó sin resolver", "..."],
  "perfiles": [
    {{"quien": "nombre exacto", "veredicto": "se manifiesta|no se observa|contradice",
      "detalle": "una o dos frases con la evidencia y el turno"}}
  ]
}}

En "perfiles" evaluá, para cada actor con perfil configurado, si el SESGO se nota en lo que hizo.

Antes de responder, tené presente la distinción que decide todo: perseguir su objetivo NO es evidencia de su sesgo. Que alguien defienda lo que se le pidió defender es que el objetivo funciona. El sesgo se ve solamente cuando el razonamiento se distorsiona MÁS ALLÁ de lo que el objetivo ya explica. Si la conducta se explica entera por el objetivo, la respuesta es "no se observa", aunque la persona haya sido coherente y enfática.

Cada sesgo tiene una huella propia, y hay que encontrar esa huella y no otra:
- anclaje: vuelve a la primera cifra o propuesta que escuchó y la usa de referencia aunque hayan aparecido datos mejores.
- confirmación: se le presenta evidencia que lo contradice y la descarta, la minimiza o no la responde.
- costo hundido: defiende seguir con algo invocando lo ya invertido, no lo que rinde de acá en adelante.
- disponibilidad: generaliza a partir de un caso puntual que recuerda, y le da más peso que a datos agregados.
- endogrupo: evalúa la misma propuesta distinto según quién la haya hecho.

Poné "se manifiesta" solo si podés señalar el turno donde se ve ESA huella y citar qué dijo. Poné "contradice" si hizo lo opuesto: por ejemplo, alguien con sesgo de confirmación que incorpora una objeción que lo desmiente. En cualquier otro caso poné "no se observa", y en el detalle explicá qué se vio en cambio.

Esperamos que "no se observa" sea frecuente: los componentes influyen sin determinar, y fuera del tipo Mínimo compiten con el resto de la memoria por entrar en cada acción. Una lista donde los cinco se manifiestan es señal de que se está confirmando lo configurado en vez de ponerlo a prueba, y eso no sirve. Si nadie tiene perfil, devolvé la lista vacía.

En "acuerdos" poné solo lo que fue aceptado explícitamente por los actores, no lo que alguien propuso y nadie contestó. En "pendientes" poné lo que se planteó y quedó sin respuesta, lo que se objetó sin resolverse, y lo que el escenario pedía decidir y no se decidió. Cada entrada, una frase corta y concreta. Si no hubo acuerdos explícitos, devolvé la lista vacía.

El campo "resumen" tiene que cubrir:
1. Qué se discutió y qué se resolvió, si es que se resolvió algo.
2. Qué defendió cada actor y si su posición cambió a lo largo de los turnos.
3. Dónde hubo desacuerdo real y dónde hubo adhesión sin reparos. Sé específico: si alguien aceptó una propuesta sin objetar nada, decilo y señalá en qué turno.
4. Si los indicadores se movieron de forma coherente con lo que efectivamente se dijo.

Reglas estrictas:
- Basate únicamente en lo que aparece en la transcripción. No inventes citas, hechos ni acuerdos.
- Si algo no se resolvió o quedó ambiguo, decilo con todas las letras en lugar de completarlo.
- Nada de vocabulario de manual ni conclusiones infladas. Si la deliberación fue floja, decilo.
- No uses títulos ni viñetas: párrafos corridos.

Sobre el nivel de consenso, si se midió: que llegue al máximo NO es un buen resultado por sí mismo. En una mesa donde los actores fueron diseñados con intereses en conflicto, un consenso altísimo alcanzado sin objeciones sostenidas indica que los personajes no defendieron sus posiciones, y eso es un problema del escenario que hay que señalar, no un logro que celebrar.

No cierres con un veredicto sobre si la deliberación estuvo bien o mal, ni con una frase de síntesis elogiosa. Terminá con lo que quedó sin resolver o con lo que habría que revisar del escenario."""


PROMPT_HALLAZGOS = """Sos analista metodológico y revisás una simulación deliberativa ya corrida. No te interesa contar qué pasó —eso ya está escrito— sino qué se puede aprender del armado y qué habría que cambiar para la próxima.

CONTEXTO DEL ESCENARIO
{contexto}

CÓMO ESTABA ARMADO
- Narrador: {narrador}
- Motor: {motor}
- Orden de la palabra: {orden}
- Turnos pedidos: {pedidos} · efectivamente corridos: {corridos}

PERFILES PSICOLÓGICOS CONFIGURADOS
{perfiles}

INDICADORES Y SU REGLA DE ACTUALIZACIÓN
{reglas}

CÓMO SE MOVIÓ CADA INDICADOR, TURNO A TURNO
{trayectorias}

MOMENTOS DE DECISIÓN INYECTADOS
{decisiones}

LA DELIBERACIÓN, TURNO POR TURNO
{transcripcion}

Devolvé un JSON con exactamente esta forma, sin texto alrededor ni bloques de código:

{{
  "hallazgos": [
    {{"tema": "Componentes psicológicos|Indicadores|Dinámica|Método",
      "texto": "dos a cuatro frases con la evidencia concreta"}}
  ],
  "variaciones": [
    {{"cambio": "qué cambiar, con nombre y valor exactos",
      "hipotesis": "qué pone a prueba ese cambio",
      "esperado": "qué habría que observar si la hipótesis es cierta"}}
  ],
  "mejoras": ["cambio puntual al diseño de este escenario", "..."]
}}

En "hallazgos" cubrí, y salteá la categoría donde no haya evidencia suficiente en vez de especular:

- Componentes psicológicos: ¿los sesgos configurados produjeron las distorsiones esperadas, como patrón general? ¿Los rasgos se notaron en el estilo? Si no se configuró ninguno, decí qué podría revelar agregarlos.
- Indicadores: ¿el narrador aplicó las reglas de actualización que se le escribieron? Señalá los que se movieron de manera incoherente con lo que se dijo, los que quedaron planos cuando había motivo para moverse, y los que se movieron juntos como si el narrador no los distinguiera.
- Dinámica: qué fenómenos aparecieron —coaliciones, persuasión, bloqueo, adhesión sin objetar, alguien que arrastra al resto— y quién los inició.
- Método: qué confusores o limitaciones tiene este armado. Sé concreto: si dos cosas cambiaron a la vez y no se puede atribuir el efecto a ninguna, decilo.

En "variaciones" proponé tres o cuatro modificaciones para una corrida siguiente. Cada una tiene que ser una sola cosa que cambia, nombrada con precisión —qué actor, qué parámetro, qué valor— para que el efecto sea atribuible. Ejemplos de la forma que buscamos: cambiar el sesgo de alguien de confirmación a anclaje para probar si importa el tipo o solo la presencia; pasar de secuencial a simultáneo para probar si el orden de turno da poder de fijar agenda; sacar un indicador de los ocho para probar si el narrador los sigue mejor con menos.

En "mejoras" poné cambios puntuales a ESTE escenario: un objetivo que quedó vago, un dato que faltó y alguien tuvo que inventar, un hito crítico mal ubicado, un indicador cuya regla no es accionable.

Reglas estrictas:
- Basate solo en lo que aparece más arriba. No inventes citas ni hechos.
- Cada afirmación tiene que poder rastrearse a un turno, un indicador o un valor de configuración. Si no podés señalar dónde se ve, no lo digas.
- Nada de recomendaciones genéricas del tipo «agregar más contexto». Si proponés algo, decí qué exactamente y dónde.
- Si la corrida quedó incompleta, tenelo en cuenta: lo que no pasó puede deberse a que se cortó, no al diseño."""


# A qué modelo caer cuando falta la clave del proveedor que usó la corrida.
POR_DEFECTO = {"gemini": "gemini-3.5-flash-lite", "groq": "llama-3.3-70b-versatile"}


def clave_para(modelo: str) -> tuple[str, str, str]:
    """
    Con qué clave, contra qué proveedor y con qué modelo se pide el análisis.

    El proveedor sale del nombre del modelo, igual que en el constructor. Si la
    corrida usó Groq, pedirle el análisis a Gemini con un nombre como
    «llama-3.3-70b-versatile» falla, y el informe sale sin resumen, sin
    veredictos y sin hallazgos: justo las secciones que cuestan una llamada.

    Cuando falta la clave del proveedor que corresponde se cae al otro, y ahí
    hay que cambiar también el modelo: mandar el nombre de un modelo de Groq al
    endpoint de Gemini falla igual que no tener clave. El análisis lee la
    transcripción, no la continúa, así que otro modelo sirve.

    Devuelve ("", "", "") si no hay ninguna clave.
    """
    import os
    claves = {"gemini": os.getenv("GEMINI_API_KEY", "").strip(),
              "groq": os.getenv("GROQ_API_KEY", "").strip()}
    propio = "gemini" if modelo.startswith("gemini") else "groq"
    if claves[propio]:
        return claves[propio], propio, modelo
    otro = "groq" if propio == "gemini" else "gemini"
    if claves[otro]:
        return claves[otro], otro, POR_DEFECTO[otro]
    return "", "", ""


def pedir_al_modelo(prompt: str, modelo: str, clave: str, timeout: int = 180,
                    tope: int = 4000, proveedor: str = "gemini") -> str:
    """
    Llamada REST con biblioteca estándar. A propósito no se usa el motor de
    simulación: así este script corre sobre resultados viejos, en una máquina
    sin las dependencias pesadas instaladas, o lo corre otra persona.

    Groq habla el protocolo de OpenAI, así que cambian la URL, la forma del
    cuerpo y dónde viaja la clave, pero no el resto del script.
    """
    import urllib.request

    if proveedor == "groq":
        url = "https://api.groq.com/openai/v1/chat/completions"
        cuerpo = json.dumps({
            "model": modelo,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": tope,
        }).encode("utf-8")
        cabeceras = {"Content-Type": "application/json",
                     "Authorization": f"Bearer {clave}"}
    else:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{modelo}:generateContent?key={clave}")
        cuerpo = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": tope},
        }).encode("utf-8")
        cabeceras = {"Content-Type": "application/json"}

    pedido = urllib.request.Request(url, data=cuerpo, headers=cabeceras, method="POST")
    with urllib.request.urlopen(pedido, timeout=timeout) as r:
        datos = json.loads(r.read().decode("utf-8"))

    if proveedor == "groq":
        opciones = datos.get("choices") or [{}]
        return (opciones[0].get("message", {}).get("content") or "").strip()
    partes = datos.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in partes).strip()


def texto_perfiles(agentes) -> str:
    """
    Los perfiles configurados, en una línea por actor, para que el modelo
    pueda contrastarlos con la conducta observada. Sin esto solo se le puede
    preguntar qué pasó, no si pasó lo que se había configurado.
    """
    lineas = []
    for a in agentes or []:
        comp = a.get("components") or {}
        partes = []
        sesgo = comp.get("cognitive_bias") or {}
        if sesgo.get("bias_type"):
            partes.append(f"sesgo de {SESGOS.get(sesgo['bias_type'], sesgo['bias_type']).split(' — ')[0].lower()}"
                          f" ({FUERZAS.get(sesgo.get('bias_strength', ''), 'sin graduar')})")
        # Igual que en la ficha: algunos escenarios los guardan planos, sin el
        # nivel 'traits', y leyendo solo el anidado los rasgos no llegaban al
        # modelo aunque estuvieran configurados.
        rasgos = (comp.get("personality_traits") or {}).get("traits") or {}
        if not rasgos:
            rasgos = {k: v for k, v in (comp.get("personality_traits") or {}).items()
                      if k in RASGOS}
        if rasgos:
            partes.append("rasgos " + ", ".join(
                f"{RASGOS.get(k, k).lower()} {v}/5" for k, v in rasgos.items()))
        ident = comp.get("social_identity") or {}
        if ident.get("group_membership"):
            g = ident["group_membership"]
            partes.append("identidad: " + (", ".join(g) if isinstance(g, list) else str(g)))
        val = (comp.get("values") or {}).get("core_values")
        if val:
            partes.append("valores: " + (", ".join(val) if isinstance(val, list) else str(val)))
        emo = (comp.get("emotion") or {}).get("current_emotion")
        if emo:
            partes.append(f"emoción: {emo}")
        if partes:
            lineas.append(f"- {a.get('name', '?')}: " + "; ".join(partes))
    return "\n".join(lineas) or "(no se configuraron perfiles psicológicos)"


def analizar_hallazgos(pasos, series, esc, resumen_datos) -> dict:
    """
    Revisión metodológica: qué se aprende del armado y qué cambiar para la
    próxima. Es el segundo y tercer prompt del analizador del proyecto original,
    de los que solo usábamos el primero.

    Va en una llamada aparte, no sumada a la del resumen. El tope de salida son
    4000 tokens y el otro pedido ya usa buena parte: si el JSON se corta, se
    pierde todo lo de esa llamada, no solo lo que se agregó. Separadas, cada una
    tiene lugar y una falla no arrastra a la otra. Contra la cuota diaria esto
    cuesta una llamada más por informe, no por turno.
    """
    crudo_nombre = (resumen_datos or {}).get("modelo_gm") or (resumen_datos or {}).get("modelo") or ""
    modelo = crudo_nombre.split("/")[-1].strip() or "gemini-3.5-flash-lite"
    clave, proveedor, modelo = clave_para(modelo)
    if not clave or not pasos:
        return {}

    # La regla de actualización es lo que el narrador tenía que aplicar; sin
    # ella no se puede evaluar si la aplicó.
    reglas = "\n".join(
        f"- {bonito(v.get('name', ''))} ({v.get('variable_type', 'sin tipo')}): "
        f"{v.get('update_rule') or v.get('description') or 'sin regla escrita'}"
        for v in (esc.get("variables") or [])) or "(no se definieron indicadores)"

    # Trayectoria completa, no solo extremos: un indicador que sube y vuelve
    # dice algo distinto de uno que no se movió, y con los extremos se ven igual.
    trayectorias = "\n".join(
        f"- {bonito(n)}: " + " → ".join(f"t{p}:{v:.0f}" for p, v in s)
        for n, s in sorted(series.items())) or "(no se midieron indicadores)"

    decisiones = "\n".join(
        f"- turno {d.get('step')}: {str(d.get('event', ''))[:300]}"
        for d in (esc.get("decisiones") or [])) or "(no se inyectaron)"

    transcripcion = "\n\n".join(
        f"Turno {p['n']} — {p['quien']}:\n{(p['dicho'] or p['evento'])[:900]}" for p in pasos)

    prompt = PROMPT_HALLAZGOS.format(
        contexto=(esc.get("premisa") or "(sin premisa registrada)")[:1500],
        narrador=NOMBRES_MESA.get(esc.get("mesa_prefab", ""), esc.get("mesa_prefab") or "—"),
        motor=NOMBRES_MOTOR.get(esc.get("motor", ""), esc.get("motor") or "—"),
        orden=NOMBRES_ORDEN.get(esc.get("orden", ""), esc.get("orden") or "—"),
        pedidos=esc.get("pasos") or "?",
        corridos=len(pasos),
        perfiles=texto_perfiles(esc.get("agentes")),
        reglas=reglas,
        trayectorias=trayectorias,
        decisiones=decisiones,
        transcripcion=transcripcion[:45000],
    )

    try:
        bruto = pedir_al_modelo(prompt, modelo, clave, tope=8000, proveedor=proveedor)
    except Exception as e:
        detalle = getattr(e, "reason", None) or e
        print(f"  hallazgos omitidos: {type(e).__name__}: {str(detalle)[:160]}")
        return {}

    limpio = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", bruto.strip())
    try:
        d = json.loads(limpio)
    except json.JSONDecodeError:
        print("  hallazgos omitidos: la respuesta no era JSON válido")
        return {}
    if not isinstance(d, dict):
        return {}
    return {
        "hallazgos": [x for x in (d.get("hallazgos") or []) if isinstance(x, dict) and x.get("texto")],
        "variaciones": [x for x in (d.get("variaciones") or []) if isinstance(x, dict) and x.get("cambio")],
        "mejoras": [str(x) for x in (d.get("mejoras") or []) if str(x).strip()],
    }


def analizar_con_modelo(pasos, series, resumen_datos, premisa: str, agentes=None) -> dict:
    """
    Resumen, acuerdos y pendientes en UNA sola llamada. Se piden juntos a
    propósito: la cuota diaria del plan gratuito es de 500 llamadas por modelo,
    así que tres pedidos separados costarían el triple sin agregar nada.

    Devuelve {} si algo falla: el reporte tiene que salir igual, porque el resto
    no depende de esto.
    """
    crudo_nombre = (resumen_datos or {}).get("modelo_gm") or (resumen_datos or {}).get("modelo") or ""
    modelo = crudo_nombre.split("/")[-1].strip() or "gemini-3.5-flash-lite"
    clave, proveedor, modelo = clave_para(modelo)
    if not clave:
        print("  análisis omitido: no hay clave de ningún proveedor")
        return {}

    objetivos, orden = {}, []
    for p in pasos:
        if p["quien"] not in orden:
            orden.append(p["quien"])
        if p["objetivo"] and p["quien"] not in objetivos:
            objetivos[p["quien"]] = p["objetivo"]

    actores = "\n".join(f"- {q}: {objetivos.get(q, 'sin objetivo declarado')}" for q in orden)
    indicadores = "\n".join(
        f"- {bonito(n)}: empezó en {s[0][1]:.0f} y terminó en {s[-1][1]:.0f}"
        for n, s in sorted(series.items())) or "(no se midieron indicadores)"
    transcripcion = "\n\n".join(
        f"Turno {p['n']} — {p['quien']}:\n{(p['dicho'] or p['evento'])[:1200]}" for p in pasos)

    prompt = PROMPT_RESUMEN.format(
        contexto=(premisa or "(no se registró la consigna del escenario)")[:2000],
        actores=actores,
        indicadores=indicadores,
        perfiles=texto_perfiles(agentes),
        transcripcion=transcripcion[:60000],
    )

    # Se reusa el modelo de la corrida para no introducir uno nuevo sin aviso
    try:
        bruto = pedir_al_modelo(prompt, modelo, clave, proveedor=proveedor)
    except Exception as e:
        detalle = getattr(e, "reason", None) or e
        print(f"  análisis omitido: {type(e).__name__}: {str(detalle)[:160]}")
        return {}

    # El modelo suele envolver el JSON en un bloque de código pese a pedirle
    # que no lo haga; si aun así no se puede parsear, se usa como resumen suelto.
    limpio = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", bruto.strip())
    d = None
    try:
        d = json.loads(limpio)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", limpio, re.S)
        if m:
            try:
                d = json.loads(m.group(0))
            except json.JSONDecodeError:
                d = None

    # Si contestó en prosa en vez de JSON, el texto sirve igual como resumen.
    # Descartarlo sería tirar una llamada que ya se pagó de la cuota diaria.
    if not isinstance(d, dict) or not d:
        if bruto.strip():
            print("  el análisis no vino como JSON; se usa el texto como resumen")
            return {"resumen": bruto.strip(), "acuerdos": [], "pendientes": []}
        return {}
    return {
        "resumen": str(d.get("resumen") or "").strip(),
        "acuerdos": [str(x).strip() for x in (d.get("acuerdos") or []) if str(x).strip()],
        "pendientes": [str(x).strip() for x in (d.get("pendientes") or []) if str(x).strip()],
        # Se pedía en el prompt y se dibujaba en el informe, pero esta lista
        # blanca lo descartaba en el medio: el veredicto llegaba del modelo y se
        # tiraba acá, así que la sección nunca aparecía.
        "perfiles": [x for x in (d.get("perfiles") or [])
                     if isinstance(x, dict) and x.get("quien")],
    }


# La primera etapa depende de cómo se configuró el orden: decía siempre «el
# narrador decide a quién le toca hablar», y con orden fijo el narrador no
# decide nada —la rotación sale de la lista de actores—. Atribuirle una
# decisión que no toma es afirmar de más sobre la única etapa que puede
# introducir sesgo de agenda.
PRIMERA_ETAPA = {
    "fixed": ("Toca", "la rotación sigue el orden en que se listaron los actores"),
    "random": ("Sortea", "el turno se sortea entre los actores"),
    "game_master_choice": ("Elige", "el narrador decide a quién le toca hablar"),
}

ETAPAS = [
    ("Elige", "el narrador decide a quién le toca hablar"),
    ("Pregunta", "le hace una consigna, distinta según el momento"),
    ("Responde", "el actor dice o hace algo"),
    ("Registra", "el narrador lo convierte en un hecho ocurrido"),
    ("Reparte", "le cuenta a cada uno lo que pasó, desde su lugar"),
]


def parecido(a: str, b: str) -> float:
    """Cuánto se parecen dos textos, ignorando espacios."""
    import difflib
    na = re.sub(r"\s+", " ", a or "").strip()
    nb = re.sub(r"\s+", " ", b or "").strip()
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def reparto_de_la_palabra(pasos, esc) -> str:
    """
    Cómo se asignó cada turno, y si se cumplió.

    El informe mostraba quién habló y decía que el narrador lo decidía, sin
    verificar ninguna de las dos cosas. Con orden fijo no hay decisión: la
    rotación sale de la lista de actores, y entonces lo que importa es si
    se respetó. Con orden al azar o a criterio del narrador sí hay decisión, y
    conviene decir que el registro no guarda el motivo: no es algo que se pueda
    auditar mirando el log.
    """
    orden = esc.get("orden") or ""
    lista = [a.get("name") for a in (esc.get("agentes") or []) if a.get("name")]
    p = ['<section id="reparto"><h2>Cómo se repartió la palabra</h2>']

    if orden == "fixed" and lista:
        desvios = [(x["n"], x["quien"], lista[(x["n"] - 1) % len(lista)])
                   for x in pasos
                   if x["quien"] != lista[(x["n"] - 1) % len(lista)]]
        p.append('<p class="nota-datos">El orden es <strong>fijo</strong>: el narrador no elige. '
                 "La rotación sigue el orden en que se listaron los actores — "
                 + html.escape(" → ".join(lista)) + " — y vuelve a empezar.</p>")
        if not desvios:
            p.append(f'<p class="nota-datos">{marca("medido")} Se respetó en los '
                     f"{len(pasos)} turnos: cada uno habló cuando le tocaba.</p>")
        else:
            detalle = "; ".join(f"en el turno {n} habló {q} y correspondía {t}"
                                for n, q, t in desvios[:4])
            p.append(f'<p class="nota-datos aviso-txt">{marca("medido")} La rotación '
                     f"<strong>no se respetó</strong> en {len(desvios)} de {len(pasos)} turnos: "
                     + html.escape(detalle) + ". Con orden fijo eso no debería ocurrir.</p>")
    elif orden in ("random", "game_master_choice"):
        if orden == "random":
            p.append('<p class="nota-datos">El turno se <strong>sortea</strong> entre los '
                     "actores. No hay rotación contra la cual contrastar: un reparto "
                     "desparejo es esperable por azar y no indica nada por sí solo.</p>")
        else:
            p.append('<p class="nota-datos">El <strong>narrador elige</strong> a quién le da '
                     "la palabra en cada turno. Es la etapa con más poder de agenda de toda "
                     "la simulación, y el registro guarda a quién eligió pero no por qué: el "
                     "motivo no queda auditable mirando el log.</p>")
        # Sin rotación que verificar, lo que queda es el reparto que resultó.
        # Es la única evidencia disponible de cómo se ejerció esa decisión.
        veces = {}
        for x in pasos:
            veces[x["quien"]] = veces.get(x["quien"], 0) + 1
        for n in lista:
            veces.setdefault(n, 0)
        if veces:
            parejo = len(pasos) / len(veces)
            orden_v = sorted(veces.items(), key=lambda x: -x[1])
            p.append(f'<p class="nota-datos">{marca("medido")} Así quedó repartido, sobre '
                     f"{len(pasos)} turnos (parejo sería {parejo:.1f} cada uno):</p>")
            p.append('<ul class="lista-datos reparto">')
            for n, c in orden_v:
                p.append(f"<li>{html.escape(n)}: <strong>{c}</strong> "
                         + ("turno" if c == 1 else "turnos")
                         + (" — nunca habló" if not c else "") + "</li>")
            p.append("</ul>")
            if orden_v[0][1] >= 2 * max(1, orden_v[-1][1]):
                p.append('<p class="nota-datos aviso-txt">El más favorecido habló al menos el '
                         "doble que el menos favorecido. Si el escenario depende de que todos "
                         "sostengan su posición, conviene mirar si los que hablaron poco son "
                         "los que tenían las objeciones.</p>")
    else:
        p.append('<p class="nota-datos">No se registró con qué regla se asignaron los '
                 "turnos.</p>")
    p.append('<p class="nota-datos">Cada casilla es un turno, coloreada según quién '
             "habló.</p>")
    p.append(tira_participacion(pasos, [a.get("name") for a in (esc.get("agentes") or [])]
                                or sorted({x["quien"] for x in pasos})))
    p.append("</section>")
    return "".join(p)


def ciclo(pasos, orden: str = "") -> str:
    """
    El orden de un turno no se deduce leyendo la transcripción, y sin él no se
    entiende quién puede introducir un hecho en el mundo. Se dibuja el ciclo y
    se mide, sobre esta corrida, cuánto interviene realmente el narrador.
    """
    etapas = list(ETAPAS)
    etapas[0] = PRIMERA_ETAPA.get(orden, ETAPAS[0])
    p = ['<div class="ciclo">']
    for i, (titulo, detalle) in enumerate(etapas):
        p.append('<div class="etapa">')
        p.append(f'<span class="etapa-n">{i + 1}</span>')
        p.append(f'<p class="etapa-t">{titulo}</p>')
        p.append(f'<p class="etapa-d">{detalle}</p>')
        p.append("</div>")
        if i < len(etapas) - 1:
            p.append('<div class="flecha" aria-hidden="true">→</div>')
    p.append("</div>")

    # Paso 4: ¿el narrador transforma lo dicho, o lo copia tal cual?
    comparables = [x for x in pasos if x["dicho"] and x["evento"]]
    if comparables:
        calcados = sum(1 for x in comparables if parecido(x["dicho"], x["evento"]) > 0.95)
        if calcados == len(comparables):
            p.append('<div class="nota-ciclo aviso-ciclo">'
                     f'<strong>En los {len(comparables)} turnos, el hecho registrado quedó '
                     'idéntico a lo que dijo el actor.</strong> El narrador no filtró '
                     'ni reformuló nada en el paso 4: lo que alguien escribe pasa a ser un '
                     'hecho del mundo tal cual. Por eso pueden aparecer sucesos que nadie '
                     'configuró —una falla, una carta, un informe— y quedan como ocurridos.'
                     "</div>")
        elif calcados:
            p.append(f'<div class="nota-ciclo">El narrador dejó el texto tal cual en '
                     f'{calcados} de {len(comparables)} turnos y lo reformuló en el resto.</div>')

    # Paso 5: ¿le escribe algo distinto a cada uno?
    con_obs = [x for x in pasos if (x.get("mesa") or {}).get("observaciones")]
    if con_obs:
        textos = list(con_obs[0]["mesa"]["observaciones"].values())
        distintas = len(textos) > 1 and any(
            parecido(textos[0], t) < 0.95 for t in textos[1:])
        p.append('<div class="nota-ciclo">'
                 + (f'A cada actor le escribió una observación distinta, redactada '
                    'desde su punto de vista: no todos se enteran de lo mismo de la misma '
                    'manera.' if distintas
                    else "A todos les mandó la misma observación.")
                 + "</div>")
    return "".join(p)


def marca(clase: str) -> str:
    """
    Distingue de dónde sale cada cosa. Un número medido y uno que el modelo
    estimó interpretando la charla no valen lo mismo, y en un informe de
    políticas confundirlos es peor que no mostrarlos.
    """
    textos = {
        "medido": ("▪", "Medido", "Sale del registro de la corrida."),
        "estimado": ("▫", "Estimado por el narrador",
                     "Lo asignó el narrador, interpretando la deliberación. "
                     "No es una medición."),
        "inferido": ("◇", "Inferido",
                     "Lo dedujo un modelo al leer la transcripción."),
    }
    simbolo, rotulo, ayuda = textos[clase]
    return (f'<span class="proc p-{clase}" title="{html.escape(ayuda)}">'
            f'{simbolo} {html.escape(rotulo)}</span>')


def armar(pasos, series, franjas, resumen, decisiones, esc=None, analisis=None,
          archivos=None) -> str:
    esc = esc or {}
    analisis = analisis or {}
    texto_resumen = analisis.get("resumen", "")
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
    completa = not (resumen and resumen.get("completa") is False)
    obs = senales(pasos, series, franjas, esc, resumen)

    partes = [CABEZA]

    # ---------------------------------------------------------- 1. identidad
    nombre_esc = esc.get("mesa_nombre") or "Simulación deliberativa"
    partes.append('<header class="tapa"><div class="ancho">')
    partes.append('<p class="marca-doc">Informe de simulación deliberativa</p>')
    partes.append(f"<h1>{html.escape(nombre_esc)}</h1>")

    linea = [f"{len(quienes)} actores", f"{max_paso} turnos"]
    if resumen and resumen.get("duracion_min") is not None:
        linea.append(f"{resumen['duracion_min']} min")
    if resumen and resumen.get("terminada"):
        linea.append(str(resumen["terminada"])[:10])
    partes.append(f'<p class="linea-datos">{" · ".join(html.escape(x) for x in linea)}</p>')

    partes.append('<p class="chapa ' + ("ok" if completa else "aviso") + '">'
                  + ("Simulación completa" if completa else "Simulación incompleta") + "</p>")

    # El índice se arma al final, con las secciones que de verdad se generaron.
    # Escrito a mano quedaba desfasado: enlazaba «Acuerdos», que solo existe si
    # se pidió el análisis, y no incluía las secciones agregadas después.
    partes.append("</div></header>")
    # Fuera del encabezado a propósito: `sticky` solo pega mientras el padre
    # sigue en pantalla, así que adentro del header el índice desaparecía
    # justo cuando empieza a hacer falta.
    partes.append(MARCA_NAV)

    partes.append('<main class="ancho">')

    # La ficha y el diseño van antes que cualquier resultado: primero hay que
    # saber qué clase de cosa es esto y qué se simuló.
    partes.append(seccion_ficha(pasos, esc, resumen))
    partes.append(seccion_diseno(pasos, series, esc))

    # ------------------------------------------------- 2. resultado y estado
    partes.append('<section id="resultado"><h2>Qué resultó</h2>')
    partes.append('<div class="tarjetas">')

    definidas = {v.get("name"): v for v in esc.get("variables", [])}
    mostradas = set()

    for nombre, serie in sorted(franjas.items()):
        mostradas.add(nombre)
        partes.append('<div class="tarjeta">')
        partes.append(f'<p class="t-rotulo">{html.escape(bonito(nombre))}</p>')
        partes.append(f'<p class="t-valor">{html.escape(valor_bonito(serie[-1][1]))}</p>')
        partes.append(f'<p class="t-pie">{marca("estimado")}</p>')
        partes.append("</div>")

    for nombre, serie in sorted(series.items()):
        if "consenso" not in nombre.lower():
            continue
        mostradas.add(nombre)
        alto = max(v for _, v in serie)
        partes.append('<div class="tarjeta">')
        partes.append(f'<p class="t-rotulo">{html.escape(bonito(nombre))}</p>')
        partes.append(f'<p class="t-valor">{serie[-1][1]:.0f}<span class="t-sobre">/100</span></p>')
        if alto > serie[-1][1]:
            partes.append(f'<p class="t-nota">llegó a {alto:.0f} y bajó</p>')
        partes.append(f'<p class="t-pie">{marca("estimado")}</p>')
        partes.append("</div>")

    partes.append('<div class="tarjeta">')
    partes.append('<p class="t-rotulo">Decisión definitiva</p>')
    if completa:
        partes.append('<p class="t-valor chico">Se completaron todos los turnos</p>')
    else:
        partes.append('<p class="t-valor chico aviso-txt">Pendiente</p>')
        partes.append('<p class="t-nota">la corrida no llegó al cierre</p>')
    partes.append(f'<p class="t-pie">{marca("medido")}</p>')
    partes.append("</div>")
    partes.append("</div>")

    # Lo que el escenario no midió se dice, en vez de completarlo
    faltantes = [v for v in ("estado de aprobación", "viabilidad de implementación")]
    if not any("estado" in n.lower() or "implement" in n.lower()
               for n in list(series) + list(franjas)):
        partes.append('<p class="sin-medir">Este escenario no definió indicadores para '
                      f'{" ni ".join(faltantes)}, así que el informe no puede decir si lo '
                      'acordado quedó aprobado ni si es aplicable. Para que aparezcan, hay '
                      'que definirlos como indicadores antes de correr.</p>')
    partes.append("</section>")

    # ------------------------------------------- 3. por qué quedó incompleta
    if not completa:
        hechos = resumen.get("pasos_completados") or 0
        pedidos = resumen.get("pasos_pedidos") or 0
        sin_llegar = [d for d in (decisiones or []) if (d.get("step") or 0) > hechos]
        partes.append('<section id="incompleta" class="incompleta"><h2>Por qué quedó incompleta</h2>')
        partes.append(f'<p>Se ejecutaron {hechos} de los {pedidos} turnos previstos.</p>')
        if sin_llegar:
            partes.append("<p>No se llegó a estos hitos críticos:</p><ul>")
            for d in sin_llegar:
                partes.append(f'<li><strong>Turno {d["step"]}:</strong> '
                              f'{html.escape((d.get("event") or "")[:220])}</li>')
            partes.append("</ul>")
        motivo = resumen.get("error") or ""
        if "429" in motivo or "RESOURCE_EXHAUSTED" in motivo:
            partes.append('<p class="motivo">Se cortó por agotarse la cuota diaria del modelo, '
                          "no por un problema del escenario.</p>")
        elif motivo:
            partes.append(f'<p class="motivo">{html.escape(motivo[:300])}</p>')
        partes.append('<p class="motivo">Cualquier acuerdo que aparezca hay que leerlo como '
                      "provisorio: no hubo un cierre formal posterior.</p>")
        partes.append("</section>")

    # ------------------------------------------------------- 4. qué mirar
    if obs:
        partes.append('<section id="senales" class="senales"><h2>Qué mirar de esta corrida</h2>')
        partes.append(f'<p class="ayuda-sec">{marca("medido")} Observaciones calculadas sobre '
                      "el registro, sin intervención de ningún modelo.</p>")
        partes.append("<ul>")
        for tipo, texto in obs:
            partes.append(f'<li class="s-{tipo}">{html.escape(texto)}</li>')
        partes.append("</ul></section>")

    # Qué se aprende del armado, no de la deliberación. Va después de los
    # veredictos y antes de la ayuda de lectura: primero qué salió, después qué
    # significa para el diseño, después con qué alcance leer todo.
    hallazgos = analisis.get("hallazgos") or []
    if hallazgos:
        partes.append('<section id="hallazgos"><h2>Qué dice esto del armado</h2>')
        partes.append(f'<p class="ayuda-sec">{marca("inferido")} Revisión metodológica de la '
                      "corrida: si el narrador aplicó las reglas que se le escribieron, si los "
                      "componentes produjeron lo esperado, y qué limitaciones tiene este "
                      "diseño para atribuir lo observado.</p>")
        partes.append('<div class="tarjetas">')
        for h in hallazgos:
            tema = html.escape(str(h.get("tema") or "Observación"))
            partes.append(f'<article class="tarjeta"><h3>{tema}</h3>'
                          f'<p>{html.escape(str(h["texto"]))}</p></article>')
        partes.append("</div></section>")

    variaciones = analisis.get("variaciones") or []
    mejoras = analisis.get("mejoras") or []
    if variaciones or mejoras:
        partes.append('<section id="variaciones"><h2>Qué probar en la próxima</h2>')
        if variaciones:
            partes.append(f'<p class="ayuda-sec">{marca("inferido")} Cada variación cambia '
                          "<strong>una sola cosa</strong>, para que el efecto sea atribuible a "
                          "ese cambio y no a varios a la vez. La guía del proyecto recomienda "
                          "de tres a cinco corridas; esto es qué variar entre una y otra.</p>")
            partes.append('<ol class="variaciones">')
            for v in variaciones:
                partes.append('<li><p class="v-cambio">'
                              + html.escape(str(v["cambio"])) + "</p>")
                if v.get("hipotesis"):
                    partes.append('<p class="v-linea"><span class="et-v">Pone a prueba</span>'
                                  + html.escape(str(v["hipotesis"])) + "</p>")
                if v.get("esperado"):
                    partes.append('<p class="v-linea"><span class="et-v">Habría que ver</span>'
                                  + html.escape(str(v["esperado"])) + "</p>")
                partes.append("</li>")
            partes.append("</ol>")
        if mejoras:
            partes.append("<h3>Arreglos a este escenario</h3>")
            partes.append('<ul class="lista-datos ajustes">')
            for m in mejoras:
                partes.append(f"<li>{html.escape(m)}</li>")
            partes.append("</ul>")
        partes.append("</section>")

    partes.append(seccion_narrador(pasos, esc, resumen, series, franjas))
    partes.append(seccion_variables(series, franjas, esc))
    partes.append(seccion_hitos(pasos, esc, resumen))
    partes.append(seccion_controles(pasos, esc))
    partes.append(ayuda_interpretacion(pasos, series, franjas, esc, resumen))
    partes.append(seccion_datos(archivos, resumen))

    # ---------------------------------------------------------- 5. resumen
    if texto_resumen:
        partes.append('<section id="sintesis" class="resumen"><h2>En pocas palabras</h2>')
        partes.append(f'<p class="marca-ia">{marca("inferido")}</p>')
        partes.append(parrafos(texto_resumen))
        partes.append("</section>")

    # --------------------------------------------- 5b. acuerdos y pendientes
    acuerdos = analisis.get("acuerdos") or []
    pendientes = analisis.get("pendientes") or []
    if acuerdos or pendientes:
        partes.append('<section id="acuerdos"><h2>Acuerdos y cuestiones pendientes</h2>')
        partes.append(f'<p class="ayuda-sec">{marca("inferido")} Lo extrajo un modelo leyendo '
                      'la transcripción. En «acuerdos» va solo lo que alguien aceptó de forma '
                      'explícita, no lo que se propuso y nadie respondió.</p>')
        partes.append('<div class="dos-columnas">')
        for titulo, items, clase in (("Acuerdos alcanzados", acuerdos, "col-acuerdo"),
                                     ("Quedó sin resolver", pendientes, "col-pendiente")):
            partes.append(f'<div class="columna {clase}"><h3>{titulo}</h3>')
            if items:
                partes.append("<ul>")
                for x in items:
                    partes.append(f"<li>{html.escape(x)}</li>")
                partes.append("</ul>")
            else:
                partes.append('<p class="vacio">Ninguno registrado.</p>')
            partes.append("</div>")
        partes.append("</div></section>")

    # Si se configuró un sesgo, la pregunta que sigue es si se notó. El informe
    # mostraba el perfil configurado y la conducta observada en secciones
    # distintas, dejando el contraste a cargo del lector.
    veredictos = [v for v in (analisis.get("perfiles") or [])
                  if isinstance(v, dict) and v.get("quien")]
    # La medición de si el perfil llegó a la acción va antes que el veredicto:
    # decide cómo leerlo. Un «no se observa» sobre un perfil que nunca se
    # recuperó no es un resultado ambiguo, está explicado.
    llegada = llegada_del_perfil(pasos, esc)
    if veredictos or llegada:
        partes.append('<section id="perfiles"><h2>¿Se notó el perfil configurado?</h2>')
    if llegada:
        partes.append(llegada)
    if veredictos:
        partes.append(f'<p class="ayuda-sec">{marca("inferido")} Contraste entre el perfil '
                      "psicológico que se le cargó a cada actor y lo que efectivamente "
                      "hizo. Que un perfil no se note es un resultado, no una falla del "
                      "análisis: los componentes influyen en la conducta, no la determinan, "
                      "y en los tipos que no son «Mínimo» compiten con el resto de la "
                      "memoria por entrar en cada acción.</p>")
        etiquetas = {"se manifiesta": ("v-si", "Se nota"),
                     "no se observa": ("v-no", "No se observa"),
                     "contradice": ("v-contra", "Contradice")}
        partes.append('<ul class="veredictos">')
        for v in veredictos:
            clase, rotulo = etiquetas.get(str(v.get("veredicto", "")).strip().lower(),
                                          ("v-no", str(v.get("veredicto") or "—")))
            partes.append(f'<li><span class="chapa-v {clase}">{html.escape(rotulo)}</span>'
                          f'<div><p class="quien-v">{html.escape(str(v["quien"]))}</p>'
                          f'<p class="detalle-v">{html.escape(str(v.get("detalle") or ""))}</p>'
                          "</div></li>")
        partes.append("</ul>")
    if veredictos or llegada:
        partes.append("</section>")

    # ------------------------------------------------------- 6. evolución
    if series or franjas:
        partes.append('<section id="evolucion"><h2>Cómo evolucionó</h2>')
        partes.append(f'<p class="ayuda-sec">{marca("estimado")} Los valores los asigna el narrador '
                      "interpretando lo que se dijo; no son mediciones de nada observado. "
                      "Las líneas punteadas marcan los hitos críticos previstos.</p>")
        if series:
            hitos = [d.get("step") for d in (decisiones or []) if d.get("step")]
            partes.append('<div class="grafico">' + grafico(series, max_paso, hitos) + "</div>")
        if franjas:
            partes.append('<div class="grafico franjas">')
            for nombre, serie in sorted(franjas.items()):
                partes.append(franja(nombre, serie, max_paso))
            partes.append("</div>")
        partes.append("</section>")

    # --------------------------------------------------- 7. actores
    # Un solo lugar por actor. Estaba partido en dos: nombre, turnos y
    # objetivo acá, y perfil y recuerdos en la ficha técnica dentro de un
    # plegado, en otra pestaña. Para saber cómo estaba configurado alguien había
    # que juntar dos secciones que ni se mencionaban entre sí.
    por_nombre = {a.get("name"): a for a in (esc.get("agentes") or [])}
    partes.append('<section id="actores"><h2>Quiénes participaron</h2>')
    partes.append(f'<p class="ayuda-sec">{marca("medido")} Cómo estaba configurado cada uno: '
                  "qué busca, con qué perfil y con qué recuerdos entró.</p>")
    indirectos = [a for a in por_nombre.values()
                  if perfil_psicologico(a.get("components") or {})
                  and a.get("prefab") != "minimal__Entity"]
    if indirectos:
        partes.append('<p class="ayuda-sec">Los perfiles de '
                      f"{len(indirectos)} actores entraron como recuerdo, no como "
                      "componente fijo: solo el tipo «Mínimo» los tiene presentes en cada "
                      "acción. En los demás compiten con el resto de la memoria por ser "
                      "recuperados, así que pueden no pesar en todos los turnos.</p>")

    # La comparación primero: sin ella hay que leer cinco fichas de prosa y
    # armar de memoria dónde chocan.
    partes.append(tabla_actores(esc, veces))

    for q in quienes:
        a = por_nombre.get(q, {})
        partes.append('<article class="actor">')
        partes.append(f"<h3>{html.escape(q)}</h3>")
        partes.append(f'<p class="veces">{veces[q]} '
                      f'{"turno" if veces[q] == 1 else "turnos"}</p>')
        meta = a.get("goal") or objetivos.get(q)
        if meta:
            partes.append(f'<p class="objetivo">{html.escape(meta)}</p>')
        perfil = perfil_psicologico(a.get("components") or {})
        if perfil:
            partes.append(perfil)
        recuerdos = a.get("memories") or []
        if recuerdos:
            partes.append('<details class="interno"><summary>'
                          f"Con qué {len(recuerdos)} recuerdos entró</summary>"
                          "<ul class='lista-datos'>")
            for m in recuerdos:
                partes.append(f"<li>{html.escape(m)}</li>")
            partes.append("</ul></details>")
        partes.append("</article>")
    # Los que estaban configurados y nunca hablaron: sin esto, un actor
    # que la corrida no alcanzó a darle la palabra desaparece del informe.
    callados = [n for n in por_nombre if n not in quienes]
    if callados:
        partes.append('<p class="franja-cambios">Configurados pero sin intervenir: '
                      + html.escape(", ".join(callados)) + ".</p>")
    partes.append("</section>")

    # --------------------------------------------------- 8. la deliberación
    partes.append('<section id="ciclo"><h2>Cómo transcurre un turno</h2>')
    partes.append('<p class="ayuda-sec">La discusión no es una charla libre: cada turno sigue '
                  'siempre la misma secuencia, y quién puede introducir un hecho en el mundo '
                  'depende de ella.</p>')
    partes.append(ciclo(pasos, esc.get("orden") or ""))
    partes.append("</section>")
    partes.append(reparto_de_la_palabra(pasos, esc))

    partes.append('<section id="deliberacion"><h2>La deliberación</h2>')
    partes.append('<p class="ayuda-sec">Cada turno se abre y muestra la secuencia completa: '
                  "qué se le preguntó, qué respondió y qué se enteraron los demás. "
                  "Los textos son literales, sin resumir.</p>")
    # Cada etapa dice de quién es la mano. La distinción no es decorativa: de
    # las seis, cuatro son del narrador, y son las únicas que pueden meter un
    # hecho nuevo en el mundo. Sin la marca hay que acordarse de cuál era cuál.
    partes.append('<p class="ayuda-sec">Cada etapa lleva de quién es: '
                  '<span class="autor a-n">narrador</span> o '
                  '<span class="autor a-p">actor</span>. Cuatro de las seis son del '
                  "narrador, y son las únicas que pueden introducir un hecho en el mundo: lo "
                  "que dice un actor recién existe cuando el narrador lo registra.</p>")
    # El reparto de turnos encabeza la transcripcion, no la configuracion: es lo
    # que efectivamente paso, y sirve de mapa de lo que se va a leer.

    # Leer la deliberación entera obligaba a abrir turno por turno. El botón se
    # inserta desde el script para que no aparezca muerto donde no haya JS: sin
    # él, los turnos siguen abriéndose de a uno como hasta ahora.
    partes.append('<div id="control-turnos"></div>')
    for p in pasos:
        hito = por_paso.get(p["n"])
        if hito:
            partes.append('<div class="hito"><span class="hito-rotulo">Hito crítico '
                          f'· turno {p["n"]}</span><p>{html.escape(hito)}</p></div>')
        dicho = p["dicho"] or p["evento"]
        adelanto = re.sub(r"\s+", " ", re.sub(r"\*\*|__", "", dicho))[:150]
        partes.append('<details class="turno">')
        partes.append('<summary><span class="paso">' + str(p["n"]) + "</span>"
                      f'<span class="orador">{html.escape(p["quien"])}</span>'
                      f'<span class="adelanto">{html.escape(adelanto)}…</span></summary>')
        mesa = p.get("mesa") or {}
        partes.append('<div class="cuerpo-turno">')

        if mesa.get("consigna"):
            partes.append('<div class="etapa-turno"><span class="et">Se le preguntó<span class="autor a-n">narrador</span></span>'
                          f'<p class="consigna">{html.escape(mesa["consigna"])}</p></div>')

        partes.append('<div class="etapa-turno"><span class="et">Respondió<span class="autor a-p">actor</span></span>'
                      f'<div class="dicho">{parrafos(dicho)}</div></div>')

        # Si el hecho registrado difiere de lo dicho, el narrador intervino y
        # conviene poder verlo; si es igual, decirlo evita repetir el texto.
        if p["evento"] and p["dicho"]:
            if parecido(p["dicho"], p["evento"]) > 0.95:
                partes.append('<div class="etapa-turno"><span class="et">Quedó registrado<span class="autor a-n">narrador</span></span>'
                              '<p class="igual">Tal cual, sin cambios del narrador.</p></div>')
            else:
                partes.append('<div class="etapa-turno"><span class="et">Quedó registrado<span class="autor a-n">narrador</span></span>'
                              f'<div class="dicho">{parrafos(p["evento"])}</div></div>')

        obs = mesa.get("observaciones") or {}
        if obs:
            partes.append('<details class="interno"><summary><span class="autor a-n">narrador</span>Qué se enteró cada uno '
                          f'({len(obs)} actores)</summary><div>')
            for quien, texto in obs.items():
                partes.append(f'<p class="quien-obs">{html.escape(quien)}</p>')
                partes.append(f'<div class="obs">{parrafos(texto)}</div>')
            partes.append("</div></details>")

        # Las tres preguntas que se hace antes de actuar, en el orden en que
        # Concordia se las plantea: quién soy, dónde estoy, qué haría alguien así.
        tres = [("Qué clase de persona es", p["persona"], "SelfPerception"),
                ("En qué situación se ve", p["situacion"], "SituationPerception"),
                ("Qué haría alguien como él o ella", p["haria"], "PersonBySituation")]
        if any(t[1] for t in tres):
            pasos_cot = sum(len(p["razonamiento"].get(c) or []) for _, _, c in tres)
            partes.append('<details class="interno"><summary><span class="autor a-p">actor</span>Cómo razonó antes de hablar'
                          + (f" ({pasos_cot} pasos)" if pasos_cot else "")
                          + "</summary><div>")
            for titulo, texto, campo in tres:
                if not texto:
                    continue
                partes.append(f'<p class="quien-obs">{titulo}</p>')
                partes.append(f'<div class="obs">{parrafos(texto)}</div>')
                cot = p["razonamiento"].get(campo) or []
                if cot:
                    partes.append('<details class="cot"><summary>Los '
                                  f'{len(cot)} pasos de ese razonamiento</summary><ol>')
                    for x in cot:
                        t = re.sub(r"\s+", " ", str(x)).strip()
                        if t:
                            partes.append(f"<li>{html.escape(t[:600])}</li>")
                    partes.append("</ol></details>")
            partes.append("</div></details>")

        mem = p.get("memoria") or {}
        if mem.get("recuerdos"):
            partes.append('<details class="interno"><summary><span class="autor a-p">actor</span>Qué recordó '
                          f'({len(mem["recuerdos"])} recuerdos)</summary><div>')
            if mem.get("consulta"):
                partes.append('<p class="quien-obs">Buscó en su memoria con esto</p>')
                partes.append(f'<p class="obs">{html.escape(mem["consulta"][:400])}</p>')
            partes.append('<p class="quien-obs">Y le vino a la mente</p><ul class="recuerdos">')
            for r in mem["recuerdos"][:12]:
                partes.append(f"<li>{html.escape(r[:400])}</li>")
            partes.append("</ul>")
            if len(mem["recuerdos"]) > 12:
                partes.append(f'<p class="igual">Y {len(mem["recuerdos"]) - 12} recuerdos más.</p>')
            partes.append("</div></details>")

        partes.append("</div></details>")
    partes.append("</section>")

    # ------------------------------------------------------ 9. ficha técnica
    partes.append('<section id="tecnica"><h2>Ficha técnica</h2>')
    ficha_esc = seccion_escenario(esc, quienes)
    if ficha_esc:
        partes.append(ficha_esc)

    # Cuánto costó, en pedidos al modelo. Es lo que permite dimensionar la
    # próxima corrida por división en vez de descubrir el techo a mitad de
    # camino, que es como se cortaron las tres primeras.
    consumo = resumen.get("consumo") or {}
    if consumo:
        por_paso = resumen.get("consumo_por_paso") or {}
        hechos = resumen.get("pasos_completados") or len(pasos)
        partes.append('<details class="escenario"><summary>Cuánto consumió</summary>'
                      "<div class='cuerpo-esc'>")
        partes.append('<p class="ayuda-sec">El plan gratuito permite 500 pedidos '
                      "diarios <em>por modelo</em>. Usar un modelo distinto para el "
                      "narrador duplica el techo, porque cada uno lleva su propia "
                      "cuenta.</p>")
        partes.append("<table class='config'><tbody>")
        for m, d in sorted(consumo.items()):
            pp = por_paso.get(m)
            detalle = f"{d['llamadas']} pedidos"
            # Los tokens van primero cuando los hay: es el techo que primero se
            # toca. Una corrida murió con 98.365 de 100.000 tokens diarios
            # gastados en un solo turno, mientras los pedidos ni se acercaban al
            # suyo.
            if d.get("tokens"):
                por_turno = d["tokens"] / hechos if hechos else 0
                detalle += f" · {d['tokens']:,} tokens".replace(",", ".")
                if por_turno:
                    detalle += f" ({por_turno:,.0f} por turno)".replace(",", ".")
            if pp:
                detalle += f" · {pp} por turno"
            if d.get("esperas"):
                detalle += (f" · {d['esperas']} esperas por el límite por minuto "
                            f"({d['segundos_esperando'] // 60} min parado)")
            partes.append(f"<tr><th>{html.escape(m)}</th><td>{detalle}</td></tr>")
        partes.append("</tbody></table>")
        techo = min((int(500 // float(p)) for p in por_paso.values() if p), default=0)
        if techo and hechos:
            partes.append(f'<p class="franja-cambios">A este ritmo entran unos '
                          f"<strong>{techo} turnos por día</strong> con la cuota "
                          "gratuita. Este escenario pidió "
                          f"{esc.get('pasos') or hechos}.</p>")
        partes.append("</div></details>")

    consignas = [p["mesa"]["consigna"] for p in pasos if p.get("mesa", {}).get("consigna")]
    if consignas:
        distintas = []
        for c in consignas:
            if c not in distintas:
                distintas.append(c)
        partes.append('<details class="escenario"><summary>Con qué consigna se dio la palabra'
                      "</summary><div class='cuerpo-esc'>")
        partes.append('<p class="ayuda-sec">La consigna es la pregunta que el narrador le hace a '
                      "quien va a hablar. Cuando no cambia, todos responden al mismo estímulo. "
                      "Van en el idioma en que las genera el motor.</p>")
        if len(distintas) == 1:
            partes.append(f'<p><strong>Una sola consigna para los {len(consignas)} '
                          "turnos.</strong></p>")
        else:
            partes.append(f"<p><strong>{len(distintas)} consignas distintas</strong> en "
                          f"{len(consignas)} turnos.</p>")
        partes.append('<ul class="consignas">')
        for c in distintas[:8]:
            partes.append(f'<li><span class="cuantas">{consignas.count(c)}×</span>'
                          f"<span>{html.escape(c)}</span></li>")
        partes.append("</ul>")
        if len(distintas) > 8:
            partes.append(f'<p class="franja-cambios">Y {len(distintas) - 8} más, distintas '
                          "entre sí.</p>")
        cierres = [p["mesa"]["termina"] for p in pasos if p.get("mesa", {}).get("termina")]
        if cierres:
            quiso = sum(1 for c in cierres if c.strip().lower().startswith(("s", "y")))
            partes.append(f'<p class="franja-cambios">Se preguntó en {len(cierres)} turnos si '
                          "la deliberación había terminado. "
                          + (f"En {quiso} respondió que sí." if quiso
                             else "Siempre respondió que no.") + "</p>")
        partes.append("</div></details>")

    partes.append('<details class="escenario"><summary>Cómo leer este informe</summary>'
                  "<div class='cuerpo-esc'><ul class='lista-datos'>"
                  f"<li>{marca('medido')} sale del registro de la corrida: turnos, quién habló, "
                  "cuándo se cortó.</li>"
                  f"<li>{marca('estimado')} lo asignó el narrador interpretando la deliberación. "
                  "Son juicios de un modelo, no mediciones.</li>"
                  f"<li>{marca('inferido')} lo dedujo un modelo leyendo la transcripción "
                  "después de terminada la corrida.</li></ul></div></details>")
    partes.append("</section>")

    partes.append('<footer class="pie">Generado a partir del registro de la simulación. '
                  "La transcripción reproduce lo que dijo cada actor, sin resumir.</footer>")
    partes.append("""<script>
(function(){
  var tabs = [].slice.call(document.querySelectorAll('.pestana'));
  var grupos = [].slice.call(document.querySelectorAll('.grupo'));
  if (tabs.length < 2 || !grupos.length) return;

  function mostrar(gid, mover){
    grupos.forEach(function(g){ g.hidden = (g.id !== gid); });
    tabs.forEach(function(t){
      var act = t.getAttribute('data-grupo') === gid;
      t.setAttribute('aria-selected', act ? 'true' : 'false');
      t.tabIndex = act ? 0 : -1;
      t.classList.toggle('activa', act);
    });
    if (mover) window.scrollTo(0, 0);
  }

  // Un enlace a #deliberacion tiene que abrir su pestaña, no dejar la pagina
  // en una seccion oculta.
  function grupoDe(id){
    var s = id && document.getElementById(id);
    var g = s && s.closest('.grupo');
    return g ? g.id : null;
  }

  tabs.forEach(function(t, i){
    t.addEventListener('click', function(e){
      e.preventDefault();
      mostrar(t.getAttribute('data-grupo'), true);
      history.replaceState(null, '', t.getAttribute('href'));
    });
    t.addEventListener('keydown', function(e){
      var d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      var n = tabs[(i + d + tabs.length) % tabs.length];
      n.focus(); n.click();
    });
  });

  // Enlaces internos que apuntan a una seccion de otra pestaña.
  document.addEventListener('click', function(e){
    var a = e.target.closest && e.target.closest('a[href^="#"]');
    if (!a || a.classList.contains('pestana')) return;
    var g = grupoDe(a.getAttribute('href').slice(1));
    if (g) mostrar(g, false);
  });

  var inicial = grupoDe(location.hash.slice(1)) || grupos[0].id;
  mostrar(inicial, false);
  if (location.hash) {
    var destino = document.getElementById(location.hash.slice(1));
    if (destino) destino.scrollIntoView();
  }
})();
</script>
<script>
(function(){
  var caja = document.getElementById('control-turnos');
  var turnos = document.querySelectorAll('details.turno');
  if (!caja || !turnos.length) return;
  var b = document.createElement('button');
  b.className = 'b-turnos';
  b.type = 'button';
  var abiertos = false;
  function rotular(){
    b.textContent = abiertos ? 'Cerrar los ' + turnos.length + ' turnos'
                             : 'Abrir los ' + turnos.length + ' turnos';
  }
  b.addEventListener('click', function(){
    abiertos = !abiertos;
    for (var i = 0; i < turnos.length; i++) turnos[i].open = abiertos;
    rotular();
  });
  rotular();
  caja.appendChild(b);
})();
</script>""")
    partes.append("</main></body></html>")
    return armar_pestanas("".join(partes))


CABEZA = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Informe de simulación deliberativa</title>
<style>
:root{--ground:#f7f7f5;--surface:#fff;--surface-alt:#f1f2ef;--ink:#17191c;--ink-soft:#5b6168;
--ink-faint:#878d94;--rule:#dfe0dc;--rule-strong:#c9cbc5;
--acuerdo:#2d5f5d;--pendiente:#8a6a1f;--bloqueo:#8f3a2f;--estimado:#6b4a7a;--evento:#3d6b8a;
--acuerdo-suave:#e4eeed;--pendiente-suave:#f8f1de;--bloqueo-suave:#f7e8e5;
--serif:Georgia,"Iowan Old Style","Times New Roman",serif;
--sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--ground:#131619;--surface:#1a1e22;--surface-alt:#22272c;--ink:#e9ecee;--ink-soft:#9aa2aa;
--ink-faint:#757d85;--rule:#2b3137;--rule-strong:#3b434a;
--acuerdo:#6fb3ae;--pendiente:#d3a84e;--bloqueo:#d98070;--estimado:#b394c4;--evento:#7aa8c8;
--acuerdo-suave:#1d2e2e;--pendiente-suave:#2c2617;--bloqueo-suave:#2e1f1c}}
:root[data-theme="dark"]{--ground:#131619;--surface:#1a1e22;--surface-alt:#22272c;--ink:#e9ecee;
--ink-soft:#9aa2aa;--ink-faint:#757d85;--rule:#2b3137;--rule-strong:#3b434a;
--acuerdo:#6fb3ae;--pendiente:#d3a84e;--bloqueo:#d98070;--estimado:#b394c4;--evento:#7aa8c8;
--acuerdo-suave:#1d2e2e;--pendiente-suave:#2c2617;--bloqueo-suave:#2e1f1c}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
font-size:15.5px;line-height:1.6}
.ancho{max-width:70rem;margin:0 auto;padding:0 1.5rem}
main.ancho{padding-bottom:5rem}

.tapa{background:var(--surface);border-bottom:1px solid var(--rule);padding:2.5rem 0 0}
.marca-doc{font-family:var(--mono);font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;
color:var(--acuerdo);margin:0 0 .7rem}
h1{font-family:var(--serif);font-weight:400;font-size:2.1rem;line-height:1.15;margin:0 0 .6rem;
letter-spacing:-.01em;text-wrap:balance;max-width:36rem}
.linea-datos{margin:0 0 .9rem;color:var(--ink-soft);font-size:.93rem;font-variant-numeric:tabular-nums}
.chapa{display:inline-block;margin:0 0 1.5rem;padding:.3rem .8rem;border-radius:99px;
font-size:.82rem;font-weight:600}
.chapa.ok{background:var(--acuerdo-suave);color:var(--acuerdo)}
.chapa.aviso{background:var(--pendiente-suave);color:var(--pendiente)}
/* El índice queda fijo arriba: el informe pasa de las 400 KB y casi todo es la
   deliberación, así que uno termina lejos del encabezado y sin forma de volver.
   Se desplaza a lo ancho en pantallas angostas en vez de partirse en dos filas
   que empujan el contenido. */
.pestanas{position:sticky;top:0;z-index:20;background:var(--ground);
border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)}
.barra{display:flex;gap:.35rem;overflow-x:auto;scrollbar-width:thin}
.pestana{color:var(--ink-soft);text-decoration:none;font-size:.9rem;white-space:nowrap;
padding:.85rem .95rem;border-bottom:2px solid transparent;margin-bottom:-1px}
.pestana:hover{color:var(--ink)}
/* Sin JavaScript ninguna queda marcada y todas las secciones se ven de corrido,
   que es un resultado correcto: la marca es del script, no del documento. */
.pestana.activa{color:var(--ink);border-bottom-color:var(--acuerdo);font-weight:600}
.pestana:focus-visible{outline:2px solid var(--acuerdo);outline-offset:-2px}
.grupo>section:first-child{padding-top:.4rem}
/* Sin esto el índice fijo tapa el título de la sección a la que se saltó. */
section[id]{scroll-margin-top:3.5rem}
.variaciones{margin:0;padding-left:1.4rem;display:flex;flex-direction:column;gap:1rem;
max-width:52rem}
.variaciones li{padding-left:.3rem}
.v-cambio{margin:0 0 .4rem;font-size:.93rem;font-weight:600}
.v-linea{margin:0 0 .25rem;font-size:.88rem;color:var(--ink-soft);line-height:1.55}
.et-v{display:inline-block;min-width:8.5rem;font-size:.72rem;letter-spacing:.05em;
text-transform:uppercase;font-family:var(--mono);color:var(--ink-faint)}
.ajustes{max-width:52rem}
#hallazgos .tarjetas{grid-template-columns:repeat(auto-fit,minmax(20rem,1fr))}
#hallazgos .tarjeta h3{margin:0 0 .5rem;font-size:.97rem;font-weight:600}
#hallazgos .tarjeta p{margin:0;font-size:.9rem;line-height:1.6;color:var(--ink-soft)}
.ficha-corrida{max-width:34rem}
.tabla-vars{width:100%}
.tabla-vars thead th{font-size:.74rem;letter-spacing:.05em;text-transform:uppercase;
font-family:var(--mono);color:var(--ink-faint);border-bottom:1px solid var(--rule-strong);
padding-bottom:.4rem;text-align:left}
.tabla-vars tbody th{width:11rem;font-weight:600;color:var(--ink);font-family:var(--sans);
font-size:.88rem}
.tabla-vars td{font-family:var(--sans);font-size:.85rem;color:var(--ink-soft);
line-height:1.5;padding:.5rem .8rem .5rem 0;vertical-align:top}
.encuadre{margin:0 0 1.2rem;font-size:.98rem;line-height:1.65;max-width:46rem;
padding-left:.9rem;border-left:3px solid var(--acuerdo)}
.hitos{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:.65rem;
max-width:52rem;counter-reset:h}
.hitos li{display:flex;gap:.85rem;align-items:flex-start;background:var(--surface);
border:1px solid var(--rule);border-radius:8px;padding:.75rem .9rem}
.hitos li.no-alcanzado{opacity:.62}
.turno-hito{flex:none;font-size:.74rem;font-family:var(--mono);color:var(--ink-faint);
background:var(--surface-alt);border-radius:4px;padding:.2rem .45rem;white-space:nowrap}
.titulo-hito{margin:0 0 .25rem;font-size:.9rem;font-weight:600}
.cuerpo-hito{margin:0;font-size:.87rem;color:var(--ink-soft);line-height:1.55}
.archivos a{color:var(--acuerdo);text-decoration:none;font-family:var(--mono);font-size:.85rem}
.archivos a:hover{text-decoration:underline}
.nota-datos{margin:.3rem 0 .8rem;font-size:.89rem;color:var(--ink-soft);line-height:1.6;
max-width:46rem}
.produjo{list-style:none;margin:.4rem 0 1rem;padding:0;display:flex;flex-direction:column;
gap:.55rem;max-width:52rem}
.produjo li{font-size:.9rem;line-height:1.6;color:var(--ink-soft);padding-left:.9rem;
border-left:2px solid var(--rule-strong)}
.donde{color:var(--ink-faint);font-size:.85rem}
.tabla-actores{width:100%;margin-bottom:.5rem}
.tabla-actores thead th{font-size:.72rem;letter-spacing:.05em;text-transform:uppercase;
font-family:var(--mono);color:var(--ink-faint);border-bottom:1px solid var(--rule-strong);
padding-bottom:.4rem;text-align:left}
.tabla-actores tbody th{width:11rem;font-weight:600;color:var(--ink);font-family:var(--sans);
font-size:.87rem;vertical-align:top}
.tabla-actores td{font-family:var(--sans);font-size:.84rem;color:var(--ink-soft);
line-height:1.5;padding:.5rem .8rem .5rem 0;vertical-align:top}
.nota-fuente{margin:.2rem 0 1.4rem;font-size:.83rem;color:var(--ink-faint);line-height:1.55;
max-width:52rem}
.autor{display:inline-block;margin-left:.45rem;font-size:.66rem;letter-spacing:.05em;
text-transform:uppercase;font-family:var(--mono);border-radius:3px;padding:.1rem .32rem;
vertical-align:middle;font-weight:500}
.a-n{background:var(--pendiente-suave);color:var(--pendiente)}
.a-p{background:var(--acuerdo-suave);color:var(--acuerdo)}
summary>.autor{margin-left:0;margin-right:.5rem}
.controles{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:.6rem;
max-width:52rem}
.controles li{display:flex;gap:.8rem;align-items:flex-start;background:var(--surface);
border:1px solid var(--rule);border-radius:8px;padding:.75rem .9rem}
.c-marca{flex:none;width:1.35rem;height:1.35rem;border-radius:50%;display:grid;
place-items:center;font-size:.8rem;font-weight:700}
.c-ok .c-marca{background:var(--acuerdo-suave);color:var(--acuerdo)}
.c-aviso .c-marca{background:var(--pendiente-suave);color:var(--pendiente)}
.c-titulo{margin:0 0 .2rem;font-size:.9rem;font-weight:600}
.c-detalle{margin:0;font-size:.88rem;color:var(--ink-soft);line-height:1.55}
.veredictos{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:.6rem;
max-width:52rem}
.veredictos li{display:flex;gap:.85rem;align-items:flex-start;background:var(--surface);
border:1px solid var(--rule);border-radius:8px;padding:.75rem .9rem}
.chapa-v{flex:none;font-size:.74rem;letter-spacing:.04em;text-transform:uppercase;
font-family:var(--mono);border-radius:4px;padding:.2rem .45rem;white-space:nowrap}
.v-si{background:var(--acuerdo-suave);color:var(--acuerdo)}
.v-no{background:var(--surface-alt);color:var(--ink-faint)}
.v-contra{background:var(--pendiente-suave);color:var(--pendiente)}
.quien-v{margin:0 0 .2rem;font-size:.9rem;font-weight:600}
.detalle-v{margin:0;font-size:.88rem;color:var(--ink-soft);line-height:1.55}
.b-turnos{font:inherit;font-size:.86rem;color:var(--ink-soft);background:var(--surface);
border:1px solid var(--rule-strong);border-radius:6px;padding:.42rem .85rem;
cursor:pointer;margin-bottom:1rem}
.b-turnos:hover{color:var(--acuerdo);border-color:var(--acuerdo)}

section{margin-top:3rem}
h2{font-family:var(--serif);font-weight:400;font-size:1.4rem;margin:0 0 1rem;
padding-bottom:.5rem;border-bottom:1px solid var(--rule)}
h3{font-size:1rem;margin:0 0 .25rem}
.ayuda-sec{margin:0 0 1.2rem;color:var(--ink-soft);font-size:.92rem;max-width:52rem}

.proc{font-family:var(--mono);font-size:.68rem;letter-spacing:.06em;text-transform:uppercase;
padding:.1rem .4rem;border-radius:3px;white-space:nowrap}
.p-medido{background:var(--acuerdo-suave);color:var(--acuerdo)}
.p-estimado{background:transparent;color:var(--estimado);border:1px solid var(--estimado)}
.p-inferido{background:transparent;color:var(--ink-faint);border:1px dashed var(--rule-strong)}

.tarjetas{display:grid;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr));gap:1rem}
.tarjeta{background:var(--surface);border:1px solid var(--rule);border-radius:9px;padding:1.1rem 1.25rem}
.t-rotulo{margin:0 0 .45rem;font-size:.78rem;letter-spacing:.07em;text-transform:uppercase;
color:var(--ink-faint);font-family:var(--mono)}
.t-valor{margin:0;font-size:1.6rem;line-height:1.2;font-variant-numeric:tabular-nums}
.t-valor.chico{font-size:1.05rem;line-height:1.4}
.t-sobre{font-size:.9rem;color:var(--ink-faint)}
.aviso-txt{color:var(--pendiente)}
/* Las tarjetas de interpretación llevan párrafos, no cifras: necesitan más
   ancho que las de resultado o el texto queda en columnas de cinco palabras. */
#interpretacion .tarjetas{grid-template-columns:repeat(auto-fit,minmax(20rem,1fr))}
#interpretacion .tarjeta h3{margin:0 0 .5rem;font-size:.97rem;font-weight:600}
#interpretacion .tarjeta p{margin:0;font-size:.9rem;line-height:1.6;color:var(--ink-soft)}
#interpretacion .tarjeta.aviso{border-color:var(--pendiente)}
#interpretacion .tarjeta.aviso h3{color:var(--pendiente)}
.t-nota{margin:.35rem 0 0;font-size:.85rem;color:var(--ink-soft)}
.t-pie{margin:.7rem 0 0}
.sin-medir{margin:1.1rem 0 0;padding:.85rem 1.05rem;background:var(--surface-alt);
border-radius:7px;font-size:.9rem;color:var(--ink-soft);max-width:52rem}

.incompleta{background:var(--pendiente-suave);border:1px solid var(--pendiente);
border-radius:9px;padding:1.3rem 1.5rem}
.incompleta h2{border:none;padding:0;margin:0 0 .7rem;font-size:1.25rem;color:var(--pendiente)}
.incompleta p{margin:0 0 .7rem;font-size:.93rem;max-width:52rem}
.incompleta ul{margin:.3rem 0 .8rem;padding-left:1.2rem;font-size:.9rem;max-width:52rem}
.incompleta li{margin-bottom:.4rem}
.incompleta .motivo{color:var(--ink-soft);font-size:.89rem}

.senales ul{list-style:none;margin:0;padding:0;display:grid;
grid-template-columns:repeat(auto-fit,minmax(21rem,1fr));gap:.7rem}
.senales li{background:var(--surface);border:1px solid var(--rule);
border-left:3px solid var(--rule-strong);border-radius:0 7px 7px 0;padding:.85rem 1.05rem;font-size:.92rem}
.s-alerta{border-left-color:var(--bloqueo)!important}
.s-buena{border-left-color:var(--acuerdo)!important}
.s-neutra{border-left-color:var(--ink-faint)!important}

.resumen{background:var(--surface);border:1px solid var(--rule);border-left:3px solid var(--estimado);
border-radius:0 9px 9px 0;padding:1.3rem 1.6rem}
.resumen h2{border:none;padding:0;margin:0 0 .5rem;font-size:1.25rem}
.resumen p{margin:0 0 .85rem;max-width:44rem}
.resumen p:last-child{margin-bottom:0}
.marca-ia{margin:0 0 .9rem!important}

.grafico{background:var(--surface);border:1px solid var(--rule);border-radius:9px;
padding:1.2rem 1.3rem;overflow-x:auto}
.franjas{margin-top:1rem;display:flex;flex-direction:column;gap:1.3rem}
svg{display:block;width:100%;min-width:32rem;height:auto}
.rejilla{stroke:var(--rule);stroke-width:1}
.eje-y,.eje-x{fill:var(--ink-faint);font-size:11px;font-family:var(--mono)}
.eje-y{text-anchor:end}.eje-x{text-anchor:middle}
.hito-linea{stroke:var(--evento);stroke-width:1;stroke-dasharray:3 3;opacity:.85}
.hito-marca{fill:var(--evento);font-size:9px;font-family:var(--mono);text-anchor:middle}
.leyenda{list-style:none;margin:1.1rem 0 0;padding:0;display:flex;flex-direction:column;gap:.42rem}
.leyenda li{display:flex;align-items:center;gap:.6rem;font-size:.88rem}
.punto{width:.7rem;height:.7rem;border-radius:2px;flex:none}
.leyenda-nombre{flex:1;min-width:0}
.leyenda-dato{font-family:var(--mono);color:var(--ink-soft);font-variant-numeric:tabular-nums}
.franja-nombre{font-size:.86rem;font-weight:600;margin-bottom:.45rem}
.franja-barra{display:flex;gap:2px;border-radius:5px;overflow:hidden}
.tramo{min-width:0;padding:.5rem .6rem;color:#fff;font-size:.78rem;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
.franja-cambios{margin:.45rem 0 0;font-size:.84rem;color:var(--ink-soft)}

.tira{display:flex;flex-wrap:wrap;gap:3px;margin-bottom:1rem}
.celda{width:2rem;height:2rem;border-radius:4px;color:#fff;font-size:.72rem;font-family:var(--mono);
display:flex;align-items:center;justify-content:center}
.leyenda-tira{margin-bottom:1.8rem}
.actor{padding:.9rem 0;border-top:1px solid var(--rule);max-width:52rem}
.veces{margin:0;font-size:.82rem;color:var(--ink-faint);font-family:var(--mono)}
.objetivo{margin:.5rem 0 0;color:var(--ink-soft);font-size:.94rem}

.turno{background:var(--surface);border:1px solid var(--rule);border-radius:8px;margin-bottom:.6rem}
.turno>summary{cursor:pointer;padding:.85rem 1.1rem;display:flex;align-items:center;gap:.75rem;
list-style:none}
.turno>summary::-webkit-details-marker{display:none}
.turno>summary:hover .orador{color:var(--acuerdo)}
.paso{font-family:var(--mono);font-size:.75rem;color:var(--acuerdo);border:1px solid var(--rule-strong);
border-radius:3px;padding:.08rem .4rem;flex:none}
.orador{font-weight:600;font-size:.9rem;flex:none}
.adelanto{color:var(--ink-faint);font-size:.86rem;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;min-width:0}
.cuerpo-turno{padding:0 1.2rem 1.2rem;border-top:1px solid var(--rule)}
.dicho{max-width:44rem;padding-top:1rem}
.dicho p{margin:0 0 .8rem}.dicho p:last-child{margin-bottom:0}
.vacio{color:var(--ink-faint)}
.interno{margin-top:1rem;border-top:1px solid var(--rule);padding-top:.7rem;max-width:44rem}
.interno summary{cursor:pointer;font-size:.82rem;color:var(--ink-faint);font-family:var(--mono)}
.interno summary:hover{color:var(--acuerdo)}
.interno>div{margin-top:.6rem;font-size:.92rem;color:var(--ink-soft)}
.hito{border-left:3px solid var(--evento);background:var(--surface-alt);padding:.85rem 1.1rem;
border-radius:0 7px 7px 0;margin:1.8rem 0 .9rem;max-width:52rem}
.hito-rotulo{font-family:var(--mono);font-size:.68rem;letter-spacing:.1em;text-transform:uppercase;
color:var(--evento)}
.hito p{margin:.4rem 0 0;font-size:.93rem}

.escenario{border:1px solid var(--rule);border-radius:9px;background:var(--surface);margin-bottom:.8rem}
.escenario>summary{cursor:pointer;padding:.9rem 1.2rem;font-weight:600;font-size:.92rem;
list-style:none;display:flex;justify-content:space-between;align-items:center;gap:1rem}
.escenario>summary::-webkit-details-marker{display:none}
.escenario>summary::after{content:"+";font-family:var(--mono);color:var(--acuerdo);font-size:1.1rem}
.escenario[open]>summary::after{content:"–"}
.escenario>summary:hover{color:var(--acuerdo)}
.cuerpo-esc{padding:0 1.2rem 1.4rem;border-top:1px solid var(--rule);max-width:52rem}
.cuerpo-esc h3{font-family:var(--serif);font-weight:400;font-size:1.1rem;margin:1.5rem 0 .6rem}
.cuerpo-esc h4{font-size:.95rem;margin:0 0 .3rem}
.lista-datos{margin:.4rem 0;padding-left:1.15rem;font-size:.9rem;color:var(--ink-soft)}
.lista-datos li{margin-bottom:.45rem}
.ficha-agente{padding:.85rem 0;border-top:1px solid var(--rule)}
.obj-agente{margin:0;font-size:.9rem;color:var(--ink-soft)}
.perfil{border-collapse:collapse;margin:.5rem 0 .2rem;font-size:.85rem;
background:var(--surface-alt);border-radius:6px}
.perfil th{text-align:left;font-weight:500;color:var(--ink-faint);
padding:.3rem .8rem .3rem .7rem;white-space:nowrap;vertical-align:top}
.perfil td{padding:.3rem .7rem .3rem 0;color:var(--ink-soft)}
.config{border-collapse:collapse;width:100%;font-size:.89rem;margin-top:.4rem}
.config th{text-align:left;font-weight:500;color:var(--ink-faint);padding:.35rem .8rem .35rem 0;
white-space:nowrap;vertical-align:top}
.config td{padding:.35rem 0;font-family:var(--mono);font-size:.85rem}
.consignas{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:.5rem}
.consignas li{display:flex;gap:.75rem;align-items:baseline;background:var(--surface-alt);
border-radius:6px;padding:.65rem .85rem;font-size:.88rem}
.cuantas{font-family:var(--mono);color:var(--acuerdo);flex:none;font-variant-numeric:tabular-nums}

.dos-columnas{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
@media (max-width:44rem){.dos-columnas{grid-template-columns:1fr}}
.columna{background:var(--surface);border:1px solid var(--rule);border-radius:9px;
padding:1.1rem 1.3rem}
.columna h3{font-size:.8rem;letter-spacing:.07em;text-transform:uppercase;font-family:var(--mono);
margin:0 0 .8rem}
.col-acuerdo{border-top:3px solid var(--acuerdo)}
.col-acuerdo h3{color:var(--acuerdo)}
.col-pendiente{border-top:3px solid var(--pendiente)}
.col-pendiente h3{color:var(--pendiente)}
.columna ul{margin:0;padding-left:1.15rem;font-size:.92rem}
.columna li{margin-bottom:.55rem}
.columna li:last-child{margin-bottom:0}
.ciclo{display:flex;align-items:stretch;gap:.5rem;overflow-x:auto;padding:.3rem 0 1rem}
.etapa{flex:1;min-width:9rem;background:var(--surface);border:1px solid var(--rule);
border-radius:8px;padding:.85rem .9rem}
.etapa-n{font-family:var(--mono);font-size:.7rem;color:var(--acuerdo);border:1px solid var(--rule-strong);
border-radius:3px;padding:.05rem .35rem}
.etapa-t{margin:.5rem 0 .2rem;font-weight:600;font-size:.92rem}
.etapa-d{margin:0;font-size:.83rem;color:var(--ink-soft);line-height:1.45}
.flecha{display:flex;align-items:center;color:var(--ink-faint);font-size:1.1rem;flex:none}
.nota-ciclo{margin-top:.9rem;padding:.85rem 1.05rem;background:var(--surface-alt);
border-radius:7px;font-size:.91rem;color:var(--ink-soft);max-width:52rem}
.aviso-ciclo{background:var(--pendiente-suave);border-left:3px solid var(--pendiente);
color:var(--ink);border-radius:0 7px 7px 0}
.etapa-turno{padding-top:1rem}
.et{font-family:var(--mono);font-size:.68rem;letter-spacing:.09em;text-transform:uppercase;
color:var(--ink-faint)}
.etapa-turno .consigna{margin:.4rem 0 0;padding-left:.7rem;border-left:2px solid var(--rule-strong);
color:var(--ink-soft);font-size:.88rem;font-style:italic;max-width:44rem}
.igual{margin:.4rem 0 0;font-size:.88rem;color:var(--ink-faint)}
.quien-obs{margin:.9rem 0 .2rem;font-size:.82rem;font-weight:600}
.obs{font-size:.9rem;color:var(--ink-soft);max-width:44rem}
.obs p{margin:0 0 .5rem}
@media (max-width:44rem){.flecha{display:none}.ciclo{flex-direction:column}}
.cot{margin:.5rem 0 .8rem}
.cot>summary{cursor:pointer;font-size:.78rem;color:var(--ink-faint);font-family:var(--mono)}
.cot>summary:hover{color:var(--acuerdo)}
.cot ol{margin:.5rem 0 0;padding-left:1.4rem;font-size:.86rem;color:var(--ink-soft);max-width:44rem}
.cot li{margin-bottom:.45rem}
.recuerdos{margin:.4rem 0 0;padding-left:1.2rem;font-size:.88rem;color:var(--ink-soft);max-width:44rem}
.recuerdos li{margin-bottom:.45rem}
a{color:var(--acuerdo)}
.pie{margin-top:3.5rem;padding-top:1.25rem;border-top:1px solid var(--rule);
color:var(--ink-faint);font-size:.86rem}
@media (max-width:40rem){h1{font-size:1.7rem}.tapa{padding-top:1.75rem}
.adelanto{display:none}.tarjetas{grid-template-columns:1fr}}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style></head><body>
"""



def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("crudo", type=Path, help="raw_log.json de la corrida")
    ap.add_argument("-o", "--salida", type=Path, default=None)
    ap.add_argument("--escenario", type=Path, default=None,
                    help="JSON del escenario, para marcar los hitos críticos")
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

    esc = leer_escenario(args.escenario)
    decisiones = esc.get("decisiones", [])
    premisa = esc.get("premisa", "")

    analisis = {}
    if args.resumen:
        print("  pidiendo resumen, acuerdos y pendientes...")
        analisis = analizar_con_modelo(pasos, series, resumen, premisa,
                                       esc.get("agentes"))
        print("  pidiendo hallazgos y recomendaciones...")
        analisis.update(analizar_hallazgos(pasos, series, esc, resumen))

    salida = args.salida or args.crudo.parent / "reporte.html"

    # Solo se enlaza lo que efectivamente está al lado del informe: los archivos
    # dependen de qué produjo la corrida y de si se pidió el CSV, y un enlace a
    # algo que no existe es peor que no ofrecerlo. El CSV se escribe después, así
    # que se lo da por presente si se pidió.
    posibles = ["reporte.csv", "raw_log.json", "sim_structured.json",
                "resumen.json", "measurements.json"]
    archivos = [n for n in posibles
                if (salida.parent / n).is_file()
                or (n == "reporte.csv" and args.csv)]

    salida.write_text(
        armar(pasos, series, franjas, resumen, decisiones, esc, analisis, archivos),
        encoding="utf-8")

    print(f"reporte      : {salida}")
    print(f"turnos       : {len(pasos)}")
    print(f"actores: {len({p['quien'] for p in pasos})}")
    print(f"indicadores  : {len(series)}" + (f" ({', '.join(sorted(series))})" if series else ""))
    print(f"decisiones   : {len(decisiones)}")
    print(f"resumen      : {'si' if analisis.get('resumen') else 'no'}")
    print(f"acuerdos     : {len(analisis.get('acuerdos') or [])}"
          f" · pendientes: {len(analisis.get('pendientes') or [])}")

    if args.csv:
        ruta_csv = salida.with_suffix(".csv")
        exportar_csv(pasos, series, ruta_csv)
        print(f"csv          : {ruta_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
