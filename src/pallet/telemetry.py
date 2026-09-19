"""
La costura con la plataforma: medidas del episodio -> filas de Supabase, en vivo.

**Este es el único módulo de la simulación que sabe que la plataforma existe.** Todo lo
demás produce medidas y no tiene ni idea de dónde acaban ni de cómo se llaman las
columnas. Si mañana cambia el contrato, se toca aquí y en ningún otro sitio.

Se escribe con el CICLO DE VIDA del episodio, no de golpe al terminar:

    begin()  ->  event() / placement() / pallet_state()  ->  end()

y ése es todo el motivo por el que la pantalla Live está viva. `RunLog.episode()` sube
el episodio ya acabado con sus filas hijas de una vez, y con eso nunca existe una fila
`running` ni llega el `UPDATE` de cierre: dos de las tres señales a las que Live se
suscribe no se disparan jamás y el jurado ve el palé aparecer ya montado.

No se mezclan los dos caminos. Llamar a `episode()` y a `begin()` en el mismo episodio
insertaría la fila dos veces y el `unique(run_id, seed)` tumbaría la segunda.

Orden sagrado, heredado de la plataforma: **el disco primero**. Las imágenes se guardan
en `runs/<ts>/<seed>/` pase lo que pase, y `end()` escribe el `jsonl` antes de tocar la
red. Los fallos remotos no se gestionan aquí: el SDK avisa una vez, apaga la subida y
el episodio sigue.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import imageio.v3 as iio
import numpy as np
from theker_telemetry import EpisodeResult, RunLog

from src.pallet import measure
from src.pallet.episode import Episode
from src.pallet.scene import PalletScene

# Bucket de Storage donde van las dos vistas del palé. Se crea solo la primera vez.
BUCKET = "snapshots"


class RunLogSink:
    """
    Traduce las medidas del episodio a las llamadas del SDK, según ocurren.

    Es lo que `run_episode(..., sink=...)` va llamando. Cada método sube UNA fila.
    """

    def __init__(self, log: RunLog, scene: PalletScene):
        self.log = log
        self.scene = scene

    # ── el ciclo de vida ─────────────────────────────────────────────────────

    def begin(self, seed: int) -> None:
        """Abre el episodio en curso: es lo que hace que Live lo vea aparecer."""
        self.log.begin(seed, n_objects=len(self.scene.boxes))

    def end(self, episode: Episode) -> EpisodeResult:
        """Cierra el episodio. El `PATCH` es el aviso de "terminado" que Live espera.

        Las dos vistas del palé se suben ANTES del cierre, porque sus URLs viajan en las
        métricas del episodio y el `PATCH` es lo que las escribe.
        """
        urls = upload_snapshots(self.log, episode)
        result = episode_result(episode, self.scene, snapshot_urls=urls)
        self.log.end(result)
        return result

    # ── las filas, una a una ────────────────────────────────────────────────

    def placement(self, index: int, placement: measure.Placement) -> None:
        self.log.placement(**placement_row(index, placement))

    def pallet_state(self, index: int, state: measure.PalletState,
                     drift: float) -> None:
        self.log.pallet_state(**pallet_state_row(index, state, drift))

    def event(self, row: dict) -> None:
        self.log.event(**row)


# ─────────────────────────────────────────────────────────────────────────────
# Las filas. Los nombres de estas claves SON el contrato: cada una es una columna.
# ─────────────────────────────────────────────────────────────────────────────

def placement_row(index: int, p: measure.Placement) -> dict:
    """
    Una caja intentada. `seq` es su índice en el guion, y es único en el episodio.

    La fila se escribe UNA vez, en el momento de colocar, y ya no se vuelve a tocar:
    el SDK solo sabe insertar filas hijas. Si una colocación que era buena se estropea
    después porque el montón se mueve, eso no se ve aquí — se ve en la traza de CoG,
    en el evento `fail` y en el `n_placed` final del episodio, que sí se remide.
    """
    return {
        "seq": index,
        "package_id": p.box.name,
        "package_type": p.box.type_name,
        "mass_kg": _r(p.box.mass),
        "dims_m": [_r(d) for d in p.box.dims],
        "layer": p.box.layer,
        "planned_pose": _pose(p.planned, 0.0),
        "actual_pose": _pose(p.position, p.yaw),
        "error_xy_m": _r(p.error_xy, 5),
        "error_yaw_rad": _r(p.error_yaw, 5),
        "support_ratio": _r(p.support_ratio),
        "overhang_m": _r(p.overhang),
        "placed": bool(p.placed),
    }


def pallet_state_row(index: int, state: measure.PalletState, drift: float) -> dict:
    """
    El montón tras ese intento: esto ES la traza del centro de gravedad.

    Hay una fila por paquete INTENTADO, incluido el que derrumba el montón. Es justo la
    que hace que la traza cruce el cero: si solo se emitieran los intentos que salieron
    bien, el gráfico acabaría en verde en un episodio que se cayó.
    """
    return {
        "after_seq": index,
        "mass_kg": _r(state.mass_kg),
        "cog_x": _r(state.cog[0]),
        "cog_y": _r(state.cog[1]),
        "cog_z": _r(state.cog[2]),
        "stability_margin_m": _r(state.stability_margin),
        "fill_ratio": _r(state.fill_ratio),
        "settle_drift_m": _r(drift),
    }


def episode_result(episode: Episode, scene: PalletScene,
                   snapshot_urls: dict[str, str] | None = None) -> EpisodeResult:
    """El resumen del episodio, con las métricas de paletizado en `metrics`."""
    state = episode.states[-1] if episode.states else None
    on_pallet = measure.on_pallet(scene, episode.placements)
    pallet = scene.pallet["pallet"]
    return EpisodeResult(
        seed=episode.seed,
        level=int(scene.pallet["episode"]["level"]),
        n_objects=episode.n_objects,
        n_placed=episode.n_placed,
        # Solo significa algo en los niveles de clasificación de inducción. Aquí no hay
        # destinos que confundir, pero el campo es obligatorio en el contrato.
        n_misrouted=0,
        success=episode.success,
        duration_s=episode.duration_s,
        failure=episode.failure,
        # Las poses de los huecos se conocen de antemano: no hay percepción que pueda
        # equivocarse. Eso es exactamente lo que `oracle` significa en este repo, y la
        # interfaz ya sabe que un run así no se compara con uno que sí mira imágenes.
        oracle=True,
        task="palletizing",
        metrics={
            "cog_offset_xy": _r(np.linalg.norm(state.cog[:2])) if state else 0.0,
            "fill_ratio": _r(state.fill_ratio) if state else 0.0,
            "settle_drift": _r(max(episode.drifts)) if episode.drifts else 0.0,
            "n_layers": max((p.box.layer for p in on_pallet), default=0),
            "layer_flatness": _r(measure.layer_flatness(on_pallet)),
            "max_overhang": _r(max((p.overhang for p in on_pallet), default=0.0)),
            "score": round(episode.n_placed / episode.n_objects, 4)
            if episode.n_objects else 0.0,
            # El sitio de esto es `runs.config`, pero `RunLog` todavía no deja mandarlo
            # y la interfaz dibuja a escala real: sin el tamaño de verdad supone un palé
            # europeo de 1200x800 y pinta esta maqueta seis veces más grande. Va aquí
            # mientras tanto, que `metrics` es jsonb libre y no se pierde el dato.
            "pallet_size_m": [_r(d) for d in pallet["dims"]],
            "pallet_scale": float(pallet["scale"]),
            # Las dos vistas del palé terminado. Su sitio natural es una tabla
            # `snapshots` con su `after_seq`, que la plataforma todavía no tiene; hasta
            # entonces viajan aquí, que `metrics` es jsonb libre y es el punto de
            # extensión del contrato. Mover esto después es cambiar de dónde lo lee el
            # front, no volver a producirlo.
            **{f"snapshot_{view}_url": url
               for view, url in (snapshot_urls or {}).items()},
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Imágenes a Supabase Storage.
#
# El SDK no sabe de Storage: cuelga de `/storage/v1` y no de `/rest/v1`, y el cuerpo es
# el fichero en crudo, no JSON. Son treinta líneas de `urllib` y se hablan desde aquí,
# reutilizando la clave y el timeout del cliente que ya existe.
# ─────────────────────────────────────────────────────────────────────────────

_storage_warned = False


def _root(client) -> str:
    """La raíz del proyecto. El cliente solo guarda la de PostgREST."""
    return client.base.removesuffix("/rest/v1")


def _request(client, url: str, *, data=None, method="GET", content_type=None) -> bytes:
    headers = {"apikey": client.key, "Authorization": f"Bearer {client.key}"}
    if content_type:
        headers["Content-Type"] = content_type
        headers["x-upsert"] = "true"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=client.timeout) as response:
        return response.read()


def ensure_bucket(client, bucket: str = BUCKET) -> None:
    """
    Crea el bucket si no existe. Público en lectura, para que las URLs valgan tal cual.

    "Ya existe" es el caso normal a partir de la segunda vez, y hay que reconocerlo por
    el CUERPO y no por el código HTTP: Supabase contesta 400 con un `statusCode: 409`
    dentro del JSON, así que mirar `err.code` no basta.
    """
    try:
        _request(client, f"{_root(client)}/storage/v1/bucket",
                 data=f'{{"id":"{bucket}","name":"{bucket}","public":true}}'.encode(),
                 method="POST", content_type="application/json")
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")
        if err.code != 409 and "already exists" not in detail.lower():
            raise RuntimeError(f"no se pudo crear el bucket {bucket}: "
                               f"HTTP {err.code} {detail[:200]}") from err


def upload_snapshots(log: RunLog, episode: Episode) -> dict[str, str]:
    """
    Sube la cenital y el alzado, y devuelve sus URLs públicas por vista.

    El fallo se captura aquí y no se propaga. Una foto que no sube no puede llevarse por
    delante el episodio entero —sus métricas, su traza de CoG y sus eventos—, que es lo
    que pasaría si dejáramos que el error subiera hasta el `_try` del SDK: ése apaga el
    remoto para el resto de la ejecución.
    """
    global _storage_warned
    client = getattr(log, "client", None)
    if client is None or not log.run_id or not episode.snapshots:
        return {}

    urls: dict[str, str] = {}
    try:
        ensure_bucket(client)
        for view, image in episode.snapshots.items():
            path = f"{log.run_id}/{episode.seed}/{view}.png"
            _request(client, f"{_root(client)}/storage/v1/object/{BUCKET}/{path}",
                     data=iio.imwrite("<bytes>", image, extension=".png"),
                     method="POST", content_type="image/png")
            urls[view] = f"{_root(client)}/storage/v1/object/public/{BUCKET}/{path}"
    except (urllib.error.URLError, OSError, RuntimeError) as err:
        if not _storage_warned:
            detail = (err.read().decode("utf-8", "replace")[:200]
                      if isinstance(err, urllib.error.HTTPError) else err)
            print(f"  aviso: no se pudieron subir las imágenes ({detail}). "
                  f"El episodio se sube igual y las fotos quedan en disco.")
            _storage_warned = True
        return {}
    return urls


# ─────────────────────────────────────────────────────────────────────────────
# Imágenes. Hoy solo a disco: la plataforma aún no tiene dónde guardarlas.
# ─────────────────────────────────────────────────────────────────────────────

def save_snapshots(episode: Episode, directory: Path) -> dict[str, Path]:
    """
    Guarda la cenital y el alzado del palé terminado. Esto ocurre siempre.

    Subirlas a Supabase está pendiente del otro lado: hace falta un bucket, una tabla
    `snapshots` y un `RunLog.snapshot()`. Mientras no exista, las imágenes se quedan
    aquí y no se pierden; enchufarlas después son unas pocas líneas en este módulo.
    """
    directory.mkdir(parents=True, exist_ok=True)
    out = {}
    for view, image in episode.snapshots.items():
        path = directory / f"{view}.png"
        iio.imwrite(path, image)
        out[view] = path
    return out


def _pose(position, yaw: float) -> dict:
    """`{x, y, z, yaw}` en metros y radianes, que es lo que espera la interfaz."""
    return {"x": _r(position[0]), "y": _r(position[1]), "z": _r(position[2]),
            "yaw": _r(yaw)}


def _r(value, digits: int = 4) -> float:
    """Redondeo en la frontera. Milímetros de resolución sobran para un palé."""
    return round(float(value), digits)
