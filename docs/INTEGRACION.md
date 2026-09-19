# Integrar la simulación real con la plataforma

Guía para quien escriba el paletizado de verdad —con percepción y planificador— y
quiera que aparezca en la plataforma igual que aparece ésta.

La versión guionizada de este repo ya recorre el contrato entero y está probada contra
Supabase. **Es la implementación de referencia**: `src/pallet/telemetry.py` son 190
líneas y la mitad son comentarios. Cópialo y cambia solo lo que se marca abajo.

---

## 1. Lo que hay que instalar

```bash
pip install -e <ruta>/Platform/backend          # rama feature/backend-hardening
```

Comprueba que trae el ciclo de vida, porque otras ramas no:

```python
from theker_telemetry import RunLog
assert hasattr(RunLog, "begin"), "SDK sin ciclo de vida: te falta la rama buena"
```

Credenciales en un `.env` del repo o de su carpeta padre (ver `.env.example`).

---

## 2. Las cinco llamadas

```python
log = RunLog(repo, task="palletizing", level=2, oracle=False,
             motion_speed=1.0, tag="real", label="planificador-v1",
             n_episodes=n, remote=True)

for seed in semillas:
    log.begin(seed, n_objects=len(paquetes))     # nace 'running'

    # ... y durante el episodio, según se mide cada cosa:
    log.event(ts=…, seq=…, kind=…, package_id=…, payload={…})
    log.placement(seq=…, package_id=…, package_type=…, …)
    log.pallet_state(after_seq=…, mass_kg=…, cog_x=…, …)

    log.end(EpisodeResult(...))                  # escribe el jsonl y cierra
log.close()
```

`event`, `placement` y `pallet_state` son `**kwargs` puros: **cada clave es una
columna**, sin validación. Un nombre mal escrito es un 400 que el SDK se traga, y a
partir de ahí la subida queda apagada para el resto de la ejecución.

**No mezcles `episode()` con `begin()`** en el mismo episodio: insertaría la fila dos
veces y el `unique(run_id, seed)` tumba la segunda. `episode()` sigue existiendo, pero
es para el backfill y el sembrado.

---

## 3. Lo único que cambia respecto a la guionizada

| | guionizada | real |
|---|---|---|
| `oracle` | `True` — las poses se conocen | **`False`** — se ven por cámara |
| `perceive` payload | `seen` = las que faltan, `confidence` 1.0 | lo que de verdad detectó y su confianza |
| `plan` payload | el hueco del guion | el hueco que decidió el planificador |

Todo lo demás es idéntico. Y `oracle` importa: la interfaz no compara un run con oracle
contra uno sin él, así que ponerlo mal invalida justo la comparación que justifica el
trabajo.

---

## 4. Las cinco trampas

**Unidades cruzadas.** Las columnas van en SI; los `payload` de eventos, en mm y grados.
Contradice la regla general del proyecto, y es la que más se cuela.

```
perceive  {seen, confidence}          pick    {mass_kg, type}
plan      {layer, slot}               settle  {layer, drift_mm}
place     {error_xy_mm, error_yaw_deg, overhang_mm}
fail      {cause}
```

Esos nombres son cerrados: si falta uno, la interfaz **no enseña nada y no da error**.

**`seq` es único por episodio, no por paquete.** Hay cinco eventos por caja. Un contador
del episodio entero (`len(eventos)`) lo resuelve; el índice del paquete, no.

**Una fila de `pallet_states` por paquete INTENTADO**, incluido el que derrumba el
montón. Si solo emites los que salieron bien, la traza de CoG acaba en verde en un
episodio que se cayó y el gráfico deja de servir para lo que existe.

**El centro de gravedad cuenta las cajas fuera de tolerancia.** Una caja mal puesta sigue
pesando. Lo que NO cuenta es la que se quedó en la mesa: el criterio es geométrico
—¿su huella toca el palé?—, no "¿falló la maniobra?". Ver `measure.on_pallet`.

**El margen de estabilidad se mide contra el polígono de soporte**, que es la envolvente
de las huellas de la capa 1, no contra el borde del palé. Contra el palé los números
salen optimistas y la pantalla dice que todo va bien hasta el derrumbe.

---

## 5. Vocabulario de fallos: 8 valores, cerrado

```
no_detection  ik_unreachable  collision  grasp_slip
wrong_placement  timeout  stack_collapse  overhang_violation
```

`EpisodeResult` lanza `ValueError` con cualquier otro, a propósito: así te enteras en tu
repo y no al ver la interfaz vacía. Añadir uno obliga a tocar tres sitios en Platform
(el Python, el CHECK de SQL y el texto del front) y hay un test que lo vigila: pídeselo
a quien lleve el backend.

---

## 6. Lo que NO tienes que hacer

- **No gestiones errores de red.** El SDK avisa una vez, apaga la subida y sigue. Un
  `try/except` tuyo encima solo esconde el aviso.
- **No escribas `synthetic`.** Es para datos sembrados.
- **No repitas `git_sha` ni `oracle` por episodio.** Van en el run.
- **No calcules `status`.** Sale de `EpisodeResult.success`.

---

## 7. Lo que todavía no soporta la plataforma

Si tu simulación los produce, guárdalos en disco y pídelos; no los metas en otra tabla.

- **Imágenes.** No hay bucket, ni tabla `snapshots`, ni `RunLog.snapshot()`. Aquí las dos
  vistas del palé se guardan en `runs/<ts>/<seed>/`.
- **`runs.config`.** `RunLog.__init__` no lo acepta todavía, así que el tamaño real del
  palé viaja de momento en `episodes.metrics` (`pallet_size_m`, `pallet_scale`). Importa
  si trabajas a escala: sin ese dato la interfaz dibuja suponiendo un europeo de
  1200×800.
- **`stability_margin`** está duplicada entre `seed/palletizing.py` de Platform y
  `src/pallet/measure.py` de aquí. Si la implementas por tercera vez, ancla los valores
  con un test.

---

## 8. Comprobación, en este orden

1. **Sin red.** `python tests/test_pallet.py` como modelo: que las claves de las filas
   sean las de las columnas y que no se repitan los `seq`. Falla en segundos, no tras
   tres minutos de simulación.
2. **Sin Supabase.** Un episodio entero a disco. Mira el `episodes.jsonl`.
3. **Con `--telemetry`, y que falle alto si no hay credenciales.** El SDK trata "sin
   credenciales" como su modo normal: si no lo compruebas tú, un `.env` mal puesto se
   traga la subida en silencio.
4. **Con `/` abierto en el navegador.** Es la prueba de verdad:
   - el episodio aparece **en curso** al arrancar, sin recargar;
   - el palé se monta paquete a paquete y los KPIs se mueven solos;
   - al acabar pasa a terminado con su causa de fallo o su éxito.
5. **Contra la base**, después:

```sql
-- una fila de traza por paquete intentado
select count(*) from pallet_states where episode_id = '<id>';
-- vacío: ningún seq repetido
select seq from events where episode_id = '<id>' group by seq having count(*) > 1;
-- el cierre llegó
select status, ended_at from episodes where id = '<id>';
```

Si el paso 4 no se mueve, casi seguro estás llamando a `episode()` en vez de a
`begin()`/`end()`: sin una fila `running` y sin el `UPDATE` final, dos de las tres
suscripciones de la pantalla no se disparan nunca.
