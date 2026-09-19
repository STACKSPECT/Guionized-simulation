#!/usr/bin/env python
"""
Paletizado guionizado: el brazo monta un palé de 10 cajas en 2 capas.

    source .venv/bin/activate
    python scripts/palletize.py                  # un episodio, headless, a disco
    python scripts/palletize.py --viewer         # con ventana, a tiempo real
    python scripts/palletize.py -n 3 --telemetry # 3 episodios, y a Supabase

No hay percepción ni planificador: de dónde se coge cada caja y en qué hueco va está
escrito en `configs/pallet.yaml`, y el puzle encaja. Lo que SÍ es real es la física, y
por tanto todo lo que se mide: el error contra el hueco, cuánto apoya cada caja, dónde
queda el centro de gravedad y con qué margen aguantaría el transporte.

Para qué sirve, entonces: para que la plataforma de observabilidad reciba episodios de
paletizado de verdad —con sus imágenes— desde el primer día, y el pipeline que venga
después solo tenga que sustituir el guion.

Controles del visor: arrastrar = rotar cámara · rueda = zoom · ESC = salir.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Headless por defecto. Tiene que estar ANTES de importar mujoco: si no, en una máquina
# sin pantalla el import revienta y no hay episodio que valga.
if "--viewer" not in sys.argv:
    os.environ.setdefault("MUJOCO_GL", "egl")

from theker_telemetry import RunLog                                   # noqa: E402

from src.pallet.episode import run_episode                            # noqa: E402
from src.pallet.scene import build_scene, load_configs                # noqa: E402
from src.pallet.telemetry import (                                    # noqa: E402
    RunLogSink, episode_result, run_config, save_snapshots,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-n", "--episodes", type=int, default=1)
    parser.add_argument("--seed", type=int, default=1,
                        help="semilla del primer episodio; las siguientes van detrás")
    parser.add_argument("--viewer", action="store_true",
                        help="abre el visor interactivo (solo con un episodio)")
    parser.add_argument("--hold", action="store_true",
                        help="con --viewer, deja la ventana abierta al acabar para "
                             "mirar el palé. Entonces no termina hasta que la cierres")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="ritmo de REPRODUCCIÓN en el visor. No toca al robot")
    parser.add_argument("--motion-speed", type=float, default=1.0,
                        help="velocidad REAL del brazo, x veces la de configs/pallet.yaml. "
                             "Por encima de x1 la caja se escurre de la pinza: la tabla "
                             "de medidas está en pallet.yaml")
    parser.add_argument("--telemetry", action="store_true",
                        help="sube a Supabase además de escribir en disco")
    parser.add_argument("--label", default="paletizado-guion")
    args = parser.parse_args()

    if args.viewer and args.episodes != 1:
        parser.error("el visor es para mirar un episodio, no una tanda: usa -n 1")

    scene_cfg, pallet_cfg = load_configs(REPO)
    scene = build_scene(scene_cfg, pallet_cfg, repo=REPO)
    log = RunLog(
        REPO, task="palletizing",
        level=int(pallet_cfg["episode"]["level"]),
        # Sin percepción: las poses se conocen de antemano. La interfaz marca estos runs
        # y no los compara con los que sí miran imágenes.
        oracle=True,
        motion_speed=args.motion_speed,
        tag="pallet", label=args.label,
        # El tamaño real del palé. La interfaz dibuja a escala y no puede deducirlo.
        config=run_config(scene),
        n_episodes=args.episodes,
        remote=args.telemetry,
    )

    # Pedir `--telemetry` y quedarse sin ella es un fallo, no un modo de trabajo. El SDK
    # trata "no hay credenciales" como lo normal —correr sin Supabase es su defecto— y
    # con eso un flag mal puesto, o un `.env` que no está donde se busca, se tragan sin
    # decir nada: no te enteras hasta abrir la interfaz y verla vacía.
    if args.telemetry and not log.run_id:
        print("ERROR: --telemetry pero no hay ejecución abierta en Supabase.\n"
              "  Comprueba SUPABASE_URL y SUPABASE_SERVICE_KEY. Se leen del .env de\n"
              f"  este repo ({REPO}/.env) o del de su carpeta padre\n"
              f"  ({REPO.parent}/.env). Si el problema fuera otro, el SDK lo habrá\n"
              "  impreso justo encima de esta línea.")
        return 1

    dims = pallet_cfg["pallet"]["dims"]
    # Se dice "por episodio" a propósito: leer "10 cajas · 1 episodio" y entender que
    # ha corrido 1 de 10 es fácil, y son dos cuentas distintas.
    print(f"palé {dims[0] * 1000:.0f} x {dims[1] * 1000:.0f} mm · "
          f"{len(scene.boxes)} cajas por episodio · "
          f"{args.episodes} episodio{'s' if args.episodes != 1 else ''} a correr"
          + ("  (usa -n para más)" if args.episodes == 1 else ""))

    ok = 0
    for i in range(args.episodes):
        seed = args.seed + i
        # Escena nueva por episodio: reutilizarla dejaría el palé ya montado. La primera
        # se aprovecha, que compilar el modelo no es gratis.
        if i:
            scene = build_scene(scene_cfg, pallet_cfg, repo=REPO)
        print(f"\nsemilla {seed}")

        # Con Supabase configurado se escribe EN VIVO: `begin()` abre el episodio en
        # curso y las filas van saliendo según se miden, que es lo que hace que la
        # pantalla enseñe el palé montarse en vez de aparecer ya montado.
        sink = RunLogSink(log, scene) if log.run_id else None
        if sink is not None:
            sink.begin(seed)

        episode = _run(scene, seed, args, sink)
        if episode is None:
            return 0                              # visor cerrado a mano

        # El disco siempre, y antes que la red: las dos fotos quedan en runs/ pase lo
        # que pase, y `sink.end()` las sube además a Storage.
        save_snapshots(episode, log.directory / str(seed))
        if sink is not None:
            sink.end(episode)                     # escribe el jsonl y cierra el episodio
        else:
            log.writer.write(episode_result(episode, scene))

        state = episode.states[-1] if episode.states else None
        ok += episode.success
        print(f"  {episode.n_placed}/{episode.n_objects} colocadas · "
              f"{episode.duration_s:.0f} s · "
              + (f"margen {state.stability_margin * 1000:+.0f} mm · "
                 f"llenado {state.fill_ratio:.0%} · " if state else "")
              + ("ÉXITO" if episode.success else f"fallo: {episode.failure}"))

    log.close()
    print(f"\n{ok}/{args.episodes} episodios con éxito")
    print(f"disco: {log.directory}")
    if log.path_in_ui:
        print(f"interfaz: {log.path_in_ui}")
    return 0 if ok == args.episodes else 1


def _run(scene, seed: int, args, sink=None):
    """Un episodio, con visor o sin él. `None` si cierran la ventana."""
    if not args.viewer:
        return run_episode(scene, seed=seed, speed=args.motion_speed,
                           verbose=True, sink=sink)

    import mujoco.viewer

    # El visor se abre sobre un MjModel concreto, así que tiene que ser exactamente el
    # que se simula: por eso la escena se construye fuera y se le pasa al episodio.
    with mujoco.viewer.launch_passive(scene.model, scene.data) as viewer:
        started = time.time()

        def sync() -> None:
            if not viewer.is_running():
                raise KeyboardInterrupt
            viewer.sync()
            wait = scene.data.time / args.speed - (time.time() - started)
            if wait > 0:
                time.sleep(wait)

        try:
            episode = run_episode(scene, seed=seed, speed=args.motion_speed,
                                  on_step=sync, verbose=True, sink=sink)
        except KeyboardInterrupt:
            print("visor cerrado")
            return None

        # Por defecto el visor se cierra al acabar y el programa termina. Con `--hold`
        # se queda abierto para poder mirar el palé montado; entonces NO termina hasta
        # que lo cierres, que es justo lo que se espera de un flag que se llama así.
        # Las dos fotos retratan ese mismo instante, así que no hace falta para verlo.
        if args.hold:
            print("  palé terminado · ESC o cerrar la ventana para seguir")
            while viewer.is_running():
                viewer.sync()
                time.sleep(1 / 60)
        return episode


if __name__ == "__main__":
    raise SystemExit(main())
