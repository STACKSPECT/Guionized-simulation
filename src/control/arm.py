"""
Ejecución: GraspPlan -> trayectoria (mink) -> data.ctrl.

El brazo se mueve en cartesiano entre waypoints, resolviendo IK diferencial con mink a
`control_hz` y estampando el resultado en los 7 actuadores de posición. La pinza va
aparte, por el actuador 8 (rango 0-255, 255 = abierta).

Cuidado con los 9 DOF: `mink.Configuration` trabaja sobre el modelo entero, así que la
IK también calcula velocidad para los dos dedos, y mink no sabe nada del tendón ni de la
`equality` que los acopla. Se anclan con el `PostureTask` y jamás se escribe
`configuration.q[7:9]` en `data.ctrl`.
"""

from __future__ import annotations

from collections.abc import Callable

import mink
import mujoco
import numpy as np

from src.cell import RobotScene

# Rango del actuador de pinza, tal y como lo define panda.xml.
GRIPPER_OPEN = 255.0
GRIPPER_CLOSED = 0.0


class ArmController:
    """Mueve el brazo y la pinza avanzando la física de la escena que recibe."""

    def __init__(self, scene: RobotScene, on_step: Callable[[], None] | None = None,
                 speed: float = 1.0):
        """
        `speed` multiplica la velocidad del brazo sobre la de `configs/scene.yaml`.

        Medido en el nivel 2 (40 episodios, semillas 200-239): x1 -> 94 % de bultos en
        0.79 s; x4 -> 93 % en 0.54 s. Es decir, **acelerar x4 sale casi gratis**, que no
        es lo que cabría esperar. Lo que sí cambia es la causa de fallo: a x1 domina
        `wrong_placement` (6 de 7) y a x4 aparece `grasp_slip` (3 de 5), que es el bulto
        escapándose de una pinza que solo cierra con ~2.5 N.

        Por encima de x8 no está medido. La velocidad base de scene.yaml es conservadora
        precisamente porque se eligió mientras el bulto pesaba el triple; con 0.05 kg hay
        margen de sobra.
        """
        if speed <= 0.0:
            raise ValueError(f"speed tiene que ser > 0, no {speed}")
        self.scene = scene
        self.on_step = on_step
        self.speed = float(speed)
        cfg = scene.cfg["scene"]
        self.motion = cfg["motion"]
        self.robot = cfg["robot"]

        self.control_dt = 1.0 / float(cfg["episode"]["control_hz"])
        self.steps_per_tick = max(1, round(self.control_dt / scene.model.opt.timestep))
        self.gripper_idx = int(self.robot["gripper_actuator"])

        self.configuration = mink.Configuration(scene.model)
        self.tcp_task = mink.FrameTask(
            frame_name=self.robot["ik_frame"],
            frame_type="site",
            position_cost=1.0,
            orientation_cost=0.5,
            lm_damping=1.0,
        )
        # Ancla los 9 DOF, dedos incluidos: sin esto la IK los deja a la deriva.
        self.posture_task = mink.PostureTask(
            scene.model, cost=float(self.motion["posture_cost"])
        )
        self.posture_task.set_target(scene.home_qpos.copy())
        self._tasks = [self.tcp_task, self.posture_task]
        # Sin esto la IK empuja alegremente contra los topes articulares y el brazo se
        # queda plantado en una configuración de la que no sabe salir.
        self._limits = [mink.ConfigurationLimit(scene.model)]

    # ------------------------------------------------------------------ estado

    def tcp_pose(self) -> np.ndarray:
        """Pose 4x4 actual del TCP en el mundo."""
        data = self.scene.data
        pose = np.eye(4)
        pose[:3, :3] = data.site_xmat[self.scene.tcp_site_id].reshape(3, 3)
        pose[:3, 3] = data.site_xpos[self.scene.tcp_site_id]
        return pose

    def finger_opening(self) -> float:
        """Separación actual entre dedos, en metros."""
        return float(self.scene.data.qpos[7] + self.scene.data.qpos[8])

    def is_holding(self, width: float, margin: float = 0.008) -> bool:
        """
        ¿Hay algo entre los dedos?

        Si la pinza se cerró bastante por debajo del ancho del bulto, es que cerró en
        vacío o el objeto escapó: eso es `grasp_slip`.
        """
        return self.finger_opening() > width - margin

    # ----------------------------------------------------------------- acciones

    def step_physics(self) -> None:
        for _ in range(self.steps_per_tick):
            mujoco.mj_step(self.scene.model, self.scene.data)
        if self.on_step is not None:
            self.on_step()

    def set_gripper(self, value: float, hold_s: float = 0.8) -> None:
        self.scene.data.ctrl[self.gripper_idx] = value
        for _ in range(int(hold_s / self.control_dt)):
            self.step_physics()

    def move_joints(self, qpos, duration_s: float = 1.5) -> None:
        """
        Interpolación en espacio de juntas, para poses fijas (home, observación).

        No hace falta IK para ir a una pose conocida, y así no se depende de que el QP
        converja para algo que ya está resuelto.
        """
        start = self.scene.data.ctrl[:7].copy()
        target = np.asarray(qpos, float)[:7]
        n = max(1, int(duration_s / self.speed / self.control_dt))
        for i in range(1, n + 1):
            self.scene.data.ctrl[:7] = start + (target - start) * (i / n)
            self.step_physics()

    def move_to(self, target: np.ndarray) -> bool:
        """
        Lleva el TCP a `target` (4x4) interpolando en línea recta, y afina al llegar.

        Devuelve False si no converge: eso es `ik_unreachable`, no un fallo silencioso.
        """
        tol_pos = float(self.motion["tol_pos"])
        tol_rot = float(self.motion["tol_rot"])
        current = self.tcp_pose()
        start = mink.SE3.from_matrix(current)
        goal = mink.SE3.from_matrix(target)

        # El tramo se dimensiona por traslación Y por giro. Con solo la traslación, un
        # traslado corto que además exige reorientar la muñeca 120 grados los mete todos
        # en los mismos ticks: la pinza gira a más de 100 grados/s y el bulto sale
        # despedido. Reorientar es parte de la tarea de inducción, así que se le da su
        # propio límite de velocidad.
        distance = float(np.linalg.norm(target[:3, 3] - current[:3, 3]))
        angle = _rotation_angle(current[:3, :3], target[:3, :3])
        lin = float(self.motion["cartesian_speed"]) * self.speed
        ang = float(self.motion["angular_speed"]) * self.speed
        n = max(
            2,
            int(distance / lin / self.control_dt),
            int(angle / ang / self.control_dt),
        )

        for i in range(1, n + 1):
            self._ik_tick(start.interpolate(goal, _ease(i / n)).as_matrix())
        # Los últimos milímetros los da el servo de posición, no la interpolación:
        # se insiste en el waypoint final hasta que el TCP real llega.
        for _ in range(int(float(self.motion["converge_s"]) / self.control_dt)):
            self._ik_tick(target)
            if self._reached(target, tol_pos, tol_rot):
                return True
        return self._reached(target, tol_pos, tol_rot)

    # ------------------------------------------------------------------ interno

    def _ik_tick(self, target: np.ndarray) -> None:
        data = self.scene.data
        self.configuration.update(data.qpos)
        self.tcp_task.set_target(mink.SE3.from_matrix(target))
        velocity = mink.solve_ik(
            self.configuration, self._tasks, self.control_dt, "daqp",
            damping=1e-3, limits=self._limits,
        )
        self.configuration.integrate_inplace(velocity, self.control_dt)
        # Solo los 7 del brazo: los dedos los manda el actuador de pinza.
        data.ctrl[:7] = self.configuration.q[:7]
        self.step_physics()

    def _reached(self, target: np.ndarray, tol_pos: float, tol_rot: float) -> bool:
        current = self.tcp_pose()
        err_pos = float(np.linalg.norm(current[:3, 3] - target[:3, 3]))
        err_rot = float(np.linalg.norm(current[:3, :3] - target[:3, :3]))
        return err_pos <= tol_pos and err_rot <= tol_rot


def _rotation_angle(rot_a: np.ndarray, rot_b: np.ndarray) -> float:
    """Ángulo del giro más corto que lleva de una orientación a la otra, en radianes."""
    cos = (float(np.trace(rot_a.T @ rot_b)) - 1.0) / 2.0
    return float(np.arccos(np.clip(cos, -1.0, 1.0)))


def _ease(alpha: float) -> float:
    """
    Suavizado en S de la interpolación (arranque y frenada progresivos).

    Interpolar lineal en alpha da velocidad constante, y por tanto un salto de
    velocidad instantáneo al empezar y al parar. Ese impulso es justo lo que arranca
    el bulto de entre los dedos: la pinza del Panda cierra con ~2.5 N y el bloque pesa
    1.47 N, así que el margen no da para tirones.
    """
    return alpha * alpha * (3.0 - 2.0 * alpha)
