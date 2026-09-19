# AGENTS.md

Contexto permanente de este repositorio. Léelo entero antes de escribir código. Si algo
aquí contradice lo que crees saber, gana este documento; si algo está desactualizado,
corrígelo en el mismo PR.

---

## 1. Qué es esto

Una simulación de paletizado **guionizada**: un brazo apila 10 cajas en un palé, las
grandes abajo y las medianas encima, con cada capa plana.

**No resuelve el paletizado y no pretende hacerlo.** Qué caja va a qué hueco está
escrito en `configs/pallet.yaml`. No hay percepción ni planificador. Si te piden
"mejorar cómo elige", lo que hay que hacer es sustituir el guion por un planificador de
verdad, no retocar el guion.

Existe para **fijar el contrato con la plataforma de observabilidad y recorrerlo entero**
antes de que exista el sistema real. La física sí es real, y por eso las métricas valen.

## 2. La distinción que lo explica todo

| | |
|---|---|
| **Guionizado** | qué caja se coge, en qué orden y en qué hueco va |
| **Medido** | dónde acaba cada caja, cuánto apoya, cuánto sobresale, el centro de gravedad, el margen de estabilidad, la deriva al asentarse |

Por eso el run se marca `oracle = true`: las poses se conocen de antemano, no se ven. La
interfaz de la plataforma ya sabe que un run así no se compara con uno que sí mira
imágenes, y lo pinta con trama para que nadie los confunda.

## 3. Puesta en marcha

```bash
bash scripts/setup.sh          # venv + dependencias + SDK + modelo del brazo
source .venv/bin/activate

python scripts/palletize.py --viewer          # verlo
python scripts/palletize.py -n 3              # medirlo, headless
python scripts/palletize.py -n 3 --telemetry  # y subirlo
python tests/test_pallet.py                   # comprobaciones
python -m src.pallet.measure                  # la medida, con sus asserts
```

El SDK `theker_telemetry` **no vive aquí**: es el contrato de lo que la plataforma
guarda, y quien define la forma del dato es quien lo almacena. `setup.sh` lo instala
editable desde el repo Platform y comprueba que trae `RunLog.begin`: solo la rama
`feature/backend-hardening` tiene el ciclo de vida del episodio.

Las credenciales de Supabase se leen del `.env` del repo o del de su carpeta padre.

## 4. Reglas duras

**El disco manda.** El `episodes.jsonl` de `runs/` es la fuente de verdad y Supabase una
réplica. Un fallo de red avisa una vez y el episodio sigue. No envuelvas las llamadas de
telemetría en try/except: el SDK ya lo hace, y hacerlo dos veces esconde el aviso.

**Se escribe en vivo, no de golpe.** `begin()` abre el episodio en curso,
`event()`/`placement()`/`pallet_state()` sueltan filas según se miden, `end()` lo cierra
con un `PATCH`. Ése es todo el motivo por el que la pantalla Live está viva: con
`episode()` no existe nunca una fila `running` ni llega el `UPDATE` de cierre, y dos de
las tres suscripciones no se disparan jamás. **No mezcles los dos caminos**: llamar a
`episode()` y a `begin()` en el mismo episodio duplica la fila y el
`unique(run_id, seed)` tumba la segunda.

**Las columnas van en SI y los `payload` de eventos en mm y grados.** Es la incoherencia
que más fácil se cuela, porque contradice la regla general. Y los nombres de esas claves
son un vocabulario cerrado: si falta una, la interfaz **no enseña nada y no da error**.

```
perceive  {seen, confidence}          pick    {mass_kg, type}
plan      {layer, slot}               settle  {layer, drift_mm}
place     {error_xy_mm, error_yaw_deg, overhang_mm}
fail      {cause}
```

**`seq` es único por episodio, no por paquete.** Hay cinco eventos por caja. Un
duplicado no se pierde solo: tumba la fila y, con ella, la subida del resto.

**Una fila de `pallet_states` por paquete INTENTADO**, incluido el que derrumba el
montón — es justo la que hace que la traza de CoG cruce el cero. Y el centro de gravedad
cuenta también las cajas fuera de tolerancia: una caja mal puesta sigue pesando. Este
repo tuvo ese fallo y la traza acababa en verde en episodios que se caían. Lo cubre
`test_the_cog_counts_boxes_outside_tolerance`.

**El vocabulario de fallos está cerrado**, 8 valores: `no_detection`, `ik_unreachable`,
`collision`, `grasp_slip`, `wrong_placement`, `timeout`, `stack_collapse`,
`overhang_violation`. `EpisodeResult` lanza `ValueError` con cualquier otro, a propósito.
Añadir uno obliga a tocar tres sitios en el repo de la plataforma: pídeselo a quien lo
lleve, no lo inventes aquí.

**`synthetic` no lo escribe esta simulación.** Es para datos sembrados. `git_sha` y
`oracle` van en el run y no se repiten por episodio.

## 5. Los cuatro números que costaron encontrar

Están medidos y razonados en la cabecera de `configs/pallet.yaml`. No se ajustan a ojo,
y `tests/test_pallet.py` comprueba que se siguen cumpliendo sin arrancar el simulador.

- **El brazo va a un cuarto de la velocidad nominal** (`cartesian_speed` 0.0625). Por
  encima, la caja se escurre de la pinza y llega 15-20 mm desplazada. No es el peso
  —probado de 0.03 a 0.13 kg, mismo deslizamiento—: es la aceleración del tramo.
- **Todo el trayecto va en cartesiano a una altura de tránsito fija.** El rodeo en
  espacio de juntas por `home` tira la caja al ir, y al volver barre la que se acaba de
  soltar.
- **23 mm entre cajas vecinas en el eje de cierre.** Los impone el dedo, no la caja:
  10.5 mm de grueso más lo que se abre al soltar. Es también el techo de ocupación del
  palé (72-76 %) y el argumento más claro para pasarse a ventosa.
- **La pinza no se abre del todo para soltar**, solo 3 mm por lado; se abre entera ya en
  alto y lejos del montón.

## 6. Qué NO hacer

- **No metas lógica de decisión en el guion.** Si empieza a elegir, deja de ser esta
  simulación y hay que decirlo en voz alta.
- **No dupliques el esquema de la plataforma aquí.** `src/pallet/telemetry.py` es la
  única frontera; el resto del código no conoce el nombre de ninguna columna.
- **No toques `configs/` a ojo.** Cada valor raro tiene su medida al lado.
- **No añadas dependencias** sin mirar antes si MuJoCo o numpy ya lo hacen.

## 7. Deuda conocida

- **`stability_margin` está duplicada.** El original vive en `seed/palletizing.py` de la
  plataforma, que no es importable desde este entorno. Lo que impide que discrepen es el
  test que ancla los valores. Pedido: subirla a `theker_telemetry/pallet.py`.
- **Las dos imágenes del palé no se suben.** La plataforma no tiene bucket, ni tabla
  `snapshots`, ni `RunLog.snapshot()`. Se guardan en `runs/<ts>/<seed>/` y enchufarlas
  después son unas pocas líneas en `telemetry.py`.
- **El tamaño real del palé viaja en `episodes.metrics`** (`pallet_size_m`,
  `pallet_scale`) y su sitio es `runs.config`, pero `RunLog` todavía no deja mandarlo.
  Importa: sin ese dato la interfaz dibuja esta maqueta de 210×140 mm como un europeo
  de 1200×800 y todas las cotas salen 5.7 veces mal.
