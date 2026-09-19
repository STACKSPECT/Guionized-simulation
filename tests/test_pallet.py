"""
Comprobaciones del paletizado. Sin frameworks: `python tests/test_pallet.py`.

Cubre lo que tiene que ser cierto POR CONSTRUCCIÓN y se rompería en silencio si alguien
edita `configs/pallet.yaml` sin echar cuentas: que las cajas caben en la pinza, que la
holgura entre vecinas da para el dedo, que cada capa es de altura uniforme y cubre el
palé sin solaparse, y que las filas que se suben tienen las columnas que espera la base
de datos.

Lo que demuestra que el sistema funciona no es esto: es `scripts/palletize.py`, que
monta el palé de verdad. Esto es la red que avisa antes de gastar tres minutos de
simulación para descubrir que el guion ya no encaja.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import numpy as np                                                    # noqa: E402
from theker_telemetry import FAILURES                                 # noqa: E402

from src.pallet import measure                                        # noqa: E402
from src.pallet.episode import Episode                                # noqa: E402
from src.pallet.scene import (                                        # noqa: E402
    layer_base_z, layer_heights, load_configs, planned_pose,
)
from src.pallet.telemetry import (                                    # noqa: E402
    episode_result, pallet_state_row, placement_row, run_config,
)

# Grueso del dedo del Panda, medido sobre su malla. Es el número que fija cuánto hay
# que separar dos cajas vecinas en el eje por el que cierra la pinza.
FINGER_THICKNESS = 0.0105

_, PALLET = load_configs(REPO)


def _slots() -> list[dict]:
    """Cada entrada del guion con su caja y su rectángulo en planta, en frame del palé."""
    out = []
    for entry in PALLET["script"]:
        dims = PALLET["boxes"][entry["type"]]["dims"]
        out.append({
            "entry": entry,
            "dims": dims,
            "rect": (float(entry["place"][0]), float(entry["place"][1]),
                     float(dims[0]), float(dims[1])),
        })
    return out


def _by_layer() -> dict[int, list]:
    out: dict[int, list] = {}
    for slot in _slots():
        out.setdefault(int(slot["entry"]["layer"]), []).append(slot)
    return out


def test_every_box_fits_the_gripper() -> None:
    """La pinza abre 80 mm. Una caja más ancha no se puede coger, y punto."""
    for name, box in PALLET["boxes"].items():
        # La pinza cierra sobre Y: el lado que tiene que caber es dims[1].
        assert box["dims"][1] < 0.08, f"{name} no cabe en la pinza: {box['dims'][1]}"
        # Con menos de 5 mm de holgura el agarre depende de la suerte.
        assert 0.08 - box["dims"][1] >= 0.005, f"{name} va demasiado justa"


def test_layers_are_flat_by_construction() -> None:
    """Toda caja de una capa mide lo mismo de alto: si no, no hay superficie plana."""
    heights = layer_heights(PALLET)          # lanza si una capa mezcla alturas
    assert sorted(heights) == list(range(1, len(heights) + 1)), "faltan capas"

    base = layer_base_z(PALLET, deck_top=0.0)
    assert base[1] == 0.0                    # la primera capa apoya en la cubierta
    for layer in sorted(heights)[1:]:
        assert base[layer] == base[layer - 1] + heights[layer - 1]


def test_bigger_boxes_go_underneath() -> None:
    """Lo más grande abajo. Es el criterio del apilado, no una casualidad del guion."""
    volume, mass = {}, {}
    for slot in _slots():
        layer = int(slot["entry"]["layer"])
        box = PALLET["boxes"][slot["entry"]["type"]]
        volume[layer] = float(np.prod(box["dims"]))
        mass[layer] = float(box["mass"])
    for layer in sorted(volume)[1:]:
        assert volume[layer] < volume[layer - 1], \
            f"la capa {layer} es más grande que la de debajo"
        assert mass[layer] < mass[layer - 1], \
            f"la capa {layer} pesa más que la de debajo"


def test_layers_do_not_overlap_and_cover_the_pallet() -> None:
    """Dentro de una capa las cajas no se pisan, y entre todas cubren el palé."""
    dims = PALLET["pallet"]["dims"]
    area = float(dims[0]) * float(dims[1])

    for layer, slots in _by_layer().items():
        for i, a in enumerate(slots):
            for b in slots[i + 1:]:
                assert measure.rect_overlap(a["rect"], b["rect"]) == 0.0, (
                    f"capa {layer}: {a['entry']['slot']} y {b['entry']['slot']} "
                    f"se solapan")

        # Cubrir: la suma de huellas tiene que llegar al 70 % del palé. Menos que eso
        # no es una capa, son cuatro cajas sueltas encima de una tabla.
        #
        # Y más del 80 % no se puede pedir con esta pinza: los 23 mm que el dedo
        # necesita entre cajas vecinas se comen el 16 % del ancho del palé. Medido, las
        # dos capas cubren 76 % y 72 %. Quien quiera subir de ahí necesita una ventosa,
        # no un guion mejor.
        covered = sum(s["rect"][2] * s["rect"][3] for s in slots)
        assert covered >= 0.70 * area, \
            f"la capa {layer} solo cubre el {covered / area:.0%} del palé"


def test_neighbours_leave_room_for_the_finger() -> None:
    """
    En el eje por el que cierra la pinza, la separación tiene que dar para el dedo.

    Es el fallo que más costó encontrar: con 15 mm el dedo aterrizaba sobre la caja de
    al lado y el episodio moría en `stack_collapse` sin que nada lo explicara. La
    condición es `separación > release_margin + grueso de dedo`.
    """
    needed = float(PALLET["motion"]["release_margin"]) + FINGER_THICKNESS

    for layer, slots in _by_layer().items():
        for i, a in enumerate(slots):
            for b in slots[i + 1:]:
                # Solo importa entre cajas que se solapan en X: si no, el dedo baja por
                # un sitio donde no hay nada que golpear.
                overlap_x = (
                    min(a["rect"][0] + a["rect"][2] / 2, b["rect"][0] + b["rect"][2] / 2)
                    - max(a["rect"][0] - a["rect"][2] / 2, b["rect"][0] - b["rect"][2] / 2))
                if overlap_x <= 0:
                    continue
                gap_y = abs(a["rect"][1] - b["rect"][1]) - (a["rect"][3] + b["rect"][3]) / 2
                assert gap_y > needed, (
                    f"capa {layer}: {a['entry']['slot']}-{b['entry']['slot']} dejan "
                    f"{gap_y * 1000:.1f} mm y el dedo necesita {needed * 1000:.1f} mm")


def test_boxes_stay_on_the_pallet() -> None:
    """Ninguna caja del guion sobresale del palé estando en su sitio exacto."""
    dims = PALLET["pallet"]["dims"]
    pallet = (0.0, 0.0, float(dims[0]), float(dims[1]))
    for slot in _slots():
        over = measure.overhang(slot["rect"], pallet)
        assert over == 0.0, f"{slot['entry']['slot']} sobresale {over * 1000:.1f} mm"


def test_planned_pose_stacks_layers() -> None:
    """La cota del hueco sale de las capas de debajo, no de un número escrito a mano."""
    deck = 0.412
    for slot in _slots():
        entry = slot["entry"]
        z = planned_pose(PALLET, entry, deck)[2]
        base = layer_base_z(PALLET, deck)[int(entry["layer"])]
        assert abs(z - (base + slot["dims"][2] / 2)) < 1e-12


def test_transit_clears_the_finished_stack() -> None:
    """El brazo cruza por encima del montón terminado, no a través de él."""
    deck = 0.412
    top = max(planned_pose(PALLET, s["entry"], deck)[2] + s["dims"][2] / 2
              for s in _slots())
    transit = float(PALLET["motion"]["transit_height"])
    tallest = max(b["dims"][2] for b in PALLET["boxes"].values())
    # La caja va colgada del TCP: lo que tiene que pasar por encima es su base.
    assert transit - tallest / 2 > top, "el brazo cruza a la altura de la carga"


def test_rows_match_the_supabase_columns() -> None:
    """
    Las filas que se suben tienen las columnas que la base de datos espera.

    Sin esto, un renombre se descubre cuando PostgREST devuelve un 400 a mitad de una
    tanda y el episodio ya se ha perdido.
    """
    episode = _fake_episode()

    placement = placement_row(0, episode.placements[0])
    assert set(placement) == {
        "seq", "package_id", "package_type", "mass_kg", "dims_m", "layer",
        "planned_pose", "actual_pose", "error_xy_m", "error_yaw_rad",
        "support_ratio", "overhang_m", "placed"}
    assert set(placement["actual_pose"]) == {"x", "y", "z", "yaw"}
    # `package_id` y `package_type` son NOT NULL en la base.
    assert placement["package_id"] and placement["package_type"]

    state = pallet_state_row(0, episode.states[0], episode.drifts[0])
    assert set(state) == {
        "after_seq", "mass_kg", "cog_x", "cog_y", "cog_z",
        "stability_margin_m", "fill_ratio", "settle_drift_m"}

    # Los payloads de eventos son un contrato tácito: no están en el SQL, los lee el
    # feed de la interfaz. Y van en mm y grados, no en SI como las columnas.
    keys = {"perceive": {"seen", "confidence"}, "plan": {"layer", "slot"},
            "pick": {"mass_kg", "type"},
            "place": {"error_xy_mm", "error_yaw_deg", "overhang_mm"},
            "settle": {"layer", "drift_mm"}, "fail": {"cause"}}
    for event in episode.events:
        assert set(event) == {"ts", "seq", "kind", "package_id", "payload"}
        assert set(event["payload"]) == keys[event["kind"]]


def test_episode_result_is_marked_as_oracle() -> None:
    """
    Sin percepción, el run va marcado. La interfaz lo pinta con trama y no lo compara
    con uno que sí mira imágenes: mezclarlos es mentirle al jurado.
    """
    result = episode_result(_fake_episode(), _FakeScene())
    assert result.oracle is True
    assert result.task == "palletizing"
    assert result.failure is None or result.failure in FAILURES
    assert set(result.metrics) >= {"cog_offset_xy", "fill_ratio", "settle_drift",
                                   "n_layers", "layer_flatness", "max_overhang",
                                   "score"}


def test_run_config_carries_the_real_pallet_size() -> None:
    """
    El tamaño del palé viaja con la ejecución, en `runs.config`.

    La interfaz dibuja a escala real y no puede deducirlo de los episodios: sin este
    dato supone un europeo de 1200x800 y pinta esta maqueta seis veces más grande.
    `palletSize()` del front lo busca aquí primero.
    """
    config = run_config(_FakeScene())
    assert config["pallet_size_m"] == [float(d) for d in PALLET["pallet"]["dims"]]
    assert config["pallet_scale"] > 1.0
    assert sum(capa["n"] for capa in config["layers"]) == len(PALLET["script"])


def test_the_views_are_named_as_the_schema_accepts() -> None:
    """
    El CHECK de `snapshots.view` solo admite top | side | iso | camera.

    Una cámara llamada de otra forma sube el PNG a Storage y luego la fila la rechaza la
    base con un 23514, así que la foto queda huérfana y la traza sin su imagen.
    """
    permitidas = {"top", "side", "iso", "camera"}
    assert set(PALLET["cameras"]) <= permitidas, (
        f"vistas fuera del vocabulario: {set(PALLET['cameras']) - permitidas}")


def test_the_cog_counts_boxes_outside_tolerance() -> None:
    """
    El paquete que derrumba el montón entra en el centro de gravedad.

    Es el fallo que tuvo esta simulación: el estado del palé se calculaba solo con las
    cajas dentro de tolerancia, así que la que provoca el vuelco quedaba fuera del
    cálculo y **la traza acababa en verde en un episodio que se cayó**. El gráfico
    existe para dejar ver venir el fallo; con ese filtro no enseñaba nada.
    """
    bien = _placement(-0.05, 0.0, 0.10, placed=True)
    # Misma caja, mismo sitio, pero fuera de tolerancia: sigue pesando igual.
    mal = _placement(0.05, 0.0, 0.10, placed=False)

    solo_buenas = measure.pallet_state([bien])
    las_dos = measure.pallet_state([bien, mal])

    assert las_dos.mass_kg > solo_buenas.mass_kg, "la caja mal puesta no pesa"
    assert las_dos.cog[0] > solo_buenas.cog[0], "la caja mal puesta no mueve el CoG"


def test_the_stability_margin_is_measured_against_the_support() -> None:
    """
    Los cuatro casos que definen el indicador.

    Se mide contra el borde del polígono de soporte, no del palé: contra el palé los
    números salen optimistas y la pantalla diría que todo va bien hasta el derrumbe.
    """
    base = [(-0.05, 0.0, 0.10, 0.06), (0.05, 0.0, 0.10, 0.06)]
    x0, x1, y0, y1 = measure.support_polygon(base)
    assert (x0, x1, y0, y1) == (-0.10, 0.10, -0.03, 0.03)

    centrado = measure.stability_margin(0.0, 0.0, 0.05, base)
    assert round(centrado, 9) == round(0.03 - 0.28 * 0.05, 9)
    # Apilar más alto lo empeora, con el mismo reparto de masa.
    assert measure.stability_margin(0.0, 0.0, 0.10, base) < centrado
    # Fuera del apoyo, vuelca.
    assert measure.stability_margin(0.0, 0.05, 0.05, base) < 0.0
    # Sin nada apoyado no hay polígono que medir, y eso no es un vuelco.
    assert measure.stability_margin(0.0, 0.0, 0.0, []) == 0.0


def test_a_box_left_on_the_table_is_not_on_the_pallet() -> None:
    """
    Lo que pesa sobre el palé no es lo mismo que lo intentado.

    Una caja que se escurrió de la pinza deja su fila de colocación con
    `placed = false`, pero su masa está en la mesa: no puede mover el centro de
    gravedad del montón. El criterio es geométrico, no "¿falló la maniobra?".
    """
    encima = _placement(0.0, 0.0, 0.02, placed=False)     # mal puesta, pero encima
    en_la_mesa = _placement(0.0, 0.30, 0.02, placed=False)  # ni toca el palé
    assert measure.on_pallet(_FakeScene(), [encima, en_la_mesa]) == [encima]


def test_rows_of_one_episode_do_not_repeat_seq() -> None:
    """
    `unique(episode_id, seq)` en `events` y `placements`, `after_seq` en `pallet_states`.

    Un duplicado no se pierde solo: tumba la fila y, con ella, la subida del resto del
    episodio. Hay cuatro o cinco eventos por paquete, así que el `seq` del evento no
    puede ser el índice del paquete.
    """
    episode = _fake_episode()
    seqs = [e["seq"] for e in episode.events]
    assert len(seqs) == len(set(seqs)) == len(episode.events)
    assert seqs == sorted(seqs), "el feed tiene que ir en orden"

    # Y los eventos son más que los paquetes: por eso no se puede reutilizar el índice.
    assert len(episode.events) > len(episode.placements)

    filas = [placement_row(i, p) for i, p in enumerate(episode.placements)]
    assert len({f["seq"] for f in filas}) == len(filas)
    estados = [pallet_state_row(i, s, 0.0) for i, s in enumerate(episode.states)]
    assert len({e["after_seq"] for e in estados}) == len(estados)


# ── andamio mínimo, para no arrancar el simulador ────────────────────────────

class _FakeScene:
    pallet = PALLET


class _FakeBox:
    def __init__(self, entry, name="caja_01"):
        box = PALLET["boxes"][entry["type"]]
        self.name = name
        self.type_name = entry["type"]
        self.dims = tuple(box["dims"])
        self.mass = float(box["mass"])
        self.layer = int(entry["layer"])
        self.slot = entry["slot"]


def _placement(x: float, y: float, z: float, *, placed: bool) -> measure.Placement:
    """Una colocación medida, puesta a mano donde interese."""
    entry = PALLET["script"][0]                       # una caja de la capa 1
    position = np.array([x, y, z])
    return measure.Placement(
        box=_FakeBox(entry), position=position, yaw=0.0,
        planned=np.array([entry["place"][0], entry["place"][1], z]),
        error_xy=0.0, error_yaw=0.0, support_ratio=1.0, overhang=0.0, placed=placed)


def _fake_episode() -> Episode:
    entry = PALLET["script"][0]
    box = _FakeBox(entry)
    position = np.array([entry["place"][0], entry["place"][1], box.dims[2] / 2])
    placement = measure.Placement(
        box=box, position=position, yaw=0.0, planned=position,
        error_xy=0.001, error_yaw=0.01, support_ratio=1.0, overhang=0.0, placed=True)
    episode = Episode(seed=1, n_objects=len(PALLET["script"]))
    episode.placements = [placement]
    episode.states = [measure.pallet_state([placement])]
    episode.drifts = [0.002]
    episode.events = [
        {"ts": 1.0, "seq": 0, "kind": "perceive", "package_id": None,
         "payload": {"seen": 10, "confidence": 1.0}},
        {"ts": 1.5, "seq": 1, "kind": "plan", "package_id": box.name,
         "payload": {"layer": box.layer, "slot": box.slot}},
        {"ts": 2.0, "seq": 2, "kind": "pick", "package_id": box.name,
         "payload": {"mass_kg": box.mass, "type": box.type_name}},
        {"ts": 3.0, "seq": 3, "kind": "place", "package_id": box.name,
         "payload": {"error_xy_mm": 1.0, "error_yaw_deg": 0.6, "overhang_mm": 0.0}},
        {"ts": 3.5, "seq": 4, "kind": "settle", "package_id": box.name,
         "payload": {"layer": box.layer, "drift_mm": 2.0}},
    ]
    episode.duration_s = 20.0
    return episode


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    measure.demo()
    print(f"{len(tests)} comprobaciones pasadas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
