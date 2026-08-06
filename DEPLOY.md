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

El workflow `.github/workflows/deploy-smoke.yml` mide el piso de memoria real y
deja el veredicto en el summary del run. Corrélo antes de decidir.

## Hugging Face Spaces

1. Sacar la API key en <https://build.nvidia.com> (empieza con `nvapi-`).
2. Crear el Space: **New Space → SDK: Docker → Blank**. Elegí **Private** si
   querés que solo lo vea el equipo (ver "Acceso" abajo).
3. En **Settings → Variables and secrets**, agregar como *secret*:
   - `NVIDIA_NIM_API_KEY` = `nvapi-...`
4. Pushear este repo al remote del Space:

   ```bash
   git remote add space https://huggingface.co/spaces/<usuario>/<space>
   git push space main
   ```

5. El Space buildea solo y queda en `https://<usuario>-<space>.hf.space`.

El `README.md` de la raíz lleva el frontmatter que HF necesita (`sdk: docker`,
`app_port: 7860`). Si lo editás, no borres ese bloque.

### En la app

En el panel de LLM del Runner: provider **NVIDIA NIM**. El dropdown de modelos
se llena solo consultando `/v1/models` de NIM con la key del entorno, así que
los IDs que ves son los que realmente existen en el catálogo.

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
- **Rate limit de NIM**: 40 req/min en el free tier. Concordia dispara varias
  llamadas por agente por paso, así que simulaciones grandes van a comer 429.
  Empezar con el template "Coffee Shop Demo" (5 pasos).
- **El Space se pausa** tras ~48 h sin uso. El primer request después tarda.
- Timeouts por default: 180 s por request LLM, watchdog de simulación a 600 s,
  frontend espera hasta 3 h (`VITE_SIMULATION_TIMEOUT`).
