#!/usr/bin/env python3
"""
Corre una simulacion desde un JSON, sin servidor ni navegador.

Reutiliza el builder y el factory del backend (que funcionan) y saltea la capa
HTTP/SSE, donde viven los tres problemas conocidos: el stream que se corta a
mitad de la primera vuelta, el watchdog que reescribe el log entero cada cinco
segundos, y el post-procesamiento que queda en loop sin escribir el resultado.

    python scripts/run_headless.py escenario.json -o resultados/

Escribe en el directorio de salida:
    index.html        la deliberacion, abrible en cualquier navegador
    raw_log.json      el log estructurado, para analizar con pandas o similar
    resumen.json      pasos, duracion, variables finales
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def log(msg: str) -> None:
    """Imprime con hora y sin buffer, para que se vea en vivo en los logs de CI."""
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("escenario", type=Path, help="JSON del escenario")
    ap.add_argument("-o", "--salida", type=Path, default=Path("resultados"))
    ap.add_argument("--max-steps", type=int, default=None,
                    help="Pisa el max_steps del JSON (util para probar)")
    args = ap.parse_args()

    if not args.escenario.is_file():
        log(f"ERROR: no existe {args.escenario}")
        return 1

    data = json.loads(args.escenario.read_text(encoding="utf-8"))

    # Acepta tanto {config, llm_settings} como un config suelto, que es lo que
    # exporta la app en sus dos formatos.
    cfg_raw = data.get("config", data)
    llm_raw = data.get("llm_settings")
    gm_raw = data.get("gm_llm_settings")

    if not llm_raw:
        log("ERROR: falta 'llm_settings' en el JSON")
        return 1

    from backend.models.schemas import SimulationConfig, LLMSettings
    from backend.services.llm_factory import get_model_and_embedder
    from backend.services.simulation_builder import build_simulation
    from backend.services.simulation_runner import _raw_log_to_html, _inject_html_styles

    try:
        config = SimulationConfig(**cfg_raw)
    except Exception as e:
        log(f"ERROR: el escenario no es valido: {e}")
        return 1

    if args.max_steps is not None:
        config.max_steps = args.max_steps

    # Falla temprano y con un mensaje claro: sin esto el error aparece recien
    # en la primera llamada, despues de haber construido toda la simulacion.
    llm_settings = LLMSettings(**llm_raw)
    gm_settings = LLMSettings(**gm_raw) if gm_raw else None

    log("=" * 62)
    log(f"Escenario  : {args.escenario.name}")
    log(f"Agentes    : {', '.join(a.name for a in config.agents)}")
    log(f"Pasos      : {config.max_steps}")
    log(f"Modelo     : {llm_settings.provider}/{llm_settings.model_name}")
    if gm_settings:
        log(f"Modelo GM  : {gm_settings.provider}/{gm_settings.model_name}")
    log("=" * 62)

    log("Cargando modelo y embedder...")
    model, embedder = get_model_and_embedder(llm_settings)
    gm_model = get_model_and_embedder(gm_settings)[0] if gm_settings else None

    log("Construyendo la simulacion...")
    sim = build_simulation(config, model, embedder, gm_model=gm_model)

    args.salida.mkdir(parents=True, exist_ok=True)
    inicio = time.time()
    paso = [0]

    def on_step(checkpoint_data):
        # checkpoint_counter es 0-indexed; +1 para el numero que ve una persona
        n = checkpoint_data.get("checkpoint_counter", 0) + 1
        paso[0] = n
        transcurrido = time.time() - inicio
        resto = ""
        if n:
            faltan = (transcurrido / n) * (config.max_steps - n)
            if faltan > 0:
                resto = f", faltan ~{faltan / 60:.0f} min" if faltan > 90 else f", faltan ~{faltan:.0f}s"
        log(f"  paso {n}/{config.max_steps} listo ({transcurrido / 60:.1f} min{resto})")

    log("Arrancando. A partir de aca cada paso son varias llamadas al modelo.")
    error = None
    try:
        sim.play(max_steps=config.max_steps, get_state_callback=on_step)
    except KeyboardInterrupt:
        error = "interrumpida a mano"
        log(f"INTERRUMPIDA en el paso {paso[0]} — igual guardo lo que hay")
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log(f"ERROR en el paso {paso[0]}: {error}")
        # Con el tipo y el mensaje solos no se puede ubicar nada: un
        # «TypeError: 'int' object is not iterable» a mitad del motor no dice ni
        # qué archivo ni qué línea, y averiguarlo cuesta otra corrida entera.
        # La traza va al registro de ejecución, que es donde se la busca.
        import traceback
        log("Traza completa:")
        for linea in traceback.format_exc().rstrip().split("\n"):
            log("    " + linea)
        log("Guardo igual los pasos que si se completaron.")

    duracion = time.time() - inicio
    log(f"Corrida terminada en {duracion / 60:.1f} min ({paso[0]}/{config.max_steps} pasos)")

    # De aca en adelante todo es guardar. Cada pieza va en su propio try para
    # que un fallo al escribir una no se lleve puestas las demas.
    log("Guardando resultados...")
    raw_log = sim.get_raw_log()

    try:
        html = _inject_html_styles(_raw_log_to_html(raw_log))
        (args.salida / "index.html").write_text(html, encoding="utf-8")
        log(f"  index.html      ({len(html):,} caracteres)")
    except Exception as e:
        log(f"  index.html      FALLO: {e}")

    crudo = ""   # queda definido aunque el volcado falle: lo usa la comparación de abajo
    try:
        crudo = json.dumps(raw_log, ensure_ascii=False, indent=1, default=str)
        (args.salida / "raw_log.json").write_text(crudo, encoding="utf-8")
        log(f"  raw_log.json    ({len(crudo):,} caracteres)")
    except Exception as e:
        log(f"  raw_log.json    FALLO: {e}")

    # Formato canónico de Concordia. Es el que lee su herramienta oficial
    # `concordia-log` (overview, actions, context, memories, search...), y
    # deduplica el contenido repetido, así que además pesa bastante menos que
    # el volcado crudo.
    try:
        from concordia.utils.structured_logging import SimulationLog
        estructurado = SimulationLog.from_raw_log(raw_log).to_json()
        (args.salida / "sim_structured.json").write_text(estructurado, encoding="utf-8")
        if crudo:
            ahorro = 100 - (100 * len(estructurado) / len(crudo))
            log(f"  sim_structured  ({len(estructurado):,} caracteres, "
                f"{ahorro:.0f}% menos que el crudo)")
        else:
            log(f"  sim_structured  ({len(estructurado):,} caracteres)")
    except Exception as e:
        log(f"  sim_structured  FALLO: {type(e).__name__}: {e}")

    # Telemetría por componente. No viaja en el log crudo: vive en un objeto
    # aparte que se pierde al terminar el proceso si no se vuelca acá.
    try:
        medidas = getattr(sim, "_measurements", None)
        canales = medidas.get_all_channels() if medidas else {}
        if canales:
            volcado = {}
            for nombre, datos in canales.items():
                volcado[nombre] = [
                    {k: str(v) for k, v in (d.__dict__ if hasattr(d, "__dict__") else d).items()}
                    if hasattr(d, "__dict__") or isinstance(d, dict) else str(d)
                    for d in datos
                ]
            texto = json.dumps(volcado, ensure_ascii=False, indent=1, default=str)
            (args.salida / "measurements.json").write_text(texto, encoding="utf-8")
            log(f"  measurements    ({len(canales)} canales, {len(texto):,} caracteres)")
        else:
            log("  measurements    (sin canales)")
    except Exception as e:
        log(f"  measurements    FALLO: {e}")

    resumen = {
        "escenario": args.escenario.name,
        "terminada": datetime.datetime.now().isoformat(timespec="seconds"),
        "duracion_min": round(duracion / 60, 1),
        "pasos_completados": paso[0],
        "pasos_pedidos": config.max_steps,
        "completa": paso[0] >= config.max_steps and error is None,
        "error": error,
        # Bandera explícita en vez de dejar que el relanzador adivine buscando
        # texto: el corte por cuota traduce el error de Google al castellano, así
        # que buscar 'PerDay' en el mensaje dejó de encontrarlo y la corrida que
        # había que relanzar quedaba esperando indefinidamente.
        "corto_por_cuota": bool(error and (
            "cuota DIARIA" in error or "PerDay" in error
            or "RESOURCE_EXHAUSTED" in error)),
        "agentes": [a.name for a in config.agents],
        "modelo": f"{llm_settings.provider}/{llm_settings.model_name}",
        "modelo_gm": f"{gm_settings.provider}/{gm_settings.model_name}" if gm_settings else None,
    }

    # Cuántas llamadas costó, por modelo. Es el dato que faltaba para saber
    # cuántos pasos entran en los 500 diarios del plan gratuito en vez de
    # descubrirlo cuando la corrida se corta por la mitad.
    try:
        from backend.models.llm_wrappers import CONSUMO
        if CONSUMO:
            resumen["consumo"] = CONSUMO
            resumen["consumo_por_paso"] = {
                m: round(d["llamadas"] / paso[0], 1)
                for m, d in CONSUMO.items() if paso[0]
            }
            for m, d in CONSUMO.items():
                log(f"  {m}: {d['llamadas']} llamadas, "
                    f"{d['esperas']} esperas ({d['segundos_esperando']}s)")
    except Exception as e:
        log(f"  consumo         FALLO: {e}")

    (args.salida / "resumen.json").write_text(
        json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"  resumen.json")

    # Salida 0 tambien cuando quedo incompleta: los resultados parciales sirven
    # y el workflow tiene que poder publicarlos igual.
    return 0


if __name__ == "__main__":
    sys.exit(main())
