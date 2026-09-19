"""
El episodio de paletizado: recorrer el guion y medir lo que hace la física.

No hay percepción ni planificación. De dónde se coge cada caja y en qué hueco va está
escrito en `configs/pallet.yaml`, y este módulo se limita a ejecutarlo con el brazo y a
medir el resultado. Todo lo que se sube a la plataforma sale de esa medida.

Todo el trayecto va en cartesiano a una altura de tránsito fija: subir recto, cruzar,
bajar recto. El rodeo en espacio de juntas por `home` que usan otras celdas aquí hace
daño dos veces —con una caja cogida la tira, y de vuelta del palé la mano barre la que
se acaba de soltar— y no aporta nada, porque todas las poses comparten orientación.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import mujoco
import numpy as np

from src.control.arm import GRIPPER_CLOSED, GRIPPER_OPEN, ArmController
from src.pallet import measure
from src.pallet.scene import Box, PalletScene, planned_pose, render, tcp_frame

# La pinza cierra sobre su eje Y. Las cajas esperan sin girar y se depositan sin girar,
# así que la dirección de cierre es siempre la misma y no hay nada que decidir: es
# exactamente lo que significa que el guion esté resuelto de antemano.
CLOSING_DIR = np.array([0.0, 1.0, 0.0])


class Sink(Protocol):
    """
    Quien quiera enterarse de lo que ocurre MIENTRAS ocurre.

    Existe por la pantalla Live: la plataforma quiere las filas según se producen, no
    un volcado al final. Lo que pasa por aquí son objetos de dominio, no filas: este
    módulo no sabe cómo se llama ninguna columna y no tiene por qué. La traducción está
    en `src/pallet/telemetry.py`, que es la única frontera con la plataforma.
    """

    def placement(self, index: int, placement: measure.Placement) -> None: ...

    def pallet_state(self, index: int, state: measure.PalletState,
                     drift: float) -> None: ...

    def event(self, row: dict) -> None: ...

    def snapshot(self, shot: Snapshot) -> None: ...


@dataclass
class Snapshot:
    """Una vista del palé en un instante concreto del episodio.

    `after_seq` es la colocación tras la que se tomó, y casa con el `after_seq` de la
    traza de CoG: la foto y ese punto del gráfico son el mismo momento.
    """

    after_seq: int
    view: str
    image: np.ndarray


@dataclass
class Episode:
    """Lo que produce un episodio: las medidas, la traza, los eventos y las fotos."""

    seed: int
    n_objects: int
    placements: list[measure.Placement] = field(default_factory=list)
    states: list[measure.PalletState] = field(default_factory=list)
    drifts: list[float] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    failure: str | None = None
    duration_s: float = 0.0

    @property
    def n_placed(self) -> int:
        return sum(p.placed for p in self.placements)

    @property
    def success(self) -> bool:
        return self.failure is None and self.n_placed == self.n_objects


def run_episode(scene: PalletScene, seed: int = 0, speed: float = 1.0,
                on_step: Callable[[], None] | None = None,
                verbose: bool = False, sink: Sink | None = None) -> Episode:
    """
    Monta el palé entero y devuelve lo medido.

    `seed` no cambia nada de la escena —el guion es fijo— pero identifica el episodio en
    la plataforma. Dos semillas sobre el mismo commit miden la misma tarea dos veces, y
    lo que las separa es el ruido del simulador, que también es un dato.

    `sink`, si se le pasa, se entera de cada medida en cuanto existe. Es lo que permite
    que la pantalla Live vea el palé montarse en vez de aparecer ya montado.
    """
    arm = ArmController(scene, on_step=on_step, speed=speed)
    episode = Episode(seed=seed, n_objects=len(scene.boxes))
    max_duration = float(scene.pallet["episode"]["max_duration_s"])
    settle_steps = int(scene.pallet["episode"]["settle_steps"])

    arm.set_gripper(GRIPPER_OPEN, hold_s=0.2)

    for index, box in enumerate(scene.boxes):
        if scene.data.time > max_duration:
            # Este paquete no se llega a intentar, así que no deja fila: `timeout` es
            # quedarse sin tiempo ANTES de tocarlo.
            episode.failure = "timeout"
            break

        _event(episode, scene, sink, "perceive",
               seen=episode.n_objects - index, confidence=1.0)
        _event(episode, scene, sink, "plan", box, layer=box.layer, slot=box.slot)

        # A partir de aquí el paquete cuenta como INTENTADO y deja su fila pase lo que
        # pase: si el agarre o el depósito fallan, la fila sale con `placed = false` y
        # el estado del palé queda como estaba. Sin ella, el intento que tumba el
        # episodio sería el único que no aparece en la tabla.
        failure = _pick(arm, scene, box)
        if failure:
            episode.failure = failure
            _record(episode, scene, box, index, 0.0, sink, verbose)
            break
        _event(episode, scene, sink, "pick", box,
               mass_kg=round(box.mass, 4), type=box.type_name)

        watched = [p.box for p in episode.placements] + [box]
        failure, drift = _place(arm, scene, box, index, settle_steps, watched)
        if failure:
            episode.failure = failure
            _record(episode, scene, box, index, drift, sink, verbose)
            break

        _record(episode, scene, box, index, drift, sink, verbose)
        if episode.failure:
            break

        # Al cerrar una capa, retrato. Así la interfaz ve el montón crecer en fotos y
        # no solo en números, y cada imagen tiene su punto en la traza de CoG.
        if index == _last_of_layer(scene, box.layer):
            _shoot(episode, scene, arm, index, settle_steps, sink)

    episode.duration_s = round(float(scene.data.time), 2)
    _finish(episode, scene, arm, settle_steps, sink)
    if episode.failure:
        _event(episode, scene, sink, "fail", cause=episode.failure)
    return episode


def _last_of_layer(scene: PalletScene, layer: int) -> int:
    """Índice de la última caja de esa capa en el guion."""
    return max(i for i, b in enumerate(scene.boxes) if b.layer == layer)


def _shoot(episode: Episode, scene: PalletScene, arm: ArmController, after_seq: int,
           settle_steps: int, sink: Sink | None, park_joints: bool = False) -> None:
    """
    Aparta el brazo, deja que el montón se pare y retrata el palé.

    Hay que apartarlo o la foto sale del dorso de la mano: el brazo acaba justo encima
    del palé, que es donde estaba soltando.

    **A media ejecución se aparta en CARTESIANO**, manteniendo la orientación que ya
    tiene. Hacerlo en espacio de juntas deja el recorrido sin controlar y barre el
    montón recién colocado: medido, el episodio pasaba de 10/10 a 4/10 con
    `overhang_violation` en cuanto se metió la foto por capa. Es el mismo motivo por el
    que no se pasa por `home` con una caja cogida.

    `park_joints` es para la foto FINAL, donde ya no queda nada que colocar y la pose de
    observación da un encuadre más limpio.
    """
    if park_joints:
        arm.move_joints(scene.scene_cfg["robot"]["observe_qpos"], duration_s=2.5)
    else:
        motion = scene.scene_cfg["motion"]
        park_x, park_y = motion["park_xy"]
        transit_z = float(motion["transit_height"])
        pose = _at_height(arm.tcp_pose(), transit_z)
        arm.move_to(pose)                                   # primero recto hacia arriba
        pose[0, 3], pose[1, 3] = float(park_x), float(park_y)
        arm.move_to(pose)                                   # y luego a un lado
    _settle(scene, settle_steps, arm)
    for view in scene.pallet["cameras"]:
        shot = Snapshot(after_seq=after_seq, view=view, image=render(scene, view))
        episode.snapshots.append(shot)
        if sink is not None:
            sink.snapshot(shot)


def _finish(episode: Episode, scene: PalletScene, arm: ArmController,
            settle_steps: int, sink: Sink | None) -> None:
    """
    Aparta el brazo, deja que el montón se pare del todo, vuelve a medir y fotografía.

    El brazo se retira porque si no acaba aparcado justo encima del palé y tapa la
    cenital entera: la foto sale del dorso de la mano, no de la carga.

    Y se vuelve a medir DESPUÉS de apartarlo, no antes, para que las cifras que se
    suben y la imagen que las acompaña sean del mismo instante. Si al retirarse
    descoloca algo, eso es parte del resultado y tiene que verse en los dos sitios.

    Lo que NO se toca es la traza de CoG: sus filas ya se fueron subiendo una a una
    según se colocaba cada paquete, y cada `after_seq` solo se puede escribir una vez.
    Reescribir aquí la última dejaría el disco diciendo una cosa y la base otra.
    """
    if episode.placements:
        final: list[measure.Placement] = []
        for i in range(len(episode.placements)):
            final.append(measure.measure_placement(scene, scene.boxes[i], i, final))
        if any(old.placed and not new.placed
               for old, new in zip(episode.placements, final)):
            episode.failure = episode.failure or "stack_collapse"
        episode.placements = final

    # Si el episodio acabó justo al cerrar una capa, esa foto YA es la del palé
    # terminado y repetirla chocaría contra el `unique(episode_id, after_seq, view)`.
    final = max(len(episode.placements) - 1, 0)
    if not any(s.after_seq == final for s in episode.snapshots):
        _shoot(episode, scene, arm, final, settle_steps, sink, park_joints=True)


# ─────────────────────────────────────────────────────────────────────────────
# La maniobra
# ─────────────────────────────────────────────────────────────────────────────

def _pick(arm: ArmController, scene: PalletScene, box: Box) -> str | None:
    """Coge la caja de su sitio de preparación. Devuelve la causa de fallo, o None."""
    transit_z = float(scene.scene_cfg["motion"]["transit_height"])
    grasp_z = float(scene.scene_cfg["table"]["height"]) + box.half_height
    grasp = tcp_frame([box.stage[0], box.stage[1], grasp_z], CLOSING_DIR)

    # Cruzar en alto y bajar recto. El brazo llega aquí desde encima del palé, y bajar
    # en diagonal sobre la mesa mete la mano en las cajas que aún están esperando.
    if not arm.move_to(_at_height(grasp, transit_z)):
        return "ik_unreachable"
    arm.set_gripper(GRIPPER_OPEN, hold_s=0.2)
    if not arm.move_to(grasp):
        return "ik_unreachable"

    arm.set_gripper(GRIPPER_CLOSED)
    if not arm.is_holding(box.grasp_width):
        return "grasp_slip"
    if not arm.move_to(_at_height(grasp, transit_z)):
        return "ik_unreachable"
    if not arm.is_holding(box.grasp_width):
        return "grasp_slip"              # se escurrió al levantar
    return None


def _place(arm: ArmController, scene: PalletScene, box: Box, index: int,
           settle_steps: int, watched: list[Box]) -> tuple[str | None, float]:
    """
    Deposita la caja en su hueco, deja que se asiente y mide la deriva del montón.

    La deriva se toma entre el instante de ABRIR la pinza y el final del asentamiento,
    no desde antes del traslado: si no, lo que se mide es el viaje de la caja desde la
    mesa, que son 20 cm, y el número deja de significar nada.
    """
    motion = scene.scene_cfg["motion"]
    transit_z = float(motion["transit_height"])
    target = planned_pose(scene.pallet, scene.pallet["script"][index], scene.deck_top)
    # La pinza sujeta la caja por su media altura, así que el TCP va donde tiene que ir
    # el CENTRO de la caja. Se suelta un poco por encima: bajar hasta el contacto mete
    # los dedos contra la capa de abajo.
    place_z = float(target[2]) + float(motion["drop_clearance"])
    place = tcp_frame([target[0], target[1], place_z], CLOSING_DIR)

    # Cruzar en alto hasta la vertical del hueco, y solo entonces bajar: llegar en
    # diagonal desde la mesa barre las cajas que ya están puestas.
    if not arm.move_to(_at_height(place, transit_z)):
        return "ik_unreachable", 0.0
    if not arm.move_to(place):
        return "ik_unreachable", 0.0

    before = measure.positions(scene, watched)
    _release(arm, scene, box)
    _settle(scene, settle_steps, arm)
    drift = measure.max_displacement(before, measure.positions(scene, watched))

    # Retirarse RECTO HACIA ARRIBA antes de nada. Salir en diagonal hacia la siguiente
    # caja arrastra la mano por encima del montón: medido, la caja recién soltada
    # acababa 75 mm de su sitio y el episodio moría en `stack_collapse`.
    if not arm.move_to(_at_height(place, transit_z)):
        return "ik_unreachable", drift
    # Ya en alto y lejos del montón, la pinza puede abrirse del todo sin barrer nada.
    arm.set_gripper(GRIPPER_OPEN, hold_s=0.2)
    return None, drift


def _release(arm: ArmController, scene: PalletScene, box: Box) -> None:
    """
    Abre la pinza lo justo para soltar la caja, no del todo.

    Es la diferencia entre montar el palé y tirarlo: abrir del todo mete el dedo en el
    hueco de la caja vecina. El porqué y los milímetros, en `pallet.yaml`.
    """
    max_open = float(scene.scene_cfg["robot"]["gripper_max_opening"])
    margin = float(scene.scene_cfg["motion"]["release_margin"])
    opening = min(max_open, box.grasp_width + 2.0 * margin)
    # El actuador va en 0-255 sobre el recorrido completo de la pinza.
    arm.set_gripper(GRIPPER_OPEN * opening / max_open)


def _at_height(pose: np.ndarray, z: float) -> np.ndarray:
    """La misma pose del TCP, a otra altura."""
    out = pose.copy()
    out[2, 3] = z
    return out


def _settle(scene: PalletScene, steps: int, arm: ArmController | None = None) -> None:
    """
    Avanza la física para que el montón se asiente.

    Medir antes de esto es medir la caja todavía cayendo, y el error de colocación
    saldría enorme por un motivo que no es el brazo.

    Con `arm` se pasa por su bucle de control, que es el que refresca el visor: si no,
    la ventana se congela justo en el instante más interesante, el de la caja cayendo.
    """
    if arm is not None:
        for _ in range(max(1, steps // arm.steps_per_tick)):
            arm.step_physics()
        return
    for _ in range(steps):
        mujoco.mj_step(scene.model, scene.data)


# ─────────────────────────────────────────────────────────────────────────────
# La medida
# ─────────────────────────────────────────────────────────────────────────────

def _record(episode: Episode, scene: PalletScene, box: Box, index: int,
            drift: float, sink: Sink | None, verbose: bool) -> None:
    """
    Mide el palé tras este intento, decide si hay fallo y se lo cuenta al sink.

    Se vuelven a medir TODAS las cajas, no solo la nueva: lo que avisa de un derrumbe es
    que la última descoloque a las de abajo, y eso no se ve mirando únicamente la que
    acaba de caer.
    """
    done: list[measure.Placement] = []
    for i in range(index + 1):
        done.append(measure.measure_placement(scene, scene.boxes[i], i, done))

    # Una caja que estaba bien puesta y ahora no lo está solo puede significar que el
    # montón se ha movido. Es la definición operativa de derrumbe, y sale de medir, no
    # de un umbral inventado.
    derrumbe = any(old.placed and not new.placed
                   for old, new in zip(episode.placements, done))

    episode.placements = done
    # El estado del montón se calcula con lo que de verdad está SOBRE el palé, que no es
    # lo mismo que lo que está dentro de tolerancia ni lo mismo que lo intentado. Una
    # caja mal puesta pesa y mueve el CoG; una que se escurrió de la pinza y se quedó en
    # la mesa, no. Ambas tienen su fila de colocación; solo la primera entra aquí.
    episode.states.append(measure.pallet_state(measure.on_pallet(scene, done)))
    episode.drifts.append(drift)

    last = done[-1]
    state = episode.states[-1]
    _event(episode, scene, sink, "place", box,
           error_xy_mm=round(last.error_xy * 1000, 1),
           error_yaw_deg=round(float(np.degrees(last.error_yaw)), 1),
           overhang_mm=round(last.overhang * 1000, 1))
    _event(episode, scene, sink, "settle", box,
           layer=box.layer, drift_mm=round(drift * 1000, 1))

    if sink is not None:
        sink.placement(index, last)
        sink.pallet_state(index, state, drift)

    if verbose:
        print(f"    {box.name} {box.type_name:<7} capa {box.layer} {box.slot}  "
              f"error {last.error_xy * 1000:5.1f} mm  apoyo {last.support_ratio:4.0%}  "
              f"margen {state.stability_margin * 1000:+6.1f} mm  "
              f"{'ok' if last.placed else 'FALLO'}")

    if derrumbe:
        episode.failure = "stack_collapse"
    elif not last.placed:
        max_overhang = float(scene.pallet["episode"]["max_overhang"])
        episode.failure = ("overhang_violation" if last.overhang > max_overhang
                           else "wrong_placement")


def _event(episode: Episode, scene: PalletScene, sink: Sink | None, kind: str,
           box: Box | None = None, **payload) -> None:
    """
    Un evento del feed. `ts` es tiempo SIMULADO, no de reloj.

    Así el scrubber de la interfaz y el `duration_s` del episodio hablan del mismo
    tiempo, y dos ejecuciones de la misma semilla dan la misma línea temporal por
    rápida que sea la máquina que las corrió.

    `seq` es un contador del episodio entero, no del paquete: hay cuatro o cinco
    eventos por paquete y la base exige `unique(episode_id, seq)`.
    """
    row = {
        "ts": round(float(scene.data.time), 2),
        "seq": len(episode.events),
        "kind": kind,
        "package_id": box.name if box is not None else None,
        "payload": payload,
    }
    episode.events.append(row)
    if sink is not None:
        sink.event(row)
