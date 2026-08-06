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

Medido por `.github/workflows/deploy-smoke.yml` (run del 2026-08-06):

| Métrica | Valor |
| --- | --- |
| Imagen | 2040 MB |
| Piso de RAM (embedder cargado + app importada) | **536 MB** |
| Servidor idle, sin simulación | 79 MB |

Los 536 MB son con **cero agentes y cero simulación corriendo**. Render free da
512 MB: no entra ni en reposo. El workflow vuelve a correr solo cuando cambia
el `Dockerfile` o `requirements.txt`, así que el número se mantiene honesto.

## Cómo se levanta: GitHub Codespaces

Es la opción elegida para la demo: sin tarjeta, sin costo, y el Codespace tiene
8 GB de RAM contra los 536 MB de piso. La contra es que **la URL vive solo
mientras el Codespace está prendido** y consume de las 60 h/mes del free tier.
Para una demo interna alcanza; para algo sostenido, ver "Otros hosts".

1. Sacar la API key de Gemini en <https://aistudio.google.com/apikey>
   (gratis, sin tarjeta).
2. Cargarla como **secret de Codespaces**, no en el repo:
   <https://github.com/settings/codespaces> → New secret → `GEMINI_API_KEY`,
   con acceso a este repositorio.
3. Crear el Codespace desde el repo: **Code → Codespaces → Create codespace**.
   El `.devcontainer/` instala todo solo (torch CPU, requirements, embedder,
   build del SPA). La primera vez tarda ~10 min.
4. Cuando termine:

   ```bash
   ./run-demo.sh
   ```

   El script pone el puerto 8000 en público e imprime la URL, del estilo
   `https://<codespace>-8000.app.github.dev`. Esa es la que se comparte.

Si el script no logra cambiar la visibilidad, se hace a mano desde la pestaña
**PORTS** de VS Code: click derecho en el puerto 8000 → Port Visibility →
Public. Sin eso, el link pide login de GitHub y sólo entra quien tenga acceso
al repo.

## Hugging Face Spaces: ya no sirve gratis

Alrededor de julio de 2026 HF eliminó el CPU Basic gratuito y dejó **Docker y
Gradio detrás de plan pago** (PRO, USD 9/mes personal). La doc de Docker Spaces
todavía no refleja el cambio. Lo que queda gratis es ZeroGPU con 3,5 min
diarios de cuota, que no alcanza.

El `README.md` conserva el frontmatter (`sdk: docker`, `app_port: 7860`) por si
en algún momento se paga PRO: con eso el deploy es `git push` al remote del
Space y nada más. No molesta en ningún otro host.

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

En Codespaces el puerto tiene dos modos:

- **Private** (default): el link pide login de GitHub y sólo entra quien tenga
  acceso al repo. Sirve si todo el equipo está en el fork.
- **Public**: entra cualquiera con el link. Es lo que hace `run-demo.sh`.

En modo público no hay ninguna protección: el link es la credencial. Como la
URL es larga y efímera alcanza para una demo, pero **no dejes el Codespace
prendido sin necesidad**.

Si hiciera falta un gate real, sería `X-Team-Key` en `/api/simulations/*` más
una pantalla de clave en el frontend. El streaming usa `fetch` +
`ReadableStream` (no `EventSource`), así que mandar headers custom funciona.
No está implementado.

## Otros hosts

El `CMD` del Dockerfile respeta `$PORT`, así que la misma imagen corre en
Render, Fly o Cloud Run sin cambios. Para algo que dure más que una sesión,
**Cloud Run** es la mejor opción: free tier amplio, Docker nativo, escala a
cero y admite hasta 60 min de timeout por request (útil para simulaciones
largas). Pide tarjeta para habilitar billing aunque no cobre.

Si además separás el frontend a otro dominio, seteá `ALLOWED_ORIGINS`
(coma-separado) para que CORS lo permita.

## Limitaciones conocidas

- **Los logs se pierden con el Codespace.** `logs/` vive en el disco del
  Codespace, que GitHub borra tras 30 días de inactividad (o cuando lo
  eliminás). Si una corrida importa, bajala del explorador de archivos antes de
  apagar, o migrar a Supabase.
- **Rate limit de Gemini free**: ~15 req/min en los modelos flash. Concordia
  dispara varias llamadas por agente por paso, así que simulaciones grandes van
  a comer 429. Empezar con el template "Coffee Shop Demo" (5 pasos).
- **El Space se pausa** tras ~48 h sin uso. El primer request después tarda.
- Timeouts por default: 180 s por request LLM, watchdog de simulación a 600 s,
  frontend espera hasta 3 h (`VITE_SIMULATION_TIMEOUT`).
