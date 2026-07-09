from flask import Blueprint, jsonify, send_from_directory
from pathlib import Path
import json

status_bp = Blueprint("status", __name__)
TCC_PATCH_DIR = Path(__file__).resolve().parent
STATUS_PATH = TCC_PATCH_DIR.parent / "status.json"


@status_bp.route("/api/status")
def get_status():
    try:
        return jsonify(json.loads(STATUS_PATH.read_text()))
    except Exception:
        return jsonify({"status": "unknown"})


@status_bp.route("/tcc/videos.js")
def videos_js():
    return send_from_directory(TCC_PATCH_DIR, "videos.js")
