"""
Run once on the Pi to patch the upstream webserver/server.py.
Safe to run multiple times (idempotent).

Patches applied:
  1. Blueprint registration — adds /api/status endpoint.
  2. MIME type fix in serve_recording — serves .mp4 with video/mp4
     instead of the hardcoded audio/wav so browsers can play video.
"""
from pathlib import Path
import sys

SERVER_PATH = Path(__file__).parent.parent.parent / "rotary-phone-audio-guestbook" / "webserver" / "server.py"

# ── Patch 1: blueprint registration ──────────────────────────────────────────

INJECT_IMPORTS = (
    "import sys; sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'time-capsule-cam' / 'webserver_patch'))\n"
    "from status_api import status_bp\n"
)
INJECT_REGISTER = "app.register_blueprint(status_bp)\n"

MARKER_IMPORTS  = "# [time-capsule-cam] status blueprint imports"
MARKER_REGISTER = "# [time-capsule-cam] status blueprint register"

# ── Patch 2: dynamic MIME type in serve_recording ─────────────────────────────

# Line injected right after `file_size = file_path.stat().st_size`
MIME_DETECT_LINE = "    mime_type = 'video/mp4' if filename.lower().endswith('.mp4') else 'audio/wav'\n"
MIME_ANCHOR      = "    file_size = file_path.stat().st_size\n"
MARKER_MIME      = "# [time-capsule-cam] dynamic mime type"


def patch_blueprint(src: str) -> str:
    """Add blueprint import + registration. Returns modified source."""
    lines = src.splitlines(keepends=True)

    last_import_idx = 0
    for i, line in enumerate(lines):
        if line.startswith(("import ", "from ")):
            last_import_idx = i

    insert_block = f"\n{MARKER_IMPORTS}\n{INJECT_IMPORTS}\n"
    lines.insert(last_import_idx + 1, insert_block)

    src2 = "".join(lines)
    flask_init_marker = "app = Flask("
    idx = src2.find(flask_init_marker)
    if idx == -1:
        sys.exit("ERROR: Could not find `app = Flask(` in server.py")
    eol = src2.index("\n", idx)
    register_block = f"\n{MARKER_REGISTER}\n{INJECT_REGISTER}"
    return src2[:eol + 1] + register_block + src2[eol + 1:]


def patch_mime_type(src: str) -> str:
    """Replace hardcoded audio/wav MIME type with dynamic detection."""
    if MARKER_MIME in src:
        return src  # already patched

    if MIME_ANCHOR not in src:
        print("WARNING: Could not find MIME anchor line in serve_recording — skipping MIME patch.")
        return src

    # Inject mime_type detection line after the file_size line
    src = src.replace(
        MIME_ANCHOR,
        MIME_ANCHOR + f"    {MARKER_MIME}\n" + MIME_DETECT_LINE,
        1,
    )

    # Replace both hardcoded occurrences inside serve_recording
    src = src.replace("mimetype='audio/wav'", "mimetype=mime_type")

    return src


def patch():
    if not SERVER_PATH.exists():
        sys.exit(f"ERROR: {SERVER_PATH} not found — is the upstream repo cloned?")

    src = SERVER_PATH.read_text()
    changed = False

    # Patch 1 — blueprint
    if MARKER_IMPORTS in src:
        print("Patch 1 (blueprint) already applied.")
    else:
        src = patch_blueprint(src)
        changed = True
        print("Patch 1 (blueprint) applied.")

    # Patch 2 — MIME type
    if MARKER_MIME in src:
        print("Patch 2 (MIME type) already applied.")
    else:
        src = patch_mime_type(src)
        changed = True
        print("Patch 2 (MIME type) applied.")

    if changed:
        SERVER_PATH.write_text(src)
        print(f"server.py updated: {SERVER_PATH}")
    else:
        print("Nothing to do — all patches already applied.")


if __name__ == "__main__":
    patch()
