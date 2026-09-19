# Paletizado guionizado

Un brazo robótico coge 10 cajas de una mesa y las apila en un palé: las grandes abajo,
las medianas encima, cada capa plana. Simulado en MuJoCo, con un Franka Panda.

**Nada de esto lo decide un algoritmo.** Qué caja va a qué hueco está escrito a mano en
`configs/pallet.yaml` y el puzle encaja, como una partida de Tetris ya resuelta. No hay
percepción, no hay planificador, no hay nada que se pueda equivocar eligiendo.

Lo que **sí** es real es la física — y por tanto todo lo que se mide.

## Para qué existe

Para que la plataforma de observabilidad reciba episodios de paletizado **de verdad**
desde el primer día, sin esperar a que exista el sistema que los producirá.

El plan está guionizado; la medida, no. El brazo agarra de verdad, las cajas caen y se
asientan de verdad, y cada número que se sube sale de medir el estado del simulador:
el error contra el hueco planificado, cuánto apoya cada caja, cuánto sobresale, dónde
queda el centro de gravedad y con qué margen aguantaría el transporte. Eso hace que
estos episodios valgan como línea base aunque el guion sea un puzle resuelto.

Cuando llegue el pipeline real, sustituye el guion y no toca nada más.

## Arranque

```bash
bash scripts/setup.sh
source .venv/bin/activate

python scripts/palletize.py --viewer --speed 3   # verlo montarse
python scripts/palletize.py --viewer --hold      # y dejar la ventana abierta al acabar
python scripts/palletize.py -n 3                 # medirlo, headless
python scripts/palletize.py -n 3 --telemetry     # y subirlo a Supabase
python tests/test_pallet.py                      # comprobaciones
```

`--speed` es el ritmo al que **ves** el visor; no toca al robot. La velocidad del brazo
es `--motion-speed`, y por encima de x1 la caja se escurre de la pinza: la tabla de
medidas está en `configs/pallet.yaml`.

## Qué produce

Cada ejecución deja en `runs/<timestamp>-pallet/`:

```
episodes.jsonl        una línea JSON por episodio: el resultado y sus métricas
<seed>/top.png        cenital del palé terminado
<seed>/front.png      alzado
```

Para subir hace falta un `.env` con las credenciales: copia `.env.example`. Sin él la
simulación corre igual y escribe en `runs/`, pero `--telemetry` falla en voz alta en vez
de tragárselo.

Con `--telemetry` se replica a Supabase **en vivo**: el episodio nace en curso y las
filas salen según se miden, así que la pantalla Live de la plataforma enseña el palé
montarse paquete a paquete en vez de aparecer ya montado. El `jsonl` en disco sigue
siendo la fuente de verdad; si la red falla, el episodio no se entera.

Las dos imágenes **también se suben**: van a Supabase Storage (bucket `snapshots`,
público) y sus URLs viajan en `episodes.metrics` como `snapshot_top_url` y
`snapshot_front_url`. Su sitio natural sería una tabla `snapshots` con su `after_seq`,
que la plataforma todavía no tiene; moverlas después es cambiar de dónde las lee el
front, no volver a producirlas.

## Cómo está organizado

```
configs/scene.yaml       la celda: brazo, mesa y velocidades
configs/pallet.yaml      el palé, el catálogo de cajas y EL GUION
src/cell.py              mesa, cámaras y convención de agarre
src/control/arm.py       IK diferencial (mink) -> actuadores
src/pallet/scene.py      construcción de la escena y las dos vistas
src/pallet/measure.py    la medida sobre el estado real del simulador
src/pallet/episode.py    la maniobra y el bucle del episodio
src/pallet/telemetry.py  ÚNICO sitio que sabe que la plataforma existe
tests/test_pallet.py     15 comprobaciones, sin framework ni simulador
```

## Resultados (19/09/2026)

10 cajas, 2 capas, palé de 210 × 140 mm (maqueta 1:5.7 de un europeo: la pinza del
Panda abre 80 mm y un palé de verdad es inagarrable).

| | |
|---|---:|
| Colocadas | 10/10 |
| Error de colocación, medio · máximo | 2.6 mm · 5.6 mm |
| Planitud de la capa superior | 0.1 mm |
| Voladizo máximo | 0.2 mm |
| CoG respecto al centro del palé | 2.2 mm |
| Margen de estabilidad al terminar | +58 mm |
| Ocupación del palé | 77 % |
| Duración | 199 s simulados |

Repetible: el guion es fijo y MuJoCo determinista, así que la misma semilla da el mismo
palé. Eso es lo que hace comparables dos commits.

## Para el paletizado de verdad

Cuando exista la simulación real —con percepción y planificador—, `docs/INTEGRACION.md`
dice cómo enchufarla a la plataforma igual que ésta: las cinco llamadas, lo único que
cambia, las cinco trampas y cómo comprobarlo.

## De dónde sale

La geometría, el control y la calibración vienen de la simulación de inducción del reto
THEKER Robotics (HackSpain '26), donde estaban mezcladas con su percepción y su
planificador. Aquí llegan sueltas. Los números medidos que justifican cada valor están
en los comentarios de `configs/pallet.yaml`, que es donde hay que mirar antes de tocar
nada: hay cuatro que costaron encontrar y que no se ajustan a ojo.
