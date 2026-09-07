"""
Killshot FX License & Logic Server

Run locally for testing:
    python server.py

Deploy on Railway: see README.md

Endpoints:
    POST /activate              - first-time license activation (binds to a hardware ID)
    POST /validate               - periodic re-check that a license is still good
    POST /calc/martingale-sizing - example "protected logic" endpoint: does the martingale
                                    lot-sizing math server-side so it never ships in the client bot
    POST /admin/issue-key        - issues a new license key (requires ADMIN_SECRET)
    GET  /                       - health check
"""
import hashlib
import hmac
import os
import secrets
import sqlite3
import time

from flask import Flask, jsonify, request

app = Flask(__name__)

DB_PATH = os.environ.get("DB_PATH", "licenses.db")
SECRET = os.environ.get("SERVER_SECRET", "change-this-in-production")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "change-this-too")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            license_key TEXT PRIMARY KEY,
            hardware_id TEXT,
            active INTEGER DEFAULT 1,
            plan TEXT DEFAULT 'monthly',
            expires_at INTEGER,
            created_at INTEGER
        )
    """)
    conn.commit()
    conn.close()


init_db()


def sign(payload: str) -> str:
    return hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]


def check_license(license_key: str, hardware_id: str):
    """Returns (row, error_response). Exactly one of these is None."""
    conn = get_db()
    row = conn.execute("SELECT * FROM licenses WHERE license_key = ?", (license_key,)).fetchone()

    if row is None:
        conn.close()
        return None, (jsonify({"status": "error", "message": "License key not found."}), 404)

    if not row["active"]:
        conn.close()
        return None, (jsonify({"status": "error", "message": "License has been deactivated."}), 403)

    if row["expires_at"] and row["expires_at"] < time.time():
        conn.close()
        return None, (jsonify({"status": "error", "message": "License has expired."}), 403)

    if row["hardware_id"] is None:
        conn.execute("UPDATE licenses SET hardware_id = ? WHERE license_key = ?", (hardware_id, license_key))
        conn.commit()
    elif row["hardware_id"] != hardware_id:
        conn.close()
        return None, (jsonify({"status": "error", "message": "License is already active on a different machine."}), 403)

    conn.close()
    return row, None


def make_token(license_key: str, hardware_id: str) -> dict:
    expires_at = int(time.time()) + 86400  # token itself is valid 24h before needing /validate again
    payload = f"{license_key}:{hardware_id}:{expires_at}"
    return {"token_payload": payload, "token": sign(payload)}


@app.route("/activate", methods=["POST"])
@app.route("/validate", methods=["POST"])
def activate_or_validate():
    data = request.get_json(force=True, silent=True) or {}
    license_key = data.get("license_key", "")
    hardware_id = data.get("hardware_id", "")
    if not license_key or not hardware_id:
        return jsonify({"status": "error", "message": "license_key and hardware_id are required."}), 400

    row, err = check_license(license_key, hardware_id)
    if err:
        return err

    result = {"status": "ok", "plan": row["plan"], "expires_at": row["expires_at"]}
    result.update(make_token(license_key, hardware_id))
    return jsonify(result)


@app.route("/calc/martingale-sizing", methods=["POST"])
def calc_martingale_sizing():
    """
    The actual sizing formula lives ONLY here now - a client's bot sends its
    inputs (already gathered locally from MT5, since broker-specific prices
    can't be known server-side) and gets back the lot sizes, never the formula
    itself.
    """
    data = request.get_json(force=True, silent=True) or {}
    license_key = data.get("license_key", "")
    hardware_id = data.get("hardware_id", "")
    row, err = check_license(license_key, hardware_id)
    if err:
        return err

    try:
        balance = float(data["balance"])
        risk_pct = float(data["risk_pct"])
        ratio = float(data["ratio"])
        layer_count = int(data["layer_count"])
        per_lot_losses = [float(x) for x in data["per_lot_losses"]]
    except (KeyError, ValueError, TypeError):
        return jsonify({"status": "error", "message": "Missing or invalid sizing inputs."}), 400

    if len(per_lot_losses) != layer_count:
        return jsonify({"status": "error", "message": "per_lot_losses length must match layer_count."}), 400

    total_loss_per_base_lot = sum(per_lot_losses[i] * (ratio ** i) for i in range(layer_count))
    if total_loss_per_base_lot >= 0:
        return jsonify({"status": "error", "message": "Could not size the ladder - check SL distance and range."}), 400

    target_loss = -abs(balance * (risk_pct / 100.0))
    base_lot = target_loss / total_loss_per_base_lot
    lots = [round(base_lot * (ratio ** i), 2) for i in range(layer_count)]

    return jsonify({"status": "ok", "lots": lots, "target_loss": target_loss})


@app.route("/admin/issue-key", methods=["POST"])
def issue_key():
    data = request.get_json(force=True, silent=True) or {}
    if data.get("admin_secret") != ADMIN_SECRET:
        return jsonify({"status": "error", "message": "Invalid admin secret."}), 403

    plan = data.get("plan", "monthly")
    days = int(data.get("days", 30))
    key = "KILLSHOT-" + "-".join(secrets.token_hex(2).upper() for _ in range(3))
    expires_at = int(time.time()) + days * 86400

    conn = get_db()
    conn.execute(
        "INSERT INTO licenses (license_key, plan, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (key, plan, expires_at, int(time.time())),
    )
    conn.commit()
    conn.close()

    return jsonify({"status": "ok", "license_key": key, "plan": plan, "expires_at": expires_at})


@app.route("/")
def health():
    return jsonify({"status": "Killshot FX license server is running"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
