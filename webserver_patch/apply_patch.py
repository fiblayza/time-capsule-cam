"""
Run once on the Pi to patch the upstream webserver.
Safe to run multiple times (idempotent).

Patches applied:
  1. server.py — blueprint registration: adds /api/status endpoint.
  2. server.py — MIME type fix in serve_recording: .mp4 served as video/mp4.
  3. base.html  — recording status badge in the nav header + polling JS.
"""
from pathlib import Path
import sys

# Newer upstream images (Dec 2025+, trixie) install to /opt; older ones to /home/admin
_CANDIDATES = [
    Path("/opt/rotary-phone-audio-guestbook"),
    Path(__file__).parent.parent.parent / "rotary-phone-audio-guestbook",
]
UPSTREAM_DIR  = next((p for p in _CANDIDATES if p.is_dir()), _CANDIDATES[-1])
SERVER_PATH   = UPSTREAM_DIR / "webserver" / "server.py"
BASE_HTML_PATH = UPSTREAM_DIR / "webserver" / "templates" / "base.html"

# ── Patch 1: blueprint registration ──────────────────────────────────────────

# Absolute path baked in at patch time — upstream may live in /opt while we
# stay in /home/admin, so a path relative to server.py can't reach us.
INJECT_IMPORTS = (
    f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
    "from status_api import status_bp\n"
)
INJECT_REGISTER = "app.register_blueprint(status_bp)\n"

MARKER_IMPORTS  = "# [time-capsule-cam] status blueprint imports"
MARKER_REGISTER = "# [time-capsule-cam] status blueprint register"

# ── Patch 2: dynamic MIME type in serve_recording ─────────────────────────────

MIME_DETECT_LINE = "    mime_type = {'.mp4': 'video/mp4', '.jpg': 'image/jpeg'}.get(Path(filename).suffix.lower(), 'audio/wav')\n"
MIME_ANCHOR      = "    file_size = file_path.stat().st_size\n"
MARKER_MIME      = "# [time-capsule-cam] dynamic mime type"

# ── Patch 3: status badge in base.html ────────────────────────────────────────

MARKER_BADGE  = "<!-- [time-capsule-cam] recording status badge -->"
MARKER_POLL   = "<!-- [time-capsule-cam] status polling -->"

NAV_ANCHOR = '<nav class="flex items-center space-x-4">'

BADGE_HTML = f"""{NAV_ANCHOR}
          {MARKER_BADGE}
          <div id="status-badge" class="flex flex-col items-center" title="Recording status">
            <div class="w-6 h-6 flex items-center justify-center mb-1">
              <span id="status-dot" class="block w-3 h-3 rounded-full bg-gray-400 dark:bg-gray-500 transition-colors duration-300"></span>
            </div>
            <span id="status-label" class="text-xs hidden sm:block">IDLE</span>
          </div>"""

POLL_JS = f"""{MARKER_POLL}
<script>
  (function () {{
    var dot   = document.getElementById('status-dot');
    var label = document.getElementById('status-label');
    if (!dot || !label) return;

    var colors = {{
      idle:      'bg-gray-400 dark:bg-gray-500',
      recording: 'bg-red-500',
      saving:    'bg-yellow-400',
      unknown:   'bg-gray-400 dark:bg-gray-500',
    }};
    var labels = {{
      idle: 'IDLE', recording: '⏺ REC', saving: 'SAVING', unknown: '?',
    }};

    function update(status) {{
      var c = colors[status] || colors.unknown;
      dot.className = 'block w-3 h-3 rounded-full transition-colors duration-300 ' + c;
      label.textContent = labels[status] || status.toUpperCase();
    }}

    async function poll() {{
      try {{
        var r = await fetch('/api/status');
        var data = await r.json();
        update(data.status);
      }} catch (_) {{
        update('unknown');
      }}
    }}

    poll();
    setInterval(poll, 2000);
  }})();
</script>
<script src="/tcc/videos.js" defer></script>
</html>"""


# ── Patch functions ───────────────────────────────────────────────────────────

def patch_blueprint(src: str) -> str:
    lines = src.splitlines(keepends=True)
    last_import_idx = 0
    for i, line in enumerate(lines):
        if line.startswith(("import ", "from ")):
            last_import_idx = i

    insert_block = f"\n{MARKER_IMPORTS}\n{INJECT_IMPORTS}\n"
    lines.insert(last_import_idx + 1, insert_block)

    src2 = "".join(lines)
    # anchor on app.secret_key: it's a single line, whereas `app = Flask(`
    # spans several — inserting after its first line would break the call
    register_anchor = "app.secret_key"
    idx = src2.find(register_anchor)
    if idx == -1:
        sys.exit("ERROR: Could not find `app.secret_key` in server.py")
    eol = src2.index("\n", idx)
    register_block = f"\n{MARKER_REGISTER}\n{INJECT_REGISTER}"
    return src2[:eol + 1] + register_block + src2[eol + 1:]


def patch_mime_type(src: str) -> str:
    if MIME_ANCHOR not in src:
        print("WARNING: MIME anchor not found in serve_recording — skipping MIME patch.")
        return src
    src = src.replace(
        MIME_ANCHOR,
        MIME_ANCHOR + f"    {MARKER_MIME}\n" + MIME_DETECT_LINE,
        1,
    )
    src = src.replace("mimetype='audio/wav'", "mimetype=mime_type")
    return src


def patch_base_html(src: str) -> str:
    if NAV_ANCHOR not in src:
        print("WARNING: nav anchor not found in base.html — skipping badge patch.")
        return src

    # Inject badge right after the opening <nav> tag
    src = src.replace(NAV_ANCHOR, BADGE_HTML, 1)

    # Inject polling script — replace closing </html> with script + </html>
    src = src.replace("</html>", POLL_JS, 1)

    return src


# ── Main ─────────────────────────────────────────────────────────────────────

def patch():
    for path in (SERVER_PATH, BASE_HTML_PATH):
        if not path.exists():
            sys.exit(f"ERROR: {path} not found — is the upstream repo cloned?")

    # ── server.py patches ─────────────────────────────────────────────────────
    src = SERVER_PATH.read_text()
    changed = False

    if MARKER_IMPORTS in src:
        print("Patch 1 (blueprint) already applied.")
    else:
        src = patch_blueprint(src)
        changed = True
        print("Patch 1 (blueprint) applied.")

    if MARKER_MIME in src:
        print("Patch 2 (MIME type) already applied.")
    else:
        src = patch_mime_type(src)
        changed = True
        print("Patch 2 (MIME type) applied.")

    if changed:
        SERVER_PATH.write_text(src)
        print(f"server.py updated.")

    # ── base.html patch ───────────────────────────────────────────────────────
    html = BASE_HTML_PATH.read_text()
    if MARKER_BADGE in html:
        print("Patch 3 (status badge) already applied.")
    else:
        html = patch_base_html(html)
        BASE_HTML_PATH.write_text(html)
        print("Patch 3 (status badge) applied.")

    print("Done.")


if __name__ == "__main__":
    patch()
