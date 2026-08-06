# Deploy de la demo

Instancia de prueba accesible por URL, para que el equipo la use. **No es producción.**

Un solo contenedor: FastAPI sirve la API y además el SPA compilado desde
`frontend/dist`. No hay segundo origen, así que no hay CORS que configurar.

## Por qué no Render free

`requirements.txt` trae `sentence-transformers` (→ `torch`) y el embedder
`all-MiniLM-L6-v2` se carga **en proceso** en cada simulación
(`llm_factory.get_model_and_embedder`). Render free da 512 MB de RAM, y pasar a
Starter ($7) **no agrega memoria** — sigue en 512 MB, solo evita que se duerma.
Para tener RAM real en Render hay que ir a Standard ($25/mes).

Hugging Face Spaces da 2 vCPU / **16 GB RAM** / 50 GB de disco gratis y sin
tarjeta, con URL pública y SDK Docker.

Medido por `.github/workflows/deploy-smoke.yml` (run del 2026-08-06):

| Métrica | Valor |
| --- | --- |
| Imagen | 2040 MB |
| Piso de RAM (embedder cargado + app importada) | **536 MB** |
| Servidor idle, sin simulación | 79 MB |

Los 536 MB son con **cero agentes y cero simulación corriendo**. Render free da
512 MB: no entra ni en reposo. El workflow vuelve a correr solo cuando cambia
el `Dockerfile` o `requirements.txt`, así que el número se mantiene honesto.

## Hugging Face Spaces

1. Sacar la API key en <https://aistudio.google.com/apikey> (gratis, sin tarjeta).
2. Crear el Space: **New Space → SDK: Docker → Blank**, hardware **CPU basic
   (FREE)**. Elegí **Private** si querés que solo lo vea el equipo (ver
   "Acceso" abajo). El SDK Docker es gratis; lo que es PRO es Dev Mode.
3. En **Settings → Variables and secrets**, agregar como *secret*:
   - `GEMINI_API_KEY` = la key de AI Studio
4. Pushear este repo al remote del Space:

   ```bash
   git remote add space https://huggingface.co/spaces/<usuario>/<space>
   git push space main
   ```

5. El Space buildea solo y queda en `https://<usuario>-<space>.hf.space`.

El `README.md` de la raíz lleva el frontmatter que HF necesita (`sdk: docker`,
`app_port: 7860`). Si lo editás, no borres ese bloque.

### En la app

En el panel de LLM del Runner: provider **Google Gemini**, modelo
**`gemini-flash-lite-latest`**. El dropdown consulta la API de Google, así que
lista el catálogo vigente (la línea 1.5 que estaba hardcodeada ya no existe).

Medido el 2026-08-06 sobre 10 requests seguidos: ~960 ms de promedio, sin
rate limiting. `gemini-2.5-flash` aparece listado pero devuelve 404 — parece
requerir tier pago.

### Por qué no NVIDIA NIM

El provider quedó implementado y la key funciona, pero el free tier encola la
inferencia de forma que lo hace inservible acá. Medido el 2026-08-06:

| Llamada | Latencia |
| --- | --- |
| `/v1/models` (metadata) | instantánea |
| Embeddings, batch 1-32 | 14-27 s |
| Chat 70B, 10 tokens | 85 s |
| Chat 70B / nano-8B, reintentos | timeout a 240 s |

No es la red ni el tamaño del modelo: el 8B falla igual que el 70B. Con
timeout de 180 s por request, Concordia no completa ni el demo de 5 pasos.
Si en algún momento NIM deja de encolar, el provider ya está listo para usar.

El embedder remoto (`NVIDIA_EMBED_MODEL`) también quedó descartado por lo
mismo: 14 s por llamada contra ~5 ms del `all-MiniLM-L6-v2` local, y Concordia
embebe en cada memoria escrita y cada recuperación.

## Acceso

**Space privado** (recomendado): solo entra quien tenga cuenta de HF y esté
agregado al Space o a la organización. Cero código.

Si el Space tiene que ser público, hace falta un gate en la app —
`X-Team-Key` en `/api/simulations/*` más una pantalla de clave en el frontend.
El streaming usa `fetch` + `ReadableStream` (no `EventSource`), así que mandar
headers custom funciona. No está implementado todavía.

## Otros hosts

El `CMD` respeta `$PORT`, así que la misma imagen corre en Render, Fly o Cloud
Run sin cambios. Si además separás el frontend a otro dominio, seteá
`ALLOWED_ORIGINS` (coma-separado) para que CORS lo permita.

## Limitaciones conocidas

- **Los logs son efímeros.** `logs/` vive en el filesystem del contenedor: se
  pierde en cada rebuild y en cada arranque en frío. En Spaces free no hay disco
  persistente. Si hay que conservar corridas, migrar a Supabase.
- **Rate limit de Gemini free**: ~15 req/min en los modelos flash. Concordia
  dispara varias llamadas por agente por paso, así que simulaciones grandes van
  a comer 429. Empezar con el template "Coffee Shop Demo" (5 pasos).
- **El Space se pausa** tras ~48 h sin uso. El primer request después tarda.
- Timeouts por default: 180 s por request LLM, watchdog de simulación a 600 s,
  frontend espera hasta 3 h (`VITE_SIMULATION_TIMEOUT`).
