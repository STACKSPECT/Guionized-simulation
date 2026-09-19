"""
Medida del palé sobre el estado real del simulador.

**Esta es la parte que no está guionizada.** Dónde va cada caja está escrito a mano en
`configs/pallet.yaml`, pero dónde ACABA cada caja lo decide la física, y todo lo que se
sube a la plataforma sale de medirlo aquí: el error contra el hueco planificado, cuánto
apoya, cuánto sobresale, dónde queda el centro de gravedad y con qué margen aguanta.

Por eso estos números valen como línea base aunque el plan sea un puzle resuelto de
antemano: lo que miden es la puntería del brazo y el comportamiento del montón, no las
ganas del guion.

El margen de estabilidad se calcula aquí, y **es una copia**. El original vive en
`backend/seed/palletizing.py` de la plataforma, que es un script de sembrado y no es
importable desde este entorno sin meter su carpeta en `sys.path` a mano. Mientras siga
duplicado, lo que impide que las dos versiones discrepen es el test que ancla los
valores (`tests/test_pallet.py`). Petición abierta a la plataforma: subirlo a
`theker_telemetry/pallet.py` y que esta copia desaparezca.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.pallet.scene import Box, PalletScene, box_pose, planned_pose

# Rectángulo en planta: (centro x, centro y, ancho x, ancho y).
Rect = tuple[float, float, float, float]

# Un palé no vuelca porque el CoG se salga del palé: vuelca cuando, frenando o girando,
# el momento de vuelco supera al de restitución. Con una aceleración lateral `a`, el
# montón cae si `a/g > d/h`, donde `d` es la distancia del CoG al borde del APOYO y `h`
# su altura. De ahí que el margen sea `d - (a/g)*h`: apilar alto y descentrado lo empeora
# a la vez, que es justo el compromiso que el paletizado tiene que resolver.
#
# 0.28 g es un giro normal de carretilla. El listón se pone ahí porque lo que se pide es
# un palé que aguante el TRANSPORTE, no uno que se sostenga quieto.
TRANSPORT_ACCEL_G = 0.28


@dataclass
class Placement:
    """Una caja ya depositada, medida. Los campos son los de la tabla `placements`."""

    box: Box
    position: np.ndarray                 # centro real, en frame del palé
    yaw: float                           # radianes
    planned: np.ndarray                  # centro planificado, en frame del palé
    error_xy: float
    error_yaw: float
    support_ratio: float
    overhang: float
    placed: bool

    @property
    def rect(self) -> Rect:
        return footprint(self.position[0], self.position[1], self.box.dims, self.yaw)

    @property
    def top_z(self) -> float:
        return float(self.position[2]) + self.box.dims[2] / 2.0


@dataclass
class PalletState:
    """El montón tras una colocación. Los campos son los de `pallet_states`."""

    mass_kg: float
    cog: np.ndarray                      # (x, y, z) en frame del palé
    stability_margin: float
    fill_ratio: float


# ─────────────────────────────────────────────────────────────────────────────
# Geometría en planta
# ─────────────────────────────────────────────────────────────────────────────

def footprint(x: float, y: float, dims, yaw: float) -> Rect:
    """
    Huella en planta de una caja girada `yaw`, como rectángulo alineado con los ejes.

    Es la envolvente del rectángulo girado, no el rectángulo girado: exacta con yaw = 0,
    que es el caso nominal, y por arriba con cualquier otro. Se elige por arriba a
    propósito — sobreestima la huella, así que `overhang` sale mayor y `support_ratio`
    peor solo en la medida en que la caja llegó torcida, y una caja torcida ya es un
    fallo que se ve en `error_yaw`. Recortar polígonos girados de verdad son cincuenta
    líneas que no cambian ninguna decisión.
    """
    cos, sin = abs(np.cos(yaw)), abs(np.sin(yaw))
    return (float(x), float(y),
            float(dims[0] * cos + dims[1] * sin),
            float(dims[0] * sin + dims[1] * cos))


def rect_overlap(a: Rect, b: Rect) -> float:
    """Área de solape entre dos rectángulos en planta, en m2."""
    ax, ay, adx, ady = a
    bx, by, bdx, bdy = b
    dx = min(ax + adx / 2, bx + bdx / 2) - max(ax - adx / 2, bx - bdx / 2)
    dy = min(ay + ady / 2, by + bdy / 2) - max(ay - ady / 2, by - bdy / 2)
    return max(0.0, dx) * max(0.0, dy)


def support_ratio(rect: Rect, supports: list[Rect]) -> float:
    """
    Fracción de la base de la caja que apoya en algo sólido, de 0 a 1.

    `supports` son las huellas de lo que hay justo debajo: la cubierta del palé para la
    capa 1, y las cajas de la capa anterior para el resto. Se suman los solapes sin
    descontar intersecciones entre soportes, porque las cajas de una capa no se solapan
    entre sí; el `min(1.0, ...)` es el cinturón por si alguna vez lo hicieran.

    Es el número que dice si una caja se quedó en voladizo, que es el paso previo a que
    el montón se venga abajo.
    """
    area = rect[2] * rect[3]
    if area <= 0.0:
        return 0.0
    return min(1.0, sum(rect_overlap(rect, s) for s in supports) / area)


def support_polygon(base: list[Rect]) -> tuple[float, float, float, float]:
    """
    Rectángulo que apoya en el palé: la envolvente de las huellas de la capa 1.

    Lo que sostiene el montón es lo que toca la cubierta, no el palé entero. Un palé con
    una sola caja en una esquina tiene un apoyo pequeño aunque la tabla sea enorme.
    """
    xs0 = [x - dx / 2 for x, _, dx, _ in base]
    xs1 = [x + dx / 2 for x, _, dx, _ in base]
    ys0 = [y - dy / 2 for _, y, _, dy in base]
    ys1 = [y + dy / 2 for _, y, _, dy in base]
    return min(xs0), max(xs1), min(ys0), max(ys1)


def stability_margin(cog_x: float, cog_y: float, cog_z: float,
                     base: list[Rect]) -> float:
    """
    Margen de estabilidad en metros. **Negativo = vuelca en la primera frenada.**

    Se mide contra el borde del POLÍGONO DE SOPORTE, no contra el del palé. Contra el
    palé los números salen optimistas y la pantalla diría que todo va bien hasta el
    derrumbe, que es exactamente el aviso que este indicador tiene que dar antes.

    Sin nada apoyado devuelve 0.0: no hay polígono que medir, y eso no es un vuelco.
    """
    if not base:
        return 0.0
    x0, x1, y0, y1 = support_polygon(base)
    d_edge = min(cog_x - x0, x1 - cog_x, cog_y - y0, y1 - cog_y)
    return d_edge - TRANSPORT_ACCEL_G * cog_z


def overhang(rect: Rect, pallet: Rect) -> float:
    """Cuánto sobresale del palé la caja, en metros. 0 si cabe entera."""
    x, y, dx, dy = rect
    px, py, pdx, pdy = pallet
    return max(0.0,
               (x + dx / 2) - (px + pdx / 2),
               (px - pdx / 2) - (x - dx / 2),
               (y + dy / 2) - (py + pdy / 2),
               (py - pdy / 2) - (y - dy / 2))


# ─────────────────────────────────────────────────────────────────────────────
# Medida sobre el simulador
# ─────────────────────────────────────────────────────────────────────────────

def to_pallet_frame(scene: PalletScene, position) -> np.ndarray:
    """
    De coordenadas del mundo al frame del palé: origen en el centro de la cubierta.

    Es el frame en el que dibuja la interfaz, así que la conversión se hace aquí, una
    vez, y lo que sale de este módulo ya está listo para subir.
    """
    center = np.asarray(scene.pallet["pallet"]["center"], float)
    pos = np.asarray(position, float)
    return np.array([pos[0] - center[0], pos[1] - center[1], pos[2] - scene.deck_top])


def pallet_rect(scene: PalletScene) -> Rect:
    """La cubierta del palé como rectángulo, ya en frame del palé."""
    dx, dy = scene.pallet["pallet"]["dims"]
    return (0.0, 0.0, float(dx), float(dy))


def on_pallet(scene: PalletScene, placements: list[Placement]) -> list[Placement]:
    """
    Las cajas que de verdad están SOBRE el palé, de entre las que se han intentado.

    No es lo mismo que `placed`, y la diferencia importa en los dos sentidos:

      - una caja fuera de tolerancia SÍ está encima, pesa y mueve el centro de gravedad;
      - una que se escurrió de la pinza y se quedó en la mesa NO, aunque su intento haya
        dejado su fila de colocación.

    El criterio es geométrico —que la huella toque el palé— y no "¿falló la maniobra?":
    así también sale de la cuenta una caja que otra empuje fuera a mitad del episodio.
    """
    pallet = pallet_rect(scene)
    return [p for p in placements if rect_overlap(p.rect, pallet) > 0.0]


def measure_placement(scene: PalletScene, box: Box, index: int,
                      done: list[Placement]) -> Placement:
    """
    Mide una caja recién depositada contra el hueco que le tocaba.

    `done` son las colocaciones anteriores, que es lo que define sobre qué apoya ésta.
    """
    episode = scene.pallet["episode"]
    entry = scene.pallet["script"][index]

    world_pos, yaw = box_pose(scene, box)
    position = to_pallet_frame(scene, world_pos)
    planned = to_pallet_frame(scene, planned_pose(scene.pallet, entry, scene.deck_top))

    rect = footprint(position[0], position[1], box.dims, yaw)
    # Lo que sostiene esta caja: el palé si está en la capa 1, y si no, lo que se
    # colocó en la capa de debajo. Una caja de la misma capa no sostiene a otra.
    if box.layer <= 1:
        supports = [pallet_rect(scene)]
    else:
        supports = [p.rect for p in done if p.box.layer == box.layer - 1]

    error_xy = float(np.linalg.norm(position[:2] - planned[:2]))
    # Una caja rectangular girada media vuelta está igual de bien puesta; girada un
    # cuarto está atravesada. El periodo sale de la forma, no se da por hecho.
    period = np.pi / 2 if abs(box.dims[0] - box.dims[1]) < 1e-9 else np.pi
    error_yaw = abs((yaw + period / 2) % period - period / 2)
    over = overhang(rect, pallet_rect(scene))

    return Placement(
        box=box,
        position=position,
        yaw=float(yaw),
        planned=planned,
        error_xy=error_xy,
        error_yaw=float(error_yaw),
        support_ratio=support_ratio(rect, supports),
        overhang=over,
        placed=(error_xy <= float(episode["tolerance_xy"])
                and error_yaw <= float(episode["tolerance_yaw"])
                and over <= float(episode["max_overhang"])),
    )


def pallet_state(placements: list[Placement]) -> PalletState:
    """
    El montón tras la última colocación: masa, centro de gravedad y margen.

    **Entra TODO lo depositado, cumpla tolerancia o no.** Una caja mal puesta sigue
    pesando sobre el palé y sigue moviendo el centro de gravedad; filtrarla por `placed`
    deja fuera del cálculo justo al paquete que derrumba el montón, y entonces la traza
    de CoG **acaba en verde en un episodio que se cayó**. El gráfico existe para dejar
    ver venir el fallo varias colocaciones antes: con el filtro no enseña nada.

    El CoG se pondera por la masa real de cada caja, no por su volumen: es lo que hace
    que mandar lo pesado abajo se note en el indicador, que es justo lo que el
    planificador de verdad tendrá que aprender a hacer.
    """
    if not placements:
        return PalletState(0.0, np.zeros(3), 0.0, 0.0)

    mass = sum(p.box.mass for p in placements)
    cog = sum(p.box.mass * p.position for p in placements) / mass
    # Solo apoya en el palé lo que toca la cubierta. Un montón entero en una esquina
    # tiene un apoyo pequeño aunque la tabla sea grande. Aquí tampoco se filtra por
    # `placed`: una caja de la capa 1 fuera de tolerancia sigue siendo lo que sostiene.
    base = [p.rect for p in placements if p.box.layer == 1]
    return PalletState(
        mass_kg=float(mass),
        cog=cog,
        stability_margin=stability_margin(
            float(cog[0]), float(cog[1]), float(cog[2]), base),
        fill_ratio=fill_ratio(placements),
    )


def fill_ratio(placements: list[Placement]) -> float:
    """
    Volumen de carga dividido por el de su envolvente. 1.0 = ni un hueco.

    Mide cómo de compacto quedó el montón, no cuánto palé se usó: un palé con dos cajas
    bien juntas está perfectamente lleno *de lo que hay*, y es la forma honesta de
    leerlo cuando un episodio se corta a la mitad.
    """
    if not placements:
        return 0.0
    volume = sum(p.box.dims[0] * p.box.dims[1] * p.box.dims[2] for p in placements)
    rects = [p.rect for p in placements]
    x0 = min(r[0] - r[2] / 2 for r in rects)
    x1 = max(r[0] + r[2] / 2 for r in rects)
    y0 = min(r[1] - r[3] / 2 for r in rects)
    y1 = max(r[1] + r[3] / 2 for r in rects)
    z1 = max(p.top_z for p in placements)
    envelope = (x1 - x0) * (y1 - y0) * max(z1, 1e-9)
    return float(min(1.0, volume / envelope)) if envelope > 0 else 0.0


def layer_flatness(placements: list[Placement]) -> float:
    """
    Lo plana que quedó la cara superior de la capa más alta, en metros.

    Es la medida directa de lo que pide el apilado: si la cara de arriba de una capa no
    es plana, la capa siguiente se apoya en falso. 0.0 con una sola caja, que no define
    ningún plano.
    """
    if not placements:
        return 0.0
    top_layer = max(p.box.layer for p in placements)
    tops = [p.top_z for p in placements if p.box.layer == top_layer]
    return float(max(tops) - min(tops)) if len(tops) > 1 else 0.0


def positions(scene: PalletScene, boxes: list[Box]) -> dict[str, np.ndarray]:
    """Instantánea de dónde está cada caja, para comparar antes y después."""
    return {b.name: box_pose(scene, b)[0] for b in boxes}


def max_displacement(before: dict[str, np.ndarray],
                     after: dict[str, np.ndarray]) -> float:
    """
    Cuánto se movió la caja que más se movió, en metros.

    Es `settle_drift`: se mide sobre TODO el montón y no solo sobre la caja recién
    soltada, porque lo que avisa de un derrumbe es que la nueva descoloque a las de
    abajo. Una caja que aterriza quieta sobre un montón que se ha corrido entero no es
    un éxito.
    """
    shared = before.keys() & after.keys()
    if not shared:
        return 0.0
    return float(max(np.linalg.norm(after[k] - before[k]) for k in shared))


# ─────────────────────────────────────────────────────────────────────────────

def demo() -> None:
    """Comprobación sin simulador: la geometría en planta y los agregados."""
    # Solape: dos cuadrados de 1 m desplazados medio metro comparten media unidad.
    assert rect_overlap((0, 0, 1, 1), (0.5, 0, 1, 1)) == 0.5
    assert rect_overlap((0, 0, 1, 1), (5, 0, 1, 1)) == 0.0

    # Huella: sin giro es la caja; a 90 grados, la caja con los lados cambiados.
    assert footprint(0, 0, (0.1, 0.05, 0.04), 0.0) == (0.0, 0.0, 0.1, 0.05)
    _, _, dx, dy = footprint(0, 0, (0.1, 0.05, 0.04), np.pi / 2)
    assert round(dx, 6) == 0.05 and round(dy, 6) == 0.1

    # Apoyo: entera sobre el palé; con el centro justo en el borde, la mitad; y fuera.
    pallet = (0.0, 0.0, 0.21, 0.14)
    assert support_ratio((0.0, 0.0, 0.1, 0.07), [pallet]) == 1.0
    assert round(support_ratio((0.105, 0.0, 0.1, 0.07), [pallet]), 3) == 0.5
    assert support_ratio((1.0, 0.0, 0.1, 0.07), [pallet]) == 0.0

    # Voladizo: 5 mm por el lado de las x.
    assert round(overhang((0.06, 0.0, 0.1, 0.07), pallet), 6) == 0.005
    assert overhang((0.0, 0.0, 0.1, 0.07), pallet) == 0.0

    # Agregados sobre la capa 1 de verdad: 4 cajas en rejilla 2x2 sobre el palé.
    class _B:
        def __init__(self, mass, dims, layer):
            self.mass, self.dims, self.layer = mass, dims, layer

    def _p(x, y, mass, layer=1, z=0.0225):
        box = _B(mass, (0.105, 0.07, 0.045), layer)
        pos = np.array([x, y, z])
        return Placement(box, pos, 0.0, pos, 0.0, 0.0, 1.0, 0.0, True)

    def _capa(masas, z=0.0225):
        sitios = [(-0.0525, -0.035), (0.0525, -0.035), (-0.0525, 0.035), (0.0525, 0.035)]
        return [_p(x, y, m, z=z) for (x, y), m in zip(sitios, masas)]

    state = pallet_state(_capa([0.132] * 4))
    assert round(state.mass_kg, 4) == 0.528
    assert np.allclose(state.cog[:2], 0.0)      # simétrico: el CoG cae en el centro
    assert state.stability_margin > 0.0         # centrado y bajo: aguanta

    # Lo mismo, pero con la masa cargada a un lado: el CoG se va y el margen baja.
    # Se desplaza en Y a propósito: el margen lo fija el borde MÁS CERCANO, y en un palé
    # más largo que ancho ese es siempre el del lado corto. Cargarlo en X no lo movería.
    torcido = pallet_state(_capa([0.05, 0.05, 0.30, 0.30]))
    assert torcido.cog[1] > 0.02
    assert torcido.stability_margin < state.stability_margin

    # Apilar alto también lo empeora, con el mismo reparto de masa.
    alto = pallet_state(_capa([0.132] * 4, z=0.10))
    assert alto.stability_margin < state.stability_margin

    # Planitud: toda la capa a la misma altura -> plano.
    assert layer_flatness(_capa([0.132] * 4)) == 0.0
    # Una caja 4 mm más alta que el resto: eso es lo que hereda la capa de encima.
    desnivelada = _capa([0.132] * 4)
    desnivelada[2].position = np.array([-0.0525, 0.035, 0.0265])
    assert round(layer_flatness(desnivelada), 6) == 0.004

    # Deriva: se queda con la caja que más se movió, no con la media.
    before = {"a": np.zeros(3), "b": np.zeros(3)}
    after = {"a": np.array([0.001, 0, 0]), "b": np.array([0.02, 0, 0])}
    assert round(max_displacement(before, after), 4) == 0.02
    print("ok  measure.demo")


if __name__ == "__main__":
    demo()
