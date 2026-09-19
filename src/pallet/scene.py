"""
Construcción de la escena de paletizado: brazo + mesa + palé + cajas + dos cámaras.

Todo sale de `configs/scene.yaml` (robot, mesa, motion) y `configs/pallet.yaml` (palé,
catálogo, guion, cámaras). Si para cambiar el apilado hubiera que tocar este fichero,
el fichero está mal.

Se construye con `mujoco.MjSpec` sobre el `scene.xml` del Menagerie en vez de generar
XML propio: así no hay que pelearse con `meshdir` ni con rutas relativas de `<include>`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import mujoco
import numpy as np
import yaml

# `tcp_frame` se reexporta a propósito: `episode.py` lo importa de aquí, porque una pose
# de agarre es parte de cómo se describe esta escena.
from src.cell import TCP_SITE, add_table, lookat_quat, tcp_frame  # noqa: F401

REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Box:
    """Una caja del guion: lo que dice la configuración más lo que dice el modelo."""

    name: str
    type_name: str
    dims: tuple[float, float, float]     # COMPLETAS, como en pallet.yaml
    mass: float
    layer: int
    slot: str
    stage: tuple[float, float]           # dónde espera, en el mundo
    place: tuple[float, float]           # centro del hueco, relativo al palé
    body_id: int
    joint_qposadr: int

    @property
    def half_height(self) -> float:
        return self.dims[2] / 2.0

    @property
    def grasp_width(self) -> float:
        """Lo que tienen que abarcar los dedos: el lado horizontal menor."""
        return min(self.dims[0], self.dims[1])

    @property
    def footprint_area(self) -> float:
        return self.dims[0] * self.dims[1]


@dataclass
class PalletScene:
    """Modelo compilado, estado, y el inventario de lo que hay dentro."""

    model: mujoco.MjModel
    data: mujoco.MjData
    boxes: list[Box]
    # `cfg` va envuelto en {"scene": ...} y no como el YAML pelado por un motivo
    # concreto: `ArmController` lee `scene.cfg["scene"]`, y así se reutiliza tal cual,
    # sin tocar el control que ya está medido y calibrado en inducción.
    cfg: dict
    pallet: dict                         # pallet.yaml
    tcp_site_id: int
    camera_ids: dict[str, int]
    home_qpos: np.ndarray

    @property
    def scene_cfg(self) -> dict:
        """El `scene.yaml` en crudo, sin la envoltura que pide `ArmController`."""
        return self.cfg["scene"]

    @property
    def deck_top(self) -> float:
        """z de la cara sobre la que se apila."""
        return (float(self.scene_cfg["table"]["height"])
                + float(self.pallet["pallet"]["deck_thickness"]))


def load_configs(repo: Path = REPO) -> tuple[dict, dict]:
    """`scene.yaml` (la celda) y `pallet.yaml` (el palé y su guion)."""
    with open(repo / "configs" / "scene.yaml", encoding="utf-8") as fh:
        scene_cfg = yaml.safe_load(fh)
    with open(repo / "configs" / "pallet.yaml", encoding="utf-8") as fh:
        pallet_cfg = yaml.safe_load(fh)
    return scene_cfg, pallet_cfg


# ─────────────────────────────────────────────────────────────────────────────
# Geometría del apilado. Sale solo de la configuración: ni toca el simulador ni
# depende de dónde acabaron las cajas, así que es lo PLANIFICADO, y sirve de
# referencia contra la que medir el error.
# ─────────────────────────────────────────────────────────────────────────────

def layer_heights(pallet_cfg: dict) -> dict[int, float]:
    """
    Altura de cada capa, en metros.

    Exige que todas las cajas de una capa midan lo mismo de alto: es lo que hace que la
    superficie salga plana y lo que permite dar por sabida la cota de la capa siguiente.
    Si alguien mete una caja de otra altura, revienta aquí y no tres capas más arriba,
    con el montón ya torcido y sin saber por qué.
    """
    catalog = pallet_cfg["boxes"]
    out: dict[int, float] = {}
    for entry in pallet_cfg["script"]:
        layer = int(entry["layer"])
        height = float(catalog[entry["type"]]["dims"][2])
        if out.setdefault(layer, height) != height:
            raise ValueError(
                f"la capa {layer} mezcla alturas ({out[layer]} y {height}): la "
                f"superficie no quedaría plana. Ver configs/pallet.yaml"
            )
    return out


def layer_base_z(pallet_cfg: dict, deck_top: float) -> dict[int, float]:
    """z de la cara INFERIOR de cada capa: la cubierta más lo que hay debajo."""
    heights = layer_heights(pallet_cfg)
    out, z = {}, deck_top
    for layer in sorted(heights):
        out[layer] = z
        z += heights[layer]
    return out


def planned_pose(pallet_cfg: dict, entry: dict, deck_top: float) -> np.ndarray:
    """Centro donde debe quedar la caja `entry`, en el mundo: `[x, y, z]`."""
    center = np.asarray(pallet_cfg["pallet"]["center"], float)
    dims = pallet_cfg["boxes"][entry["type"]]["dims"]
    base = layer_base_z(pallet_cfg, deck_top)[int(entry["layer"])]
    return np.array([center[0] + float(entry["place"][0]),
                     center[1] + float(entry["place"][1]),
                     base + float(dims[2]) / 2.0])


# ─────────────────────────────────────────────────────────────────────────────
# Construcción
# ─────────────────────────────────────────────────────────────────────────────

def build_scene(scene_cfg: dict, pallet_cfg: dict, repo: Path = REPO) -> PalletScene:
    """Compila la escena y deja las cajas ya asentadas en su sitio de preparación."""
    # Esta celda mueve cajas mucho más grandes que la de inducción y no admite su
    # velocidad; el porqué y la tabla de medidas están en `pallet.yaml`. El pisado se
    # hace aquí, en un solo sitio, para que `ArmController` siga leyendo `motion` sin
    # enterarse de que hay dos configuraciones.
    scene_cfg = {**scene_cfg,
                 "motion": {**scene_cfg["motion"], **pallet_cfg.get("motion", {})}}

    spec = mujoco.MjSpec.from_file(str(repo / scene_cfg["robot"]["model"]))

    # El modelo del Menagerie no define ningún site; mink necesita un frame para el TCP.
    spec.body("hand").add_site(name=TCP_SITE, pos=scene_cfg["robot"]["tcp_offset"])

    add_table(spec, scene_cfg)
    _add_pallet(spec, scene_cfg, pallet_cfg)
    _set_lighting(spec, pallet_cfg["lighting"])

    max_w = max_h = 0
    for name, cam in pallet_cfg["cameras"].items():
        width, height = cam["resolution"]
        spec.worldbody.add_camera(
            name=name,
            pos=cam["position"],
            quat=_camera_quat(cam),
            fovy=cam["fovy"],
            resolution=[width, height],
        )
        max_w, max_h = max(max_w, width), max(max_h, height)
    # El renderer no puede pedir más píxeles que el buffer offscreen.
    spec.visual.global_.offwidth = max(spec.visual.global_.offwidth, max_w)
    spec.visual.global_.offheight = max(spec.visual.global_.offheight, max_h)

    table_top = float(scene_cfg["table"]["height"])
    catalog = pallet_cfg["boxes"]
    for i, entry in enumerate(pallet_cfg["script"]):
        _add_box(spec, f"caja_{i + 1:02d}", entry, catalog[entry["type"]], table_top)

    model = spec.compile()
    data = mujoco.MjData(model)
    # El keyframe "home" del Menagerie solo cubre los 9 qpos del brazo; el compilador
    # rellena con ceros los de las cajas que hemos añadido, así que las dejaría caídas
    # en el origen. `qpos0` sí trae la pose que pusimos en cada body: se restaura de ahí.
    mujoco.mj_resetDataKeyframe(model, data, 0)

    boxes = []
    for i, entry in enumerate(pallet_cfg["script"]):
        name = f"caja_{i + 1:02d}"
        jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, f"{name}_free")
        adr = model.jnt_qposadr[jid]
        data.qpos[adr : adr + 7] = model.qpos0[adr : adr + 7]
        box_cfg = catalog[entry["type"]]
        boxes.append(Box(
            name=name,
            type_name=entry["type"],
            dims=tuple(float(d) for d in box_cfg["dims"]),
            mass=float(box_cfg["mass"]),
            layer=int(entry["layer"]),
            slot=str(entry["slot"]),
            stage=(float(entry["stage"][0]), float(entry["stage"][1])),
            place=(float(entry["place"][0]), float(entry["place"][1])),
            body_id=mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, name),
            joint_qposadr=adr,
        ))

    scene = PalletScene(
        model=model,
        data=data,
        boxes=boxes,
        cfg={"scene": scene_cfg},
        pallet=pallet_cfg,
        tcp_site_id=mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, TCP_SITE),
        camera_ids={
            name: mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_CAMERA, name)
            for name in pallet_cfg["cameras"]
        },
        home_qpos=data.qpos.copy(),
    )

    for _ in range(int(scene_cfg["motion"]["settle_steps"])):
        mujoco.mj_step(model, data)
    return scene


def _set_lighting(spec: mujoco.MjSpec, lighting: dict) -> None:
    """
    Reequilibra la luz para que las dos vistas sean legibles. El porqué, en pallet.yaml.

    Solo toca esta escena: el `MjSpec` se construye por episodio y el fichero del
    Menagerie no se modifica, así que cualquier otra escena conserva la suya.
    """
    spec.visual.headlight.diffuse = [float(lighting["headlight_diffuse"])] * 3
    spec.visual.headlight.ambient = [float(lighting["headlight_ambient"])] * 3
    for light in spec.lights:
        key = "spot_diffuse" if light.name == "top" else "overhead_diffuse"
        light.diffuse = [float(lighting[key])] * 3

    fill = lighting["fill"]
    # Direccional: la posición da igual para la iluminación, solo importa `dir`. Se
    # coloca lejos y fuera de plano para que la sombra que proyecta no caiga en el palé.
    spec.worldbody.add_light(
        name="fill",
        type=mujoco.mjtLightType.mjLIGHT_DIRECTIONAL,
        pos=[0.45, -1.2, 1.2],
        dir=fill["direction"],
        diffuse=[float(fill["diffuse"])] * 3,
        specular=[0.0, 0.0, 0.0],
    )


def _camera_quat(cam: dict) -> np.ndarray:
    """
    Cuaternión de una cámara. Con `up`, se fija qué eje del mundo sube en la imagen.

    `lookat_quat` decide el "arriba" solo, y para una cámara cenital —donde +Z es
    paralelo a la vertical y no sirve de referencia— elige +X. Eso deja el palé girado
    un cuarto de vuelta respecto a como lo dibuja la interfaz (X a la derecha, Y
    arriba), y entonces la foto y el esquema no se pueden comparar de un vistazo, que
    es justo para lo que está la foto. Mirando en vertical no hay un arriba natural:
    se elige.
    """
    if "up" not in cam:
        return lookat_quat(cam["position"], cam["lookat"])
    forward = np.asarray(cam["lookat"], float) - np.asarray(cam["position"], float)
    z_cam = -forward / np.linalg.norm(forward)
    x_cam = np.cross(np.asarray(cam["up"], float), z_cam)
    x_cam /= np.linalg.norm(x_cam)
    y_cam = np.cross(z_cam, x_cam)
    quat = np.empty(4)
    mujoco.mju_mat2Quat(quat, np.column_stack([x_cam, y_cam, z_cam]).flatten())
    return quat


def _add_pallet(spec: mujoco.MjSpec, scene_cfg: dict, pallet_cfg: dict) -> None:
    """
    La cubierta del palé, apoyada en la mesa.

    Solo la tabla, sin tacos ni largueros: elevarla cambiaría todas las alturas de
    depósito, que están comprobadas contra el alcance real del brazo (ver la cabecera de
    pallet.yaml). Lo que aporta un taco es estética, y no al precio de reverificar.
    """
    pallet = pallet_cfg["pallet"]
    cx, cy = pallet["center"]
    dx, dy = pallet["dims"]
    thickness = float(pallet["deck_thickness"])
    table_top = float(scene_cfg["table"]["height"])
    spec.worldbody.add_geom(
        name="pallet_deck",
        type=mujoco.mjtGeom.mjGEOM_BOX,
        pos=[cx, cy, table_top + thickness / 2.0],
        size=[dx / 2.0, dy / 2.0, thickness / 2.0],
        friction=scene_cfg["table"]["friction"],
        rgba=pallet["rgba"],
    )


def _add_box(spec: mujoco.MjSpec, name: str, entry: dict, box_cfg: dict,
             table_top: float) -> None:
    """Una caja con free joint, esperando en su sitio de preparación."""
    dims = [float(d) for d in box_cfg["dims"]]
    x, y = entry["stage"]
    # Se suelta 2 mm por encima de la mesa y los `settle_steps` hacen el resto.
    body = spec.worldbody.add_body(
        name=name, pos=[float(x), float(y), table_top + dims[2] / 2.0 + 0.002])
    body.add_freejoint(name=f"{name}_free")
    body.add_geom(
        name=f"{name}_geom",
        type=mujoco.mjtGeom.mjGEOM_BOX,
        size=[d / 2.0 for d in dims],       # MuJoCo quiere semiejes; pallet.yaml no
        mass=float(box_cfg["mass"]),
        friction=box_cfg["friction"],
        rgba=box_cfg["rgba"],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Estado y vistas
# ─────────────────────────────────────────────────────────────────────────────

def box_pose(scene: PalletScene, box: Box) -> tuple[np.ndarray, float]:
    """Posición y yaw reales de una caja. Esto SÍ mira el simulador: es la medida."""
    adr = box.joint_qposadr
    pos = scene.data.qpos[adr : adr + 3].copy()
    quat = scene.data.qpos[adr + 3 : adr + 7]
    rot = np.empty(9)
    mujoco.mju_quat2Mat(rot, quat)
    rot = rot.reshape(3, 3)
    return pos, float(np.arctan2(rot[1, 0], rot[0, 0]))


def render(scene: PalletScene, view: str) -> np.ndarray:
    """
    Fotograma RGB de una de las cámaras del palé.

    El renderer se crea y se destruye en cada llamada: esto pasa dos veces por episodio
    y no compensa mantener vivo un contexto GL entre medias, al revés que en percepción,
    que renderiza en cada pasada.
    """
    cam = scene.pallet["cameras"][view]
    width, height = cam["resolution"]
    renderer = mujoco.Renderer(scene.model, height=height, width=width)
    try:
        renderer.update_scene(scene.data, camera=scene.camera_ids[view])
        return renderer.render()
    finally:
        renderer.close()
