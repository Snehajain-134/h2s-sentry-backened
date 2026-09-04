"""
H2S SENTRY — backend API (Python / Flask)
--------------------------------------------------------------
This replaces the earlier Node/Express server.js, which was the
wrong stack: h2s-sentry.html's own error messages already say
"make sure it is running (python server.py)" and expect port 5000
with routes like /api/wristband/:id and /api/analyze — neither of
which existed in the Node version.

Data lives in db.json (acts as the database) so it survives restarts.
Swap read_db()/write_db() for a real database later without touching
the routes.
--------------------------------------------------------------
Setup:
    pip install flask pyjwt pillow
Run:
    python server.py
Serves on http://localhost:5000 — matches API_BASE in h2s-sentry.html.
"""

import base64
import io
import json
import math
import os
from datetime import datetime, timedelta
from functools import wraps

import jwt
from flask import Flask, request, jsonify
from flask import send_from_directory
from PIL import Image

DB_PATH = os.path.join(os.path.dirname(__file__), "db.json")
JWT_SECRET = os.environ.get("JWT_SECRET", "h2s-sentry-dev-secret-change-me")

app = Flask(__name__)

# ---------------- manual CORS (no extra dependency needed) ----------------
@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

@app.route("/api/<path:_any>", methods=["OPTIONS"])
def cors_preflight(_any):
    return ("", 204)

# ---------------- tiny JSON-file "database" ----------------
def read_db():
    with open(DB_PATH, "r") as f:
        return json.load(f)

def write_db(db):
    with open(DB_PATH, "w") as f:
        json.dump(db, f, indent=2)

# ---------------- auth helpers ----------------
def sign_token(user):
    payload = {
        "id": user["id"],
        "role": user["role"],
        "workerId": user.get("workerId"),
        "exp": datetime.utcnow() + timedelta(hours=12),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else None
        if not token:
            return jsonify({"error": "Missing token"}), 401
        try:
            request.user = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        except jwt.PyJWTError:
            return jsonify({"error": "Invalid or expired token"}), 401
        return f(*args, **kwargs)
    return wrapper

def require_role(*roles):
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if request.user["role"] not in roles:
                return jsonify({"error": "Not authorized for this action"}), 403
            return f(*args, **kwargs)
        return wrapper
    return deco

# ---------------- colorimetric model (shared with the calibration dataset) ----------------
L0, A0, B0 = 65.3, -0.1, 3.9
dLm, dam, dbm = -38.0, 2.0, 9.0
DELTA_E_MAX = math.sqrt(dLm**2 + dam**2 + dbm**2)  # ~39.1
PPM_SCALE = 40.0

def classify(ppm):
    if ppm < 10:
        return "Low"
    elif ppm < 20:
        return "Medium"
    else:
        return "High"

def rgb_to_lab(r, g, b):
    def lin(c):
        c = c / 255.0
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92
    rl, gl, bl = lin(r), lin(g), lin(b)
    x = rl*0.4124 + gl*0.3576 + bl*0.1805
    y = rl*0.2126 + gl*0.7152 + bl*0.0722
    z = rl*0.0193 + gl*0.1192 + bl*0.9505
    x, y, z = x/0.95047, y/1.0, z/1.08883
    def f(t):
        return t**(1/3) if t > 0.008856 else (7.787*t + 16/116)
    fx, fy, fz = f(x), f(y), f(z)
    L = 116*fy - 16
    a = 500*(fx-fy)
    b_ = 200*(fy-fz)
    return L, a, b_

def delta_e(L, a, b_):
    return math.sqrt((L-L0)**2 + (a-A0)**2 + (b_-B0)**2)

def ppm_from_delta_e(dE):
    clamped = min(dE, DELTA_E_MAX*0.999)
    ppm = -PPM_SCALE * math.log(1 - clamped/DELTA_E_MAX)
    return max(0.0, round(ppm, 1))

def analyze_patch_image(image_base64):
    """Decode a data-URL PNG, sample the center region, return (ppm, color_status, dE).

    Validates that the sampled region actually looks like a patch (a small,
    fairly uniform-colored swatch) before trusting it. A face, hand, or busy
    background has much higher pixel-to-pixel color variation than a patch —
    that's the signal used to reject non-patch captures, rather than silently
    returning a meaningless reading.
    """
    header_split = image_base64.split(",", 1)
    raw = header_split[1] if len(header_split) > 1 else header_split[0]
    img_bytes = base64.b64decode(raw)
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = img.size
    box_size = max(10, int(min(w, h) * 0.10))
    cx, cy = w // 2, h // 2
    crop = img.crop((cx - box_size//2, cy - box_size//2, cx + box_size//2, cy + box_size//2))
    pixels = list(crop.getdata())
    n = len(pixels)

    r_vals = [p[0] for p in pixels]
    g_vals = [p[1] for p in pixels]
    b_vals = [p[2] for p in pixels]
    r = sum(r_vals) / n
    g = sum(g_vals) / n
    b = sum(b_vals) / n

    # Uniformity check: a real patch swatch is close to one flat color.
    # Skin, hair, clothing, and general backgrounds have visibly more
    # pixel-to-pixel variation within the same sample box.
    def std(vals, mean):
        return (sum((v - mean) ** 2 for v in vals) / n) ** 0.5
    color_std = (std(r_vals, r) + std(g_vals, g) + std(b_vals, b)) / 3

    UNIFORMITY_THRESHOLD = 32.0
    if color_std > UNIFORMITY_THRESHOLD:
        raise ValueError(
            "Could not detect a clear, evenly-lit patch in this photo — the frame looks "
            "too varied (make sure it's just the sensor patch filling the guide box, not "
            "a face, hand, or background). Try again."
        )

    L, a, b_ = rgb_to_lab(r, g, b)
    print(f"DEBUG r={r:.1f} g={g:.1f} b={b:.1f} L={L:.1f} a={a:.1f} b*={b_:.1f}")
    dE = delta_e(L, a, b_)
    dE_capped = min(dE, DELTA_E_MAX * 0.97)  # avoid the ppm formula blowing up near saturation
    ppm = ppm_from_delta_e(dE_capped)
    return ppm, classify(ppm), round(dE, 1)

# ---------------- worker/session helpers ----------------
def worker_payload(w_id, w, latest_override=None):
    latest = latest_override or w.get("latest_reading")
    color_status = latest["color_status"] if latest else "Low"
    fit_status = "Fit for duty" if color_status == "Low" else "Not fit — medical check-up recommended"
    return {
        "w_id": w_id,
        "w_name": w["w_name"],
        "gender": w["gender"],
        "age": w["age"],
        "doj": w["doj"],
        "zone": w["zone"],
        "shift": w["shift"],
        "wristband_id": w_id,
        "blood_group": w["blood_group"],
        "fit_status": fit_status,
        "latest_reading": latest,
    }

def has_open_session(db, w_id):
    sessions = db.get("sessions", {}).get(w_id, [])
    return bool(sessions) and sessions[0].get("exit_timestamp") is None

# ================= ROUTES =================

@app.route("/api/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    uid, password = body.get("id"), body.get("password")
    if not uid or not password:
        return jsonify({"error": "ID and password required"}), 400

    db = read_db()
    user = next((u for u in db["users"] if u["id"].upper() == str(uid).upper()), None)
    if not user or user["password"] != password:
        return jsonify({"error": "ID or password not recognized"}), 401

    if user["role"] == "worker":
        name = db["workers"][user["workerId"]]["w_name"]
    else:
        name = user["name"]

    token = sign_token(user)
    return jsonify({"token": token, "role": user["role"], "name": name,
                     "workerId": user.get("workerId"), "id": user["id"]})

@app.route("/api/me", methods=["GET"])
@require_auth
def me():
    return jsonify({"user": request.user})

@app.route("/api/worker/<w_id>", methods=["GET"])
@require_auth
def get_worker(w_id):
    if request.user["role"] == "worker" and request.user["workerId"] != w_id:
        return jsonify({"error": "Workers can only view their own profile"}), 403
    db = read_db()
    w = db["workers"].get(w_id)
    if not w:
        return jsonify({"error": "Worker not found"}), 404
    return jsonify(worker_payload(w_id, w))

@app.route("/api/wristband/<w_id>", methods=["GET"])
@require_auth
@require_role("supervisor", "admin")
def get_by_wristband(w_id):
    db = read_db()
    w = db["workers"].get(w_id)
    if not w:
        return jsonify({"error": f"No worker registered to wristband {w_id}"}), 404
    return jsonify(worker_payload(w_id, w))

@app.route("/api/workers", methods=["GET"])
@require_auth
@require_role("supervisor", "admin")
def list_workers():
    db = read_db()
    zones = None
    if request.user["role"] == "supervisor":
        user = next((u for u in db["users"] if u["id"] == request.user["id"]), None)
        zones = set(user.get("zones", [])) if user else None
    out = []
    for w_id, w in db["workers"].items():
        if zones is not None and w["zone"] not in zones:
            continue
        out.append(worker_payload(w_id, w))
    return jsonify({"workers": out})

@app.route("/api/history/<w_id>", methods=["GET"])
@require_auth
def get_history(w_id):
    if request.user["role"] == "worker" and request.user["workerId"] != w_id:
        return jsonify({"error": "Workers can only view their own history"}), 403
    db = read_db()
    return jsonify({"id": w_id, "sessions": db.get("sessions", {}).get(w_id, [])})

@app.route("/api/zones", methods=["GET"])
@require_auth
@require_role("supervisor", "admin")
def get_zones():
    db = read_db()
    zone_rank = {"Low": 0, "Medium": 1, "High": 2}
    agg = {}
    for w in db["workers"].values():
        z = w["zone"]
        status = (w.get("latest_reading") or {}).get("color_status", "Low")
        if z not in agg:
            agg[z] = {"zone": z, "worker_count": 0, "worst_status": "Low"}
        agg[z]["worker_count"] += 1
        if zone_rank[status] > zone_rank[agg[z]["worst_status"]]:
            agg[z]["worst_status"] = status
    return jsonify({"zones": list(agg.values())})

@app.route("/api/analyze", methods=["POST"])
@require_auth
@require_role("supervisor", "admin")
def analyze():
    body = request.get_json(silent=True) or {}
    w_id = body.get("wristband_id")
    image_b64 = body.get("image_base64")
    if not w_id or not image_b64:
        return jsonify({"error": "wristband_id and image_base64 are required"}), 400

    db = read_db()
    w = db["workers"].get(w_id)
    if not w:
        return jsonify({"error": f"No worker registered to wristband {w_id}"}), 404

    try:
        ppm, color_status, dE = analyze_patch_image(image_b64)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    now = datetime.now()
    now_iso = now.strftime("%Y-%m-%d %H:%M")

    was_open = has_open_session(db, w_id)
    scan_type = "Exit" if was_open else "Entry"

    latest_reading = {"intake_level_ppm": ppm, "color_status": color_status,
                       "delta_e": dE, "timestamp": now_iso}
    w["latest_reading"] = latest_reading

    sessions = db.setdefault("sessions", {}).setdefault(w_id, [])
    if scan_type == "Entry":
        sessions.insert(0, {
            "entry_timestamp": now_iso, "exit_timestamp": None,
            "duration_minutes": None, "max_ppm_in_shift": ppm,
            "overall_status": color_status, "alert_triggered": "Yes" if color_status == "High" else "No",
            "zone": w["zone"],
        })
    else:
        session = sessions[0]
        entry_dt = datetime.strptime(session["entry_timestamp"], "%Y-%m-%d %H:%M")
        session["exit_timestamp"] = now_iso
        session["duration_minutes"] = round((now - entry_dt).total_seconds() / 60, 1)
        session["max_ppm_in_shift"] = max(session["max_ppm_in_shift"], ppm)
        rank = {"Low": 0, "Medium": 1, "High": 2}
        if rank[color_status] > rank[session["overall_status"]]:
            session["overall_status"] = color_status
        if color_status == "High":
            session["alert_triggered"] = "Yes"

    write_db(db)

    fit_status = "Fit for duty" if color_status == "Low" else "Not fit — medical check-up recommended"
    return jsonify({
        "w_id": w_id, "w_name": w["w_name"], "zone": w["zone"],
        "scan_type": scan_type, "color_status": color_status,
        "intake_level_ppm": ppm, "delta_e": dE, "fit_status": fit_status,
    })
@app.route("/")
def serve_frontend():
     return send_from_directory(os.path.dirname(__file__), "h2s-sentry.html")
if __name__ == "__main__":
   port = int(os.environ.get("PORT", 5000)) 
   app.run(debug=False, host="0.0.0.0", port=port)
