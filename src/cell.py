"""
La celda: lo que hay alrededor del robot y no depende de qué tarea se haga en ella.

Cuatro piezas, ni una más: el site del TCP, la mesa, cómo se orienta una cámara y cómo
se construye una pose de agarre cenital. Vienen de la simulación de inducción
(`theker-induction`: `src/sim/scene.py` y `src/planning/grasp.py`), donde estaban
mezcladas con su percepción y su planificador. Aquí llegan sueltas y sin nada de eso.

Están juntas porque las tres primeras describen el banco de trabajo y la cuarta la
convención de muñeca del Panda, que es la misma se apilen palés o se induzcan bultos.
"""

from __future__ import annotations

from typing import Protocol

import mujoco
import numpy as np


class RobotScene(Protocol):
    """
    Lo único que el control del brazo necesita saber de una escena.

    Existe para que `src/control/arm.py` no tenga que importar la escena concreta del
    palé: el brazo es de la celda y la tarea es de quien la use. Cualquier escena que
    exponga estos cinco miembros se puede mover con `ArmController`.
    """

    model: mujoco.MjModel
    data: mujoco.MjData
    home_qpos: np.ndarray
    tcp_site_id: int
    cfg: dict                  # envuelto: `cfg["scene"]` es la celda

# El modelo del Menagerie no define ningún site; mink necesita un frame para el TCP y
# se lo añadimos entre las almohadillas de la pinza.
TCP_SITE = "tcp"

# La pinza baja en vertical: su eje Z apunta hacia la mesa.
_APPROACH = np.array([0.0, 0.0, -1.0])


def lookat_quat(position, target) -> np.ndarray:
    """
    Cuaternión (w,x,y,z) de una cámara en `position` mirando a `target`.

    MjSpec no tiene `lookat`. Convención de MuJoCo: la cámara mira por su -Z local,
    con +Y como "arriba" de la imagen.
    """
    forward = np.asarray(target, float) - np.asarray(position, float)
    forward /= np.linalg.norm(forward)
    z_cam = -forward
    up = np.array([0.0, 0.0, 1.0])
    if abs(float(up @ z_cam)) > 0.99:       # cenital: +Z es paralelo, usa +X
        up = np.array([1.0, 0.0, 0.0])
    x_cam = np.cross(up, z_cam)
    x_cam /= np.linalg.norm(x_cam)
    y_cam = np.cross(z_cam, x_cam)
    rot = np.column_stack([x_cam, y_cam, z_cam])
    quat = np.empty(4)
    mujoco.mju_mat2Quat(quat, rot.flatten())
    return quat


def add_table(spec: mujoco.MjSpec, scene_cfg: dict) -> None:
    """
    La mesa de trabajo.

    El semieje z se apoya BAJO la superficie, así que la altura de trabajo es
    exactamente `table.height` y no hay que restarle nada al colocar cosas encima.
    """
    table = scene_cfg["table"]
    cx, cy = table["center"]
    sx, sy, sz = table["size"]
    spec.worldbody.add_geom(
        name="table",
        type=mujoco.mjtGeom.mjGEOM_BOX,
        pos=[cx, cy, table["height"] - sz],
        size=[sx, sy, sz],
        friction=table["friction"],
        rgba=table["rgba"],
    )


def tcp_frame(position, closing_dir, canonical: bool = True) -> np.ndarray:
    """
    Pose 4x4 del TCP: Z hacia abajo (aproximación) e Y sobre `closing_dir` (cierre).

    `closing_dir` se proyecta al plano horizontal: la caja descansa sobre una superficie
    y el agarre es cenital, así que la dirección de cierre no tiene componente vertical.

    Una pinza paralela es simétrica: cerrar sobre `d` y sobre `-d` es el MISMO agarre
    físico, pero exige ángulos de muñeca opuestos. Con `canonical` se elige siempre el
    representante con componente X positiva, que evita pedirle a la muñeca del Panda
    (joint7, rango ±2.8973) giros que se salen por un extremo.
    """
    direction = np.asarray(closing_dir, float)
    y_axis = np.array([direction[0], direction[1], 0.0], float)
    norm = np.linalg.norm(y_axis)
    if norm < 1e-9:
        raise ValueError("dirección de cierre degenerada")
    y_axis /= norm
    if canonical and y_axis[0] < 0.0:
        y_axis = -y_axis
    z_axis = _APPROACH
    x_axis = np.cross(y_axis, z_axis)

    pose = np.eye(4)
    pose[:3, 0], pose[:3, 1], pose[:3, 2] = x_axis, y_axis, z_axis
    pose[:3, 3] = np.asarray(position, float)
    return pose
