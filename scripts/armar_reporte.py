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
            # Las tres preguntas que Concordia le hace a un participante antes
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
    Qué recordó el participante antes de hablar. Concordia busca en su memoria
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

    # A cada participante le escribe una observación distinta, redactada desde
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

    # Momentos de decisión: permiten ver si los indicadores se movieron ahí
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
                        "con intereses en conflicto eso suele indicar que los participantes "
                        "no sostuvieron sus posiciones, más que un acuerdo trabajado."))
        elif es_consenso and alto >= 100 and fin < alto:
            # Que trepe al techo y se derrumbe es la firma de una deliberación
            # de verdad: hubo acuerdo aparente y algo lo rompió.
            obs.append(("buena",
                        f"El consenso llegó a {alto:.0f} en el turno {paso_alto} y después "
                        f"cayó hasta {fin:.0f}. Un acuerdo que se arma y se rompe indica que "
                        "apareció algo que los participantes no estaban dispuestos a aceptar; "
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
                    "participante se le pidió postura sobre algo concreto de lo que se venía "
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

    # Nada filtra lo que un participante afirma: pasa a ser parte del mundo.
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
    "hallazgos": "Hallazgos",
    "variaciones": "Qué probar",
    "perfiles": "Perfiles",
    "sintesis": "En pocas palabras",
    "acuerdos": "Acuerdos",
    "evolucion": "Evolución",
    "participantes": "Participantes",
    "ciclo": "Cómo funciona",
    "deliberacion": "Deliberación",
    "tecnica": "Ficha técnica",
}


def armar_indice(doc: str) -> str:
    """
    Reemplaza la marca por un índice de las secciones que existen.

    Escrito a mano se desfasaba en las dos direcciones: enlazaba «Acuerdos»,
    que solo se genera si se pidió el análisis —y sin él el enlace no llevaba a
    ninguna parte—, y no enlazaba las secciones agregadas después. Leerlo del
    documento ya armado hace que no pueda volver a pasar.
    """
    enlaces = []
    # `[^>]*` porque varias secciones llevan class además del id.
    for ident, titulo in re.findall(r'<section id="([^"]+)"[^>]*><h2>(.*?)</h2>', doc):
        rotulo = ROTULO_INDICE.get(ident) or re.sub(r"<[^>]+>", "", titulo)
        enlaces.append(f'<a href="#{ident}">{html.escape(rotulo)}</a>')
    if not enlaces:
        return doc.replace(MARCA_NAV, "")
    nav = ('<nav class="navega"><div class="ancho barra">'
           + "".join(enlaces) + "</div></nav>")
    return doc.replace(MARCA_NAV, nav)


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
    El perfil que se le cargó al participante, en palabras.

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

    if esc.get("premisa"):
        p.append("<h3>La situación planteada</h3>")
        p.append(parrafos(esc["premisa"]))

    if esc.get("datos"):
        p.append("<h3>Datos que todos conocían</h3><ul class='lista-datos'>")
        for d in esc["datos"]:
            p.append(f"<li>{html.escape(d)}</li>")
        p.append("</ul>")

    if esc.get("agentes"):
        p.append("<h3>Cada participante</h3>")
        # Solo minimal__Entity recibe el perfil como componente presente en cada
        # acción. En el resto entra como recuerdo y compite por ser recuperado,
        # así que puede no influir en un turno dado. Quien lea el informe tiene
        # que saberlo antes de concluir que el sesgo «no funcionó».
        con_perfil = [a for a in esc["agentes"] if perfil_psicologico(a.get("components") or {})]
        indirectos = [a for a in con_perfil if a.get("prefab") != "minimal__Entity"]
        if indirectos:
            p.append('<p class="ayuda-sec">Los perfiles psicológicos de '
                     f'{len(indirectos)} de {len(con_perfil)} participantes entraron '
                     "como recuerdo, no como componente fijo: solo el tipo «Mínimo» "
                     "los tiene presentes en cada acción. En los demás compiten con "
                     "el resto de la memoria por ser recuperados, así que pueden no "
                     "pesar en todos los turnos.</p>")
        for a in esc["agentes"]:
            p.append('<div class="ficha-agente">')
            p.append(f'<h4>{html.escape(a.get("name", ""))}</h4>')
            if a.get("goal"):
                p.append(f'<p class="obj-agente"><strong>Busca:</strong> {html.escape(a["goal"])}</p>')
            perfil = perfil_psicologico(a.get("components") or {})
            if perfil:
                p.append(perfil)
            mem = a.get("memories") or []
            if mem:
                p.append("<ul class='lista-datos'>")
                for m in mem:
                    p.append(f"<li>{html.escape(m)}</li>")
                p.append("</ul>")
            p.append("</div>")

    filas = [
        ("Tipo de narrador", NOMBRES_MESA.get(esc.get("mesa_prefab", ""), esc.get("mesa_prefab", "—"))),
        ("Orden de la palabra", NOMBRES_ORDEN.get(esc.get("orden", ""), esc.get("orden", "—"))),
        ("Motor", NOMBRES_MOTOR.get(esc.get("motor", ""), esc.get("motor", "—"))),
        ("Turnos pedidos", esc.get("pasos") or "—"),
        ("Puede cerrar antes", "sí" if esc.get("cierre") else "no"),
        ("Modelo de participantes", esc.get("modelo") or "—"),
        ("Modelo del narrador", esc.get("modelo_gm") or "—"),
    ]
    # La temperatura del narrador sí opera; la de los participantes la fija el
    # motor por su cuenta en cada acción. Se dicen las dos, con esa aclaración,
    # porque de otro modo se atribuye a este valor una variabilidad que no
    # controla.
    if esc.get("temp_gm") is not None:
        filas.append(("Temperatura del narrador", esc["temp_gm"]))
    if esc.get("temp") is not None:
        filas.append(("Temperatura de participantes",
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

En "perfiles" evaluá, para cada participante con perfil configurado, si el SESGO se nota en lo que hizo.

Antes de responder, tené presente la distinción que decide todo: perseguir su objetivo NO es evidencia de su sesgo. Que alguien defienda lo que se le pidió defender es que el objetivo funciona. El sesgo se ve solamente cuando el razonamiento se distorsiona MÁS ALLÁ de lo que el objetivo ya explica. Si la conducta se explica entera por el objetivo, la respuesta es "no se observa", aunque la persona haya sido coherente y enfática.

Cada sesgo tiene una huella propia, y hay que encontrar esa huella y no otra:
- anclaje: vuelve a la primera cifra o propuesta que escuchó y la usa de referencia aunque hayan aparecido datos mejores.
- confirmación: se le presenta evidencia que lo contradice y la descarta, la minimiza o no la responde.
- costo hundido: defiende seguir con algo invocando lo ya invertido, no lo que rinde de acá en adelante.
- disponibilidad: generaliza a partir de un caso puntual que recuerda, y le da más peso que a datos agregados.
- endogrupo: evalúa la misma propuesta distinto según quién la haya hecho.

Poné "se manifiesta" solo si podés señalar el turno donde se ve ESA huella y citar qué dijo. Poné "contradice" si hizo lo opuesto: por ejemplo, alguien con sesgo de confirmación que incorpora una objeción que lo desmiente. En cualquier otro caso poné "no se observa", y en el detalle explicá qué se vio en cambio.

Esperamos que "no se observa" sea frecuente: los componentes influyen sin determinar, y fuera del tipo Mínimo compiten con el resto de la memoria por entrar en cada acción. Una lista donde los cinco se manifiestan es señal de que se está confirmando lo configurado en vez de ponerlo a prueba, y eso no sirve. Si nadie tiene perfil, devolvé la lista vacía.

En "acuerdos" poné solo lo que fue aceptado explícitamente por los participantes, no lo que alguien propuso y nadie contestó. En "pendientes" poné lo que se planteó y quedó sin respuesta, lo que se objetó sin resolverse, y lo que el escenario pedía decidir y no se decidió. Cada entrada, una frase corta y concreta. Si no hubo acuerdos explícitos, devolvé la lista vacía.

El campo "resumen" tiene que cubrir:
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

En "variaciones" proponé tres o cuatro modificaciones para una corrida siguiente. Cada una tiene que ser una sola cosa que cambia, nombrada con precisión —qué participante, qué parámetro, qué valor— para que el efecto sea atribuible. Ejemplos de la forma que buscamos: cambiar el sesgo de alguien de confirmación a anclaje para probar si importa el tipo o solo la presencia; pasar de secuencial a simultáneo para probar si el orden de turno da poder de fijar agenda; sacar un indicador de los ocho para probar si el narrador los sigue mejor con menos.

En "mejoras" poné cambios puntuales a ESTE escenario: un objetivo que quedó vago, un dato que faltó y alguien tuvo que inventar, un momento de decisión mal ubicado, un indicador cuya regla no es accionable.

Reglas estrictas:
- Basate solo en lo que aparece más arriba. No inventes citas ni hechos.
- Cada afirmación tiene que poder rastrearse a un turno, un indicador o un valor de configuración. Si no podés señalar dónde se ve, no lo digas.
- Nada de recomendaciones genéricas del tipo «agregar más contexto». Si proponés algo, decí qué exactamente y dónde.
- Si la corrida quedó incompleta, tenelo en cuenta: lo que no pasó puede deberse a que se cortó, no al diseño."""


def pedir_a_gemini(prompt: str, modelo: str, clave: str, timeout: int = 180,
                   tope: int = 4000) -> str:
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
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": tope},
    }).encode("utf-8")
    pedido = urllib.request.Request(
        url, data=cuerpo, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(pedido, timeout=timeout) as r:
        datos = json.loads(r.read().decode("utf-8"))
    partes = datos.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in partes).strip()


def texto_perfiles(agentes) -> str:
    """
    Los perfiles configurados, en una línea por participante, para que el modelo
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
    import os
    clave = os.getenv("GEMINI_API_KEY", "").strip()
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

    crudo_nombre = (resumen_datos or {}).get("modelo_gm") or (resumen_datos or {}).get("modelo") or ""
    modelo = crudo_nombre.split("/")[-1].strip() or "gemini-3.5-flash-lite"
    try:
        bruto = pedir_a_gemini(prompt, modelo, clave, tope=8000)
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
    import os
    clave = os.getenv("GEMINI_API_KEY", "").strip()
    if not clave:
        print("  análisis omitido: falta GEMINI_API_KEY")
        return {}

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
        perfiles=texto_perfiles(agentes),
        transcripcion=transcripcion[:60000],
    )

    # Se reusa el modelo de la corrida para no introducir uno nuevo sin aviso
    crudo_nombre = (resumen_datos or {}).get("modelo_gm") or (resumen_datos or {}).get("modelo") or ""
    modelo = crudo_nombre.split("/")[-1].strip() or "gemini-3.5-flash-lite"
    try:
        bruto = pedir_a_gemini(prompt, modelo, clave)
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


ETAPAS = [
    ("Elige", "el narrador decide a quién le toca hablar"),
    ("Pregunta", "le hace una consigna, distinta según el momento"),
    ("Responde", "el participante dice o hace algo"),
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


def ciclo(pasos) -> str:
    """
    El orden de un turno no se deduce leyendo la transcripción, y sin él no se
    entiende quién puede introducir un hecho en el mundo. Se dibuja el ciclo y
    se mide, sobre esta corrida, cuánto interviene realmente el narrador.
    """
    p = ['<div class="ciclo">']
    for i, (titulo, detalle) in enumerate(ETAPAS):
        p.append('<div class="etapa">')
        p.append(f'<span class="etapa-n">{i + 1}</span>')
        p.append(f'<p class="etapa-t">{titulo}</p>')
        p.append(f'<p class="etapa-d">{detalle}</p>')
        p.append("</div>")
        if i < len(ETAPAS) - 1:
            p.append('<div class="flecha" aria-hidden="true">→</div>')
    p.append("</div>")

    # Paso 4: ¿el narrador transforma lo dicho, o lo copia tal cual?
    comparables = [x for x in pasos if x["dicho"] and x["evento"]]
    if comparables:
        calcados = sum(1 for x in comparables if parecido(x["dicho"], x["evento"]) > 0.95)
        if calcados == len(comparables):
            p.append('<div class="nota-ciclo aviso-ciclo">'
                     f'<strong>En los {len(comparables)} turnos, el hecho registrado quedó '
                     'idéntico a lo que dijo el participante.</strong> El narrador no filtró '
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
                 + (f'A cada participante le escribió una observación distinta, redactada '
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


def armar(pasos, series, franjas, resumen, decisiones, esc=None, analisis=None) -> str:
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

    linea = [f"{len(quienes)} participantes", f"{max_paso} turnos"]
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
            partes.append("<p>No se llegó a estos momentos de decisión:</p><ul>")
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

    partes.append(ayuda_interpretacion(pasos, series, franjas, esc, resumen))

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
    if veredictos:
        partes.append('<section id="perfiles"><h2>¿Se notó el perfil configurado?</h2>')
        partes.append(f'<p class="ayuda-sec">{marca("inferido")} Contraste entre el perfil '
                      "psicológico que se le cargó a cada participante y lo que efectivamente "
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
        partes.append("</ul></section>")

    # ------------------------------------------------------- 6. evolución
    if series or franjas:
        partes.append('<section id="evolucion"><h2>Cómo evolucionó</h2>')
        partes.append(f'<p class="ayuda-sec">{marca("estimado")} Los valores los asigna el narrador '
                      "interpretando lo que se dijo; no son mediciones de nada observado. "
                      "Las líneas punteadas marcan los momentos de decisión previstos.</p>")
        if series:
            hitos = [d.get("step") for d in (decisiones or []) if d.get("step")]
            partes.append('<div class="grafico">' + grafico(series, max_paso, hitos) + "</div>")
        if franjas:
            partes.append('<div class="grafico franjas">')
            for nombre, serie in sorted(franjas.items()):
                partes.append(franja(nombre, serie, max_paso))
            partes.append("</div>")
        partes.append("</section>")

    # --------------------------------------------------- 7. participantes
    partes.append('<section id="participantes"><h2>Quiénes participaron</h2>')
    partes.append('<p class="ayuda-sec">Cada casilla es un turno, coloreada según quién habló.</p>')
    partes.append(tira_participacion(pasos, quienes))
    for q in quienes:
        partes.append('<article class="participante">')
        partes.append(f"<h3>{html.escape(q)}</h3>")
        partes.append(f'<p class="veces">{veces[q]} '
                      f'{"turno" if veces[q] == 1 else "turnos"}</p>')
        if objetivos.get(q):
            partes.append(f'<p class="objetivo">{html.escape(objetivos[q])}</p>')
        partes.append("</article>")
    partes.append("</section>")

    # --------------------------------------------------- 8. la deliberación
    partes.append('<section id="ciclo"><h2>Cómo transcurre un turno</h2>')
    partes.append('<p class="ayuda-sec">La discusión no es una charla libre: cada turno sigue '
                  'siempre la misma secuencia, y quién puede introducir un hecho en el mundo '
                  'depende de ella.</p>')
    partes.append(ciclo(pasos))
    partes.append("</section>")

    partes.append('<section id="deliberacion"><h2>La deliberación</h2>')
    partes.append('<p class="ayuda-sec">Cada turno se abre y muestra la secuencia completa: '
                  "qué se le preguntó, qué respondió y qué se enteraron los demás. "
                  "Los textos son literales, sin resumir.</p>")
    # Leer la deliberación entera obligaba a abrir turno por turno. El botón se
    # inserta desde el script para que no aparezca muerto donde no haya JS: sin
    # él, los turnos siguen abriéndose de a uno como hasta ahora.
    partes.append('<div id="control-turnos"></div>')
    for p in pasos:
        hito = por_paso.get(p["n"])
        if hito:
            partes.append('<div class="hito"><span class="hito-rotulo">Momento de decisión '
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
            partes.append('<div class="etapa-turno"><span class="et">Se le preguntó</span>'
                          f'<p class="consigna">{html.escape(mesa["consigna"])}</p></div>')

        partes.append('<div class="etapa-turno"><span class="et">Respondió</span>'
                      f'<div class="dicho">{parrafos(dicho)}</div></div>')

        # Si el hecho registrado difiere de lo dicho, el narrador intervino y
        # conviene poder verlo; si es igual, decirlo evita repetir el texto.
        if p["evento"] and p["dicho"]:
            if parecido(p["dicho"], p["evento"]) > 0.95:
                partes.append('<div class="etapa-turno"><span class="et">Quedó registrado</span>'
                              '<p class="igual">Tal cual, sin cambios del narrador.</p></div>')
            else:
                partes.append('<div class="etapa-turno"><span class="et">Quedó registrado</span>'
                              f'<div class="dicho">{parrafos(p["evento"])}</div></div>')

        obs = mesa.get("observaciones") or {}
        if obs:
            partes.append('<details class="interno"><summary>Qué se enteró cada uno '
                          f'({len(obs)} participantes)</summary><div>')
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
            partes.append('<details class="interno"><summary>Cómo razonó antes de hablar'
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
            partes.append('<details class="interno"><summary>Qué recordó '
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
                  "La transcripción reproduce lo que dijo cada participante, sin resumir.</footer>")
    partes.append("""<script>
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
    return armar_indice("".join(partes))


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
.navega{position:sticky;top:0;z-index:20;background:var(--ground);
border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)}
.barra{display:flex;gap:1.4rem;padding-top:.75rem;padding-bottom:.75rem;
overflow-x:auto;scrollbar-width:thin}
.navega a{color:var(--ink-soft);text-decoration:none;font-size:.88rem;white-space:nowrap}
.navega a:hover,.navega a:focus-visible{color:var(--acuerdo)}
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
.participante{padding:.9rem 0;border-top:1px solid var(--rule);max-width:52rem}
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
    salida.write_text(armar(pasos, series, franjas, resumen, decisiones, esc, analisis), encoding="utf-8")

    print(f"reporte      : {salida}")
    print(f"turnos       : {len(pasos)}")
    print(f"participantes: {len({p['quien'] for p in pasos})}")
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
