# Integrar la simulación real con la plataforma

Guía para quien escriba el paletizado de verdad —con percepción y planificador— y quiera
que aparezca en la plataforma igual que aparece ésta.

La guionizada de este repo ya recorre el contrato entero y está probada contra Supabase:
episodio en vivo, traza de centro de gravedad, fotos por capa y cierre limpio. **Es la
implementación de referencia.** `src/pallet/telemetry.py` son ~180 líneas, la mitad
comentarios, y es el único módulo que sabe que la plataforma existe. Cópialo y cambia
solo lo que se marca en §4.

Todo lo que hay aquí está medido contra el Supabase real, no deducido.

---

## 1. Instalación

El SDK **no vive en tu repo**: es el contrato de lo que la plataforma guarda, y quien
define la forma del dato es quien lo almacena.

```bash
pip install -e <ruta>/Platform/backend      # rama dev
```

Tiene que ser `dev` o algo que salga de ella. Compruébalo, porque las ramas viejas no lo
traen y el fallo no aparece al instalar sino al correr:

```python
from theker_telemetry import RunLog
for m in ("begin", "end", "snapshot"):
    assert hasattr(RunLog, m), f"SDK sin {m}(): necesitas la rama dev de Platform"
```

Credenciales en un `.env` del repo o de su carpeta padre (ver `.env.example`).
`SUPABASE_SERVICE_KEY` se salta el RLS y es la única que puede escribir: solo lado
Python, nunca en un frontend.

---

## 2. El ciclo de vida, entero

```python
from theker_telemetry import EpisodeResult, RunLog

log = RunLog(repo, task="palletizing", level=2,
             oracle=False,                      # tú SÍ tienes percepción
             motion_speed=1.0, tag="real", label="planificador-v1",
             config={"pallet_size_m": [1.2, 0.8]},   # ver §7
             n_episodes=len(semillas), remote=True)

for seed in semillas:
    log.begin(seed, n_objects=len(paquetes))    # nace 'running': Live lo ve aparecer

    # ... durante el episodio, cada cosa en cuanto se mide:
    log.event(ts=…, seq=…, kind=…, package_id=…, payload={…})
    log.placement(seq=…, package_id=…, package_type=…, …)
    log.pallet_state(after_seq=…, mass_kg=…, cog_x=…, …)
    log.snapshot(after_seq=…, view="top", png=<bytes>, width=…, height=…)

    log.end(EpisodeResult(...))                 # escribe el jsonl y cierra con un PATCH

log.close()                                     # cierra el run: pone runs.ended_at
```

`event`, `placement`, `pallet_state` y `snapshot` son `**kwargs` puros: **cada clave es
una columna**, sin validación. Un nombre mal escrito es un 400 que el SDK se traga, y a
partir de ahí la subida queda apagada para el resto de la ejecución.

`snapshot()` tiene que ir **entre `begin()` y `end()`**: cuelga del episodio abierto, y
después de `end()` ya no hay de dónde colgarla.

**No mezcles `episode()` con `begin()`** en el mismo episodio: insertaría la fila dos
veces y el `unique(run_id, seed)` tumba la segunda. `episode()` sigue existiendo, pero es
para el backfill y el sembrado, que suben cosas ya terminadas.

---

## 3. Por qué el ciclo de vida y no `episode()`

Porque es lo único que hace que la pantalla Live esté viva. Se alimenta de tres señales y
dos solo existen si usas `begin()`/`end()`:

| Señal | Quién la produce |
|---|---|
| fila `episodes` con `status='running'` | `begin()` |
| `INSERT` en `events` y `pallet_states` | `event()`, `pallet_state()` |
| `UPDATE` de `episodes` al cerrar | `end()` |

Con `episode()` el episodio aparece ya terminado, de golpe, como si el palé se montara
solo. Es la diferencia entre enseñar un sistema vivo y enseñar una captura.

---

## 4. Lo único que cambia respecto a la guionizada

| | guionizada | real |
|---|---|---|
| `oracle` | `True` — las poses se conocen | **`False`** — se ven por cámara |
| `perceive` payload | `seen` = las que faltan, `confidence` 1.0 | lo que detectó y su confianza |
| `plan` payload | el hueco del guion | el hueco que decidió el planificador |
| causas de fallo | nunca `no_detection` | ya sí: la percepción puede no ver nada |

Todo lo demás es idéntico. `oracle` importa más de lo que parece: la interfaz **no
compara** un run con oracle contra uno sin él, así que ponerlo mal invalida justo la
comparación que justifica el trabajo.

---

## 5. Las siete trampas

Ninguna se ve leyendo el SDK. Todas costaron una ejecución perdida o un episodio roto.

**1 · Unidades cruzadas.** Las columnas van en SI; los `payload` de eventos, en mm y
grados. Contradice la regla general del proyecto, y es la que más se cuela.

```
perceive  {seen, confidence}          pick    {mass_kg, type}
plan      {layer, slot}               settle  {layer, drift_mm}
place     {error_xy_mm, error_yaw_deg, overhang_mm}
fail      {cause}
```

Esos nombres son cerrados: si falta uno, la interfaz **no enseña nada y no da error**.

**2 · `seq` es único por episodio, no por paquete.** Hay cinco eventos por caja. Un
contador del episodio entero (`len(eventos)`) lo resuelve; el índice del paquete, no. Un
duplicado tumba la fila y, con ella, la subida del resto del episodio.

**3 · Una fila de `pallet_states` por paquete INTENTADO**, incluido el que derrumba el
montón y los que fallaron antes de depositar. Si solo emites los que salieron bien, la
traza de CoG acaba en verde en un episodio que se cayó, y el gráfico deja de servir para
lo único que existe: dejar ver venir el fallo varias colocaciones antes.

**4 · El centro de gravedad cuenta las cajas fuera de tolerancia.** Una caja mal puesta
sigue pesando y sigue moviendo el CoG. Lo que NO cuenta es la que se quedó en la mesa: el
criterio es geométrico —¿su huella toca el palé?—, no "¿falló la maniobra?". Ver
`measure.on_pallet`. Este repo tuvo ese fallo, y lo cubre
`test_the_cog_counts_boxes_outside_tolerance`.

**5 · El margen de estabilidad se mide contra el polígono de soporte**, la envolvente de
las huellas de la capa 1, no contra el borde del palé. Contra el palé los números salen
optimistas y la pantalla dice que todo va bien hasta el derrumbe.

**6 · `snapshots.view` es vocabulario cerrado:** `top | side | iso | camera`. Con otro
nombre el PNG sube a Storage y **luego** la base rechaza la fila con un `23514`: la foto
queda huérfana y la traza sin imagen. A esta simulación le pasó con `front`.

**7 · Un episodio abierto hay que cerrarlo SIEMPRE.** Un `Ctrl-C`, una ventana cerrada o
un proceso muerto dejan la fila en `running` para siempre. Y `getLatestEpisode` del front
elige el primer episodio en ese estado **sin ordenar**, así que un solo huérfano deja
Live clavada ahí indefinidamente. Mete el bucle en `try/finally`:

```python
try:
    ...
finally:
    if log.episode_id:
        log.end(EpisodeResult(..., success=False, failure=None))
    log.close()
```

`failure=None` es correcto: no hay causa en el vocabulario que describa "se cerró la
ventana", y `success=False` basta para que quede marcado.

---

## 6. Vocabulario de fallos: 8 valores, cerrado

```
no_detection  ik_unreachable  collision  grasp_slip
wrong_placement  timeout  stack_collapse  overhang_violation
```

`EpisodeResult` lanza `ValueError` con cualquier otro, a propósito: así te enteras en tu
repo y no al ver la interfaz vacía. Añadir uno obliga a tocar tres sitios en Platform (el
Python, el `CHECK` de SQL y el texto del front) y hay un test que lo vigila: pídeselo a
quien lleve el backend, no lo inventes.

---

## 7. Imágenes, geometría y el tamaño del palé

Las tres las cerró la plataforma en `dev`. Úsalas, no las reinventes.

**Imágenes.** `log.snapshot(after_seq=…, view=…, png=<bytes>, width=…, height=…)`: el SDK
sube el PNG a Storage y rellena la `url` sola. El `after_seq` casa con
`pallet_states.after_seq`, así que la foto y ese punto de la traza son el mismo instante
— aprovéchalo y saca una por capa, no solo al final.

Dos cosas que aprendimos sacándolas:

- **Aparta el brazo o sale el dorso de la mano**, no la carga: acaba justo encima del
  palé, que es donde estaba soltando.
- **Apártalo en cartesiano, no en espacio de juntas.** En juntas el recorrido no está
  controlado y barre el montón recién colocado: medido, esta simulación pasaba de 10/10 a
  4/10 con `overhang_violation` en cuanto se metió la foto por capa.

**Tamaño del palé.** `RunLog(config={"pallet_size_m": [x, y], ...})`. La interfaz dibuja
a escala real y `palletSize()` lo busca ahí primero. Sin él supone un europeo de
1200×800; si trabajas con una maqueta, todas las cotas salen mal por el factor de escala.
En `config` cabe lo que quieras describir de la celda: velocidades, capas, escala.

**Geometría.** `from theker_telemetry import stability_margin, support_polygon,
TRANSPORT_ACCEL_G`. Es el indicador que define toda la interfaz; una tercera copia
acabaría discrepando.

---

## 8. Lo que NO tienes que hacer

- **No gestiones errores de red.** El SDK avisa una vez, apaga la subida y sigue; el
  `jsonl` en disco es la fuente de verdad. Un `try/except` tuyo encima esconde el aviso.
  La excepción es si haces algo caro antes de subir: que su fallo no arrastre al episodio.
- **No escribas `synthetic`.** Es para datos sembrados y lo pone el sembrado.
- **No repitas `git_sha` ni `oracle` por episodio.** Van en el run; las vistas los
  recuperan por el join.
- **No calcules `status`.** Sale de `EpisodeResult.success`.
- **No dupliques el esquema en tu repo.** Una sola frontera, como
  `src/pallet/telemetry.py`: el resto del código no debería conocer el nombre de ninguna
  columna.

---

## 9. Cómo comprobar que funciona, en este orden

**1. Sin red ni simulador.** Copia el enfoque de `tests/test_pallet.py`: que las claves de
cada fila sean las columnas de su tabla, que los `seq` no se repitan y que las vistas
estén en el vocabulario. Falla en segundos, no tras tres minutos de simulación.

**2. Sin Supabase.** Un episodio entero a disco; mira el `episodes.jsonl`.

**3. Con telemetría, y que diga en qué modo va.** El SDK trata "sin credenciales" como su
modo normal, así que un `.env` mal puesto se traga la subida en silencio. Imprime siempre
si está activa o no — no imprimirlo es lo que aquí costó varias ejecuciones enteras.

**4. Con `/` abierto en el navegador.** Es la prueba de verdad:

- el episodio aparece **en curso** a los pocos segundos;
- el palé se monta paquete a paquete y los KPIs se mueven solos;
- al acabar pasa a terminado con su éxito o su causa de fallo.

**5. Contra la base**, después:

```sql
-- una fila de traza por paquete intentado
select count(*) from pallet_states where episode_id = '<id>';
-- vacío: ningún seq repetido
select seq from events where episode_id = '<id>' group by seq having count(*) > 1;
-- el cierre llegó
select status, ended_at from episodes where id = '<id>';
-- las fotos, y que su after_seq case con la traza
select after_seq, view, width, height from snapshots
 where episode_id = '<id>' order by after_seq, view;
-- basura de ejecuciones abortadas: tiene que ser 0
select count(*) from episodes where status = 'running';
```

---

## 10. Si la interfaz no reacciona

Antes de buscar en tu código, descarta esto:

| Síntoma | Causa probable |
|---|---|
| No aparece nada, nunca | No estás subiendo. ¿Hay `.env`? ¿Imprimiste el modo? |
| El palé aparece ya montado | Estás usando `episode()` en vez de `begin()`/`end()` |
| Live clavada en un episodio viejo | Un huérfano en `running`; ver trampa 7 |
| La ejecución no sale en Ejecuciones | Esa pantalla **no se refresca sola**: `runs` no está en Realtime y no tiene `refetchInterval`. Recarga |
| Las cajas salen diminutas en un palé enorme | Falta `config.pallet_size_m` |
| La foto no está pero el PNG sí | `view` fuera de `top\|side\|iso\|camera` |

Realtime está publicado sobre `events`, `pallet_states`, `episodes` y `snapshots` —
**`runs` no**. Live pregunta por un episodio nuevo cada 5 s; la pantalla Ejecuciones no
pregunta nunca.
