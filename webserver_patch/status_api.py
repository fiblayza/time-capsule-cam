from flask import Blueprint, jsonify
from pathlib import Path
import json

status_bp = Blueprint("status", __name__)
STATUS_PATH = Path(__file__).parent.parent / "status.json"


@status_bp.route("/api/status")
def get_status():
    try:
        return jsonify(json.loads(STATUS_PATH.read_text()))
    except Exception:
        return jsonify({"status": "unknown"})
