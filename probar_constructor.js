// Corre la logica real del constructor contra un DOM minimo y valida que el
// archivo que produce sea exactamente el que el motor espera.
const { readFileSync, existsSync } = require('fs');

// El archivo del repo, no una copia. Antes era 'constructor.html' a secas y
// tomaba la que hubiera en el directorio desde donde se corriera: una copia
// vieja del scratchpad paso varias rondas de cambios sin que se probara nada,
// informando que todo pasaba. Se busca la del repo y, si no aparece, se corta
// en vez de validar cualquier cosa.
const RUTA = process.argv[2] ||
  ['pages/constructor.html', 'csb/pages/constructor.html'].find(existsSync);
if (!RUTA) {
  console.error('No encontre pages/constructor.html. Corre esto desde la raiz ' +
                'del repo, o pasa la ruta como argumento.');
  process.exit(1);
}
console.log('  probando: ' + RUTA);
const html = readFileSync(RUTA, 'utf8');
const js = html.split('<script>')[1].split('</script>')[0];

// --- DOM minimo ------------------------------------------------------------
const nodo = (tag) => {
  const n = {
    tagName: tag, textContent: '', className: '', value: '', checked: false,
    rows: 0, type: '', id: '', htmlFor: '', placeholder: '', hidden: false,
    disabled: false, min: 0, max: 0, step: 0, style: {}, children: [], _opciones: [],
    appendChild(c) {
      this.children.push(c);
      if (c.tagName === 'option') { this._opciones.push(c.value); if (!this.value) this.value = c.value; }
      return c;
    },
    addEventListener() {}, removeChild() {}, click() {}, select() {},
    querySelector() { return nodo('label'); },
    scrollIntoView() {},
  };
  return n;
};

// Valores iniciales tomados del HTML, para no inventarlos en el test
const desdeHtml = {};
for (const m of html.matchAll(/<input\s+id="([^"]+)"[^>]*?value="([^"]*)"/g)) desdeHtml[m[1]] = m[2];
for (const m of html.matchAll(/id="([^"]+)"[^>]*?\svalue="([^"]*)"/g)) if (!(m[1] in desdeHtml)) desdeHtml[m[1]] = m[2];
// Los <select> que ya traen sus <option> en el HTML arrancan con el primero
// seleccionado, igual que en un navegador. Sin esto quedaban vacios y el test
// daba por buenos campos que en realidad no estaba comprobando.
for (const m of html.matchAll(/<select\s+id="([^"]+)"\s*>([\s\S]*?)<\/select>/g)) {
  const primera = m[2].match(/<option\s+value="([^"]*)"/);
  if (primera) desdeHtml[m[1]] = primera[1];
}

// value -> data-proveedor, leido del mismo HTML que se prueba.
// Cada modelo figura en las dos listas —actores y narrador— y quedarse con la
// ultima aparicion esconde que se contradigan: una etiqueta rota en una lista
// pasaba desapercibida porque su duplicado en la otra seguia bien.
const PROVEEDORES = {};
const CONFLICTOS = [];
for (const m of html.matchAll(/<option value="([^"]+)" data-proveedor="([^"]+)"/g)) {
  if (PROVEEDORES[m[1]] !== undefined && PROVEEDORES[m[1]] !== m[2]) {
    CONFLICTOS.push(m[1] + ': ' + PROVEEDORES[m[1]] + ' y ' + m[2]);
  }
  PROVEEDORES[m[1]] = m[2];
}

const cache = {};
global.document = {
  getElementById(id) {
    if (!cache[id]) { cache[id] = nodo('div'); cache[id].value = desdeHtml[id] ?? ''; }
    return cache[id];
  },
  createElement(t) { return nodo(t); },
  createTextNode(t) { return { nodeType: 3, textContent: t }; },
  body: nodo('body'),
  // proveedorDe() consulta las opciones por data-proveedor. Se resuelven contra
  // el HTML real: con un nodo falso la comprobacion pasaba sin mirar nada, y sin
  // el metodo el constructor ni corre.
  querySelector(sel) {
    const m = /option\[value="([^"]+)"\]\[data-proveedor\]/.exec(sel || '');
    if (m) {
      const prov = PROVEEDORES[m[1]];
      return prov ? { getAttribute: () => prov } : null;
    }
    return nodo('div');
  },
  querySelectorAll() { return []; },
};
global.window = global;
global.navigator = {};
global.Blob = function () {};
global.URL = { createObjectURL: () => '', revokeObjectURL() {} };
global.setTimeout = () => {};

eval(js);
const api = globalThis.__api;
if (!api) { console.error('FALLO: no se expuso la API interna'); process.exit(1); }

// Marcar las casillas que el HTML trae tildadas
document.getElementById('antiadulacion').checked = true;

// --- cargar el ejemplo y construir -----------------------------------------
document.getElementById('b-ejemplo') ; // el boton existe; llamamos al cargador via la API
// El cargador vive dentro del IIFE; lo disparamos poblando el estado como lo hace el
// boton no es posible desde afuera, asi que replicamos su efecto llamando dibujarTodo
// despues de setear el estado. En su lugar validamos el camino real: usar el ejemplo
// requiere el listener, asi que probamos el armado con un escenario equivalente.
const e = api.estado;
e.agentes = [
  { nombre: 'Laura Benítez — Estudios', prefab: 'basic__Entity', objetivo: 'Evidencia trazable.',
    memorias: 'Produce indicadores.\nDesconfía de rankings.', contexto: '', azar: false },
  { nombre: 'Mariana Roldán — Secretaría', prefab: 'basic_with_plan__Entity', objetivo: 'Diseño defendible.',
    memorias: 'Decide al final.', contexto: 'Le pidieron que salga este trimestre.', azar: false },
  { nombre: 'Martín Salvatierra — Comité', prefab: 'basic__Entity', objetivo: 'Calidad científica.',
    memorias: 'Cuestiona consorcios formales.', contexto: '', azar: true,
    acciones: 'Aprobar\nObjetar\nPedir más tiempo', ventSituacion: '40', ventAccion: '30',
    sesgo: 'anchoring_bias', sesgoFuerza: 'strong', emocion: 'cautela',
    rasgos: { conscientiousness: '5', neuroticism: '4', openness: '' },
    valores: 'Prudencia fiscal\nTransparencia', tension: 'cautela vs urgencia' },
  { nombre: 'Verónica Cruz — Consejo', prefab: 'conversational__Entity', objetivo: 'Liderazgo federal real.',
    memorias: 'Representa a las provincias chicas.', contexto: '', azar: false },
  { nombre: 'Sergio Ledesma — Productores', prefab: 'basic__Entity', objetivo: 'Soluciones aplicables.',
    memorias: 'Desconfía de tecnología cara.', contexto: '', azar: false },
];
e.variables = [
  { nombre: 'Nivel de consenso', tipo: 'percentage', inicial: 25, descripcion: 'Acuerdo explícito.', regla: 'Sube solo con aceptación.' },
  { nombre: 'Monto por proyecto', tipo: 'numerical', inicial: 40, min: 0, max: 500, descripcion: 'Millones asignados.', regla: 'Baja con el recorte.' },
  { nombre: 'Diseño final', tipo: 'categorical', inicial: 'pendiente', opciones: 'pendiente\nfondo_mixto\npostergar', descripcion: 'Modelo acordado.', regla: 'Solo lo fija quien conduce.' },
  { nombre: 'Hubo recorte', tipo: 'boolean', inicial: 'false', descripcion: 'Si se aplicó el recorte.', regla: 'Pasa a sí en el paso 17.' },
];
e.decisiones = [
  { paso: 17, texto: 'El presupuesto se reduce un veinte por ciento.' },
  { paso: 5, texto: 'Definir cómo se reparten los recursos.' },
];
e.acciones = [
  { nombre: 'Votar a favor', descripcion: 'Apoya la propuesta.', quienes: '', condicion: 'Solo tras leer el dictamen.' },
];
e.contrib = ['npc_event_generator'];

document.getElementById('premisa').value = 'Una mesa debe repartir fondos limitados.';
document.getElementById('gm-nombre').value = 'Mesa de prueba';
document.getElementById('datos').value = 'El presupuesto alcanza para seis proyectos.\nDos provincias concentran el sesenta y cinco por ciento.';
document.getElementById('moderacion').value = 'Pedir propuestas concretas.';
document.getElementById('cierre-temprano').checked = false;
document.getElementById('usar-reloj').checked = true;
document.getElementById('reloj-inicio').value = '2026-08-05 09:00:00';
document.getElementById('reloj-desc').value = 'Una jornada';

const d = api.construir();
const c = d.config, gm = c.game_master;

const filas = [
  ['participantes',        c.agents.length],
  ['prefabs distintos',    [...new Set(c.agents.map(a => a.prefab))].join(', ')],
  ['ids sin acentos',      c.agents.map(a => a.id).slice(0, 2).join(', ')],
  ['randomize_choices',    c.agents.filter(a => a.randomize_choices).length + ' con mezcla'],
  ['datos compartidos',    c.shared_memories.length + ' (2 propios + 3 anti-adulacion)'],
  ['contexto propio',      Object.keys(c.player_specific_context || {}).length + ' participante(s)'],
  ['motor',                c.engine_type],
  ['tipo de mesa',         gm.prefab],
  ['orden',                gm.acting_order],
  ['cierre temprano',      String(gm.allow_early_termination)],
  ['moderation_instructions', 'moderation_instructions' in gm.parameters ? 'presente' : 'ausente'],
  ['indicadores',          gm.grounded_variables.length],
  ['  tipos',              gm.grounded_variables.map(v => v.variable_type).join(', ')],
  ['  categorico opciones', JSON.stringify(gm.grounded_variables.find(v => v.variable_type === 'categorical').allowed_values)],
  ['  numerico min/max',   (() => { const v = gm.grounded_variables.find(x => x.variable_type === 'numerical'); return v.min_value + ' a ' + v.max_value; })()],
  ['  booleano default',   JSON.stringify(gm.grounded_variables.find(v => v.variable_type === 'boolean').default_value)],
  ['momentos de decision', gm.critical_decision_points.length + ' (ordenados: ' + gm.critical_decision_points.map(p => p.step).join(', ') + ')'],
  ['acciones disponibles', (c.available_actions || []).length],
  ['componentes extra',    (gm.contrib_components || []).map(x => x.component_id).join(', ')],
  ['reloj',                c.clock ? c.clock.clock_type + ', ' + c.clock.increment_minutes + ' min/paso' : 'sin reloj'],
  ['checkpoint cada',      c.checkpoint_interval],
  ['modelos',              d.llm_settings.model_name + ' / ' + d.gm_llm_settings.model_name],
  ['temperaturas',         d.llm_settings.temperature + ' / ' + d.gm_llm_settings.temperature],
  ['turnos de Mariana',    'pasos ' + api.pasosDe(1).join(', ')],
  ['acciones por agente',   JSON.stringify(c.agents[2].available_actions)],
  ['ventanas de memoria',   JSON.stringify(c.agents[2].components)],
  ['agente sin ajustes',    JSON.stringify(c.agents[0].components) + ' / ' +
                            JSON.stringify(c.agents[0].available_actions)],
  ['perfil: sesgo',         JSON.stringify(c.agents[2].components.cognitive_bias)],
  ['perfil: emocion',       JSON.stringify(c.agents[2].components.emotion)],
  ['perfil: rasgos',        JSON.stringify(c.agents[2].components.personality_traits)],
  ['perfil: valores',       JSON.stringify(c.agents[2].components.values)],
];
for (const [k, v] of filas) console.log('  ' + k.padEnd(24) + ': ' + v);

// --- comprobaciones duras --------------------------------------------------
const fallas = [];
const chk = (cond, msg) => { if (!cond) fallas.push(msg); };

chk(typeof c.premise === 'string' && c.premise.length > 0, 'premise vacia');
chk(Array.isArray(c.agents) && c.agents.length > 0, 'sin agentes');
chk(gm.critical_decision_points.every(p => typeof p.event === 'string' && p.event.length),
    "algun momento de decision sin 'event' (esto rompe el motor)");
chk(gm.critical_decision_points[0].step <= gm.critical_decision_points[1].step,
    'los momentos no quedaron ordenados por paso');
const cat = gm.grounded_variables.find(v => v.variable_type === 'categorical');
chk(Array.isArray(cat.allowed_values) && cat.allowed_values.length >= 2, 'categorico sin opciones');
chk(cat.allowed_values.includes(cat.default_value), 'el valor inicial del categorico no esta entre sus opciones');
chk(['numerical', 'categorical', 'boolean', 'percentage'].includes(gm.grounded_variables[0].variable_type),
    'tipo de variable fuera del esquema');
chk(['sequential', 'simultaneous', 'asynchronous', 'step_controller', 'interview', 'survey'].includes(c.engine_type),
    'engine_type fuera del esquema');
chk(['fixed', 'random', 'game_master_choice'].includes(gm.acting_order), 'acting_order fuera del esquema');
chk(c.shared_memories.length === 5, 'los datos del caso no se sumaron a shared_memories');
chk(api.pasosDe(1).includes(17), 'el recorte no cae en el turno de quien conduce');
chk(c.clock && c.clock.clock_type, 'el reloj no se armo');
chk(/\S/.test(d.llm_settings.model_name), 'el modelo de los participantes quedo vacio');
chk(/\S/.test(d.gm_llm_settings.model_name), 'el modelo de la mesa quedo vacio');
chk(/\S/.test(d.llm_settings.embedder_model), 'el modelo de memoria quedo vacio');
chk(d.llm_settings.max_tokens > 0 && d.llm_settings.request_timeout > 0, 'tokens o timeout invalidos');

// El proveedor se deduce del nombre del modelo. Si se rompe, un modelo de Groq
// termina en el cliente de Gemini y la corrida falla recien al arrancar.
// Comparar la salida contra PROVEEDORES seria tautologico: el constructor lee
// esa misma tabla, asi que una etiqueta mal puesta coincide consigo misma. Se
// deduce el proveedor del nombre del modelo, que es conocimiento independiente
// del HTML, y se exige que ambos coincidan.
function proveedorEsperado(m) {
  if (/^gemini-/.test(m)) return 'gemini';
  if (/^(nvidia|meta|mistralai|microsoft|qwen)\//.test(m)) return 'nvidia';
  if (/(-instant|-versatile)$/.test(m) || /^openai\/gpt-oss/.test(m)) return 'groq';
  return null;
}
chk(Object.keys(PROVEEDORES).length >= 8, 'faltan opciones con proveedor declarado');
// Toda opcion de las dos listas de modelo tiene que declarar su proveedor. Sin
// esto, a una que lo pierda la cubre su duplicado en la otra lista, hasta que
// alguien agregue un modelo que figure una sola vez y caiga en el que va por
// defecto.
const SIN_DECLARAR = [];
for (const bloque of html.matchAll(/<select id="modelo(?:-gm)?">([\s\S]*?)<\/select>/g)) {
  for (const o of bloque[1].matchAll(/<option value="([^"]+)"([^>]*)>/g)) {
    if (!/data-proveedor=/.test(o[2])) SIN_DECLARAR.push(o[1]);
  }
}
chk(SIN_DECLARAR.length === 0,
    'opciones de modelo sin data-proveedor: ' + SIN_DECLARAR.join(', '));
chk(CONFLICTOS.length === 0, 'un modelo declara distinto proveedor segun la lista — '
    + CONFLICTOS.join('; '));
Object.keys(PROVEEDORES).forEach(function (m) {
  var esp = proveedorEsperado(m);
  chk(esp !== null, 'no se de que proveedor es el modelo ' + m);
  chk(esp === null || PROVEEDORES[m] === esp,
      'la opcion ' + m + ' dice proveedor ' + PROVEEDORES[m] + ' y deberia ser ' + esp);
});
['llm_settings', 'gm_llm_settings'].forEach(function (k) {
  var s = d[k];
  chk(PROVEEDORES[s.model_name] !== undefined,
      k + ': el modelo ' + s.model_name + ' no declara proveedor');
  chk(s.provider === proveedorEsperado(s.model_name),
      k + ': proveedor ' + s.provider + ' para el modelo ' + s.model_name +
      ' (deberia ser ' + proveedorEsperado(s.model_name) + ')');
});
chk(!('provider' in (d.config || {})), 'el proveedor no va dentro de config');
chk(gm.prefab.endsWith('__GameMaster'), 'el tipo de mesa no es un prefab de mesa');
// Sin tocar nada, el narrador que queda es el primero de la lista. Medimos que
// el conversacional fija el consenso en el maximo desde el turno siete sin que
// nadie sostenga una objecion, asi que no puede ser el que se lleve quien no
// sabe que hay que elegir.
chk(gm.prefab === 'generic__GameMaster',
    'el narrador por defecto es ' + gm.prefab + ' y deberia ser generic__GameMaster');
chk(c.agents.every(a => a.prefab.endsWith('__Entity')), 'algun participante no tiene prefab de entidad');
// Las ventanas y las acciones solo deben aparecer en quien las configuro
chk(Array.isArray(c.agents[2].available_actions) && c.agents[2].available_actions.length === 3,
    'las acciones por agente no se generaron');
chk(c.agents[2].components
    && c.agents[2].components.situation_perception_history_length === 40
    && c.agents[2].components.person_by_situation_history_length === 30,
    'las ventanas de memoria no llegaron a components');
chk(c.agents[0].components === null && c.agents[0].available_actions === null,
    'un agente sin ajustes deberia quedar en null, no con objetos vacios');
// El perfil psicologico tiene que llegar con la forma exacta que espera cada fabrica
const cp = c.agents[2].components;
chk(cp.cognitive_bias && cp.cognitive_bias.bias_type === 'anchoring_bias'
    && cp.cognitive_bias.bias_strength === 'strong', 'el sesgo no se genero bien');
chk(cp.emotion && cp.emotion.current_emotion === 'cautela', 'la emocion no se genero bien');
chk(cp.personality_traits && cp.personality_traits.conscientiousness === 5
    && cp.personality_traits.neuroticism === 4
    && !('openness' in cp.personality_traits),
    'los rasgos vacios no deberian viajar, y los puestos deben ser numeros');
chk(cp.values && cp.values.core_values.length === 2
    && cp.values.value_conflict === 'cautela vs urgencia', 'los valores no se generaron bien');
chk(!('cognitive_bias' in (c.agents[0].components || {})),
    'un agente sin perfil no deberia llevar bloques psicologicos');
chk(c.agents.every(a => !/[^\x00-\x7F]/.test(a.id)), 'algun id quedo con acentos');

console.log('');
if (fallas.length) { fallas.forEach(f => console.log('  FALLA: ' + f)); process.exit(1); }
console.log('  todas las comprobaciones pasaron');
