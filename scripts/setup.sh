#!/usr/bin/env bash
# Prepara el entorno. Idempotente: se puede relanzar.
#
#   bash scripts/setup.sh
#   SDK=/otra/ruta/backend bash scripts/setup.sh    # otro checkout de la plataforma
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/.venv"
THIRD="$REPO/third_party"

echo "==> Repositorio: $REPO"

if [ ! -d "$VENV" ]; then
  echo "==> Creando entorno virtual"
  python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --upgrade pip -q

echo "==> Instalando dependencias"
pip install -q -r "$REPO/requirements.txt"

# El SDK de telemetría NO vive aquí: es el contrato de lo que la plataforma guarda, y
# quien define la forma del dato es quien lo almacena. Se instala editable para que un
# cambio allí se vea sin reinstalar.
#
# La ruta importa, y no es la obvia: hace falta la rama `dev` (o algo que salga de ella),
# que es la que trae el ciclo de vida del episodio (`begin`/`event`/`end`) y lo que hace
# que la pantalla Live esté viva. Instalar otra no falla aquí; falla al correr, y para
# entonces ya hay episodios a medias. Por eso se comprueba abajo.
SDK="${SDK:-$REPO/../../orca/workspaces/Platform/main/backend}"
if [ -d "$SDK" ]; then
  echo "==> Instalando el SDK de telemetría desde $SDK"
  pip install -q -e "$SDK"
  python - "$SDK" <<'PY'
import sys
from theker_telemetry import RunLog
if not hasattr(RunLog, "begin"):
    sys.exit(f"ERROR: el SDK de {sys.argv[1]} no trae el ciclo de vida del episodio.\n"
             f"       Necesitas la rama dev de Platform.")
PY
else
  echo "==> AVISO: no encuentro el SDK en $SDK"
  echo "    La simulación corre igual y escribe en runs/; lo que no podrá es subir"
  echo "    a Supabase. Pasa la ruta con SDK=... si la tienes en otro sitio."
fi

echo "==> Modelo del brazo (MuJoCo Menagerie)"
mkdir -p "$THIRD"
if [ ! -e "$THIRD/mujoco_menagerie" ]; then
  git clone --depth 1 --filter=blob:none --sparse \
    https://github.com/google-deepmind/mujoco_menagerie.git "$THIRD/mujoco_menagerie"
  git -C "$THIRD/mujoco_menagerie" sparse-checkout set franka_emika_panda
fi

echo "==> Comprobando"
python - <<'PY'
import mujoco, mink  # noqa: F401
print("  MuJoCo :", mujoco.__version__)
print("  mink   : ok")
try:
    import theker_telemetry  # noqa: F401
    print("  telemetría: ok")
except ImportError:
    print("  telemetría: no instalada (solo disco)")
PY

cat <<EOF

============================================================
 Entorno listo.

   source $VENV/bin/activate
   python scripts/palletize.py --viewer      # verlo
   python scripts/palletize.py -n 3          # medirlo, headless
   python tests/test_pallet.py               # comprobaciones
============================================================
EOF
