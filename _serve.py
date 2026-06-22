"""Dev launcher: build the app, register the Kfar manifest, serve the
frontend, and run uvicorn.

Port is configurable via the PORT env var. Default is 8800 because 8000 is
in the Windows reserved/excluded port range on this machine (bind fails with
WinError 10013).

The frontend (web/index.html) is served at "/" from the same origin as the
API, so the page can call /automations, /runs, ... with no CORS/proxy.
"""
import os
import pathlib
import sys

# Make the platform + customer packages importable without needing PYTHONPATH
# set in the environment, so `python _serve.py` just works.
_HERE = pathlib.Path(__file__).resolve().parent
for _p in (_HERE / "src", _HERE.parent / "Kfar-Hanasi-Automations" / "src"):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import uvicorn
from fastapi.responses import FileResponse

from er_automations.persistence import models
from er_automations.persistence.db import init_db
from er_automations.server.app import build_app
from kfar_hanasi.manifest import (
    AUTOMATION_CUSTOMER,
    AUTOMATION_KEY,
    AUTOMATION_NAME,
    build_manifest,
)

PORT = int(os.environ.get("PORT", "8800"))
WEB_INDEX = pathlib.Path(__file__).parent / "web" / "index.html"

root = pathlib.Path("./data")
root.mkdir(exist_ok=True)
app = build_app(root / "automations.sqlite", root)
app.state.manifests[AUTOMATION_KEY] = build_manifest()

c = init_db(root / "automations.sqlite")
models.register_automation(c, AUTOMATION_KEY, AUTOMATION_NAME, AUTOMATION_CUSTOMER)
c.commit()
c.close()


@app.get("/")
def _index() -> FileResponse:
    """Serve the single-file frontend."""
    return FileResponse(WEB_INDEX)


if __name__ == "__main__":
    print(f"ER Automations -> http://127.0.0.1:{PORT}   (API docs: /docs)")
    uvicorn.run(app, host="127.0.0.1", port=PORT)
