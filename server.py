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


# ---------------------------------------------------------------------------
# Position-management "plan" endpoints. Pattern: the client sends raw
# position data (it already has this locally from MT5), the server decides
# WHAT to do with each position, and returns a plan of actions - close this
# ticket, or modify that ticket's SL/TP to X. The client then executes the
# plan locally (only local MT5 can actually place/modify/close trades).
# Fields omitted from a "modify" action mean "leave that field unchanged" -
# the client already has the position's current SL/TP locally to fill in.
# ---------------------------------------------------------------------------

@app.route("/calc/be-plan", methods=["POST"])
def calc_be_plan():
    data = request.get_json(force=True, silent=True) or {}
    license_key = data.get("license_key", "")
    hardware_id = data.get("hardware_id", "")
    row, err = check_license(license_key, hardware_id)
    if err:
        return err

    try:
        trigger_pips = float(data["trigger_pips"])
        lock_pips = float(data["lock_pips"])
        positions = data["positions"]  # [{ticket, is_buy, entry, current_price, pip}]
    except (KeyError, ValueError, TypeError):
        return jsonify({"status": "error", "message": "Missing or invalid inputs."}), 400

    actions, skipped = [], []
    for p in positions:
        try:
            ticket, is_buy = p["ticket"], bool(p["is_buy"])
            entry, current_price, pip = float(p["entry"]), float(p["current_price"]), float(p["pip"])
        except (KeyError, ValueError, TypeError):
            continue
        if pip <= 0:
            skipped.append(ticket)
            continue
        trigger_price = trigger_pips * pip
        lock_price = lock_pips * pip
        profit_dist = (current_price - entry) if is_buy else (entry - current_price)
        if profit_dist <= trigger_price:
            skipped.append(ticket)
            continue
        new_sl = entry + lock_price if is_buy else entry - lock_price
        actions.append({"type": "modify", "ticket": ticket, "sl": new_sl})

    return jsonify({"status": "ok", "actions": actions, "skipped": skipped})


@app.route("/calc/derisk-plan", methods=["POST"])
def calc_derisk_plan():
    data = request.get_json(force=True, silent=True) or {}
    license_key = data.get("license_key", "")
    hardware_id = data.get("hardware_id", "")
    row, err = check_license(license_key, hardware_id)
    if err:
        return err

    try:
        positions = data["positions"]  # [{ticket, profit}]
    except KeyError:
        return jsonify({"status": "error", "message": "Missing positions."}), 400

    losers = [p for p in positions if p.get("profit", 0) < 0]
    winners = sorted([p for p in positions if p.get("profit", 0) > 0], key=lambda p: p["profit"])

    actions = []
    total_loss = sum(p["profit"] for p in losers)
    for p in losers:
        actions.append({"type": "close", "ticket": p["ticket"]})

    needed = abs(total_loss)
    banked = 0.0
    closed_winner_tickets = []
    for p in winners:
        if banked >= needed:
            break
        actions.append({"type": "close", "ticket": p["ticket"]})
        closed_winner_tickets.append(p["ticket"])
        banked += p["profit"]

    return jsonify({
        "status": "ok", "actions": actions,
        "total_loss": total_loss, "banked": banked,
        "closed_losers": [p["ticket"] for p in losers],
        "closed_winners": closed_winner_tickets,
    })


@app.route("/calc/trim-plan", methods=["POST"])
def calc_trim_plan():
    data = request.get_json(force=True, silent=True) or {}
    license_key = data.get("license_key", "")
    hardware_id = data.get("hardware_id", "")
    row, err = check_license(license_key, hardware_id)
    if err:
        return err

    try:
        trim_price = float(data["trim_price"])
        positions = data["positions"]  # [{ticket, is_buy, entry, profit}]
    except (KeyError, ValueError, TypeError):
        return jsonify({"status": "error", "message": "Missing or invalid inputs."}), 400

    actions, closed, be_set = [], [], []
    for p in positions:
        try:
            ticket, is_buy = p["ticket"], bool(p["is_buy"])
            entry, profit = float(p["entry"]), float(p["profit"])
        except (KeyError, ValueError, TypeError):
            continue
        candidate = (entry > trim_price) if is_buy else (entry < trim_price)
        if not candidate:
            continue
        if profit > 0:
            actions.append({"type": "close", "ticket": ticket})
            closed.append(ticket)
        else:
            actions.append({"type": "modify", "ticket": ticket, "tp": entry})
            be_set.append(ticket)

    return jsonify({"status": "ok", "actions": actions, "closed": closed, "be_set": be_set})


@app.route("/calc/cut-plan", methods=["POST"])
def calc_cut_plan():
    data = request.get_json(force=True, silent=True) or {}
    license_key = data.get("license_key", "")
    hardware_id = data.get("hardware_id", "")
    row, err = check_license(license_key, hardware_id)
    if err:
        return err

    try:
        from_price = float(data["from_price"])
        to_price = float(data["to_price"])
        positions = data["positions"]  # [{ticket, entry}]
    except (KeyError, ValueError, TypeError):
        return jsonify({"status": "error", "message": "Missing or invalid inputs."}), 400

    lo, hi = min(from_price, to_price), max(from_price, to_price)
    actions, closed = [], []
    for p in positions:
        try:
            ticket, entry = p["ticket"], float(p["entry"])
        except (KeyError, ValueError, TypeError):
            continue
        if lo <= entry <= hi:
            actions.append({"type": "close", "ticket": ticket})
            closed.append(ticket)

    return jsonify({"status": "ok", "actions": actions, "closed": closed})


@app.route("/calc/collect-plan", methods=["POST"])
def calc_collect_plan():
    data = request.get_json(force=True, silent=True) or {}
    license_key = data.get("license_key", "")
    hardware_id = data.get("hardware_id", "")
    row, err = check_license(license_key, hardware_id)
    if err:
        return err

    try:
        pct = float(data["pct"])
        positions = data["positions"]  # [{ticket, profit}]
    except (KeyError, ValueError, TypeError):
        return jsonify({"status": "error", "message": "Missing or invalid inputs."}), 400

    total = sum(p.get("profit", 0) for p in positions)
    if total <= 0:
        return jsonify({"status": "ok", "actions": [], "banked": 0.0, "total": total})

    target = total * (pct / 100.0)
    ordered = sorted(positions, key=lambda p: p["profit"])

    actions, banked = [], 0.0
    for p in ordered:
        if banked >= target:
            break
        actions.append({"type": "close", "ticket": p["ticket"]})
        banked += p["profit"]

    return jsonify({"status": "ok", "actions": actions, "banked": banked, "target": target})


@app.route("/calc/cutloss-plan", methods=["POST"])
def calc_cutloss_plan():
    """
    Mirror of collect-plan for the loss side: closes the worst-performing
    LOSING positions first until roughly pct% of the total floating loss
    has been realized, leaving the rest (including any winners) untouched.
    """
    data = request.get_json(force=True, silent=True) or {}
    license_key = data.get("license_key", "")
    hardware_id = data.get("hardware_id", "")
    row, err = check_license(license_key, hardware_id)
    if err:
        return err

    try:
        pct = float(data["pct"])
        positions = data["positions"]  # [{ticket, profit}]
    except (KeyError, ValueError, TypeError):
        return jsonify({"status": "error", "message": "Missing or invalid inputs."}), 400

    total = sum(p.get("profit", 0) for p in positions)
    if total >= 0:
        return jsonify({"status": "ok", "actions": [], "cut": 0.0, "total": total})

    target = total * (pct / 100.0)  # negative - a fraction of the total loss
    losers = sorted([p for p in positions if p.get("profit", 0) < 0], key=lambda p: p["profit"])

    actions, cut = [], 0.0
    for p in losers:
        if cut <= target:
            break
        actions.append({"type": "close", "ticket": p["ticket"]})
        cut += p["profit"]

    return jsonify({"status": "ok", "actions": actions, "cut": cut, "target": target})


@app.route("/calc/tp123-plan", methods=["POST"])
def calc_tp123_plan():
    data = request.get_json(force=True, silent=True) or {}
    license_key = data.get("license_key", "")
    hardware_id = data.get("hardware_id", "")
    row, err = check_license(license_key, hardware_id)
    if err:
        return err

    try:
        tp1, tp2, tp3 = float(data["tp1"]), float(data["tp2"]), float(data["tp3"])
        runners = int(data.get("runners", 0))
        positions = data["positions"]  # [{ticket, profit}]
    except (KeyError, ValueError, TypeError):
        return jsonify({"status": "error", "message": "Missing or invalid inputs."}), 400

    ordered = sorted(positions, key=lambda p: p["profit"])
    if runners > 0:
        working = ordered[:-runners] if runners < len(ordered) else []
        left_alone = ordered[-runners:] if runners < len(ordered) else ordered
    else:
        working, left_alone = ordered, []

    n = len(working)
    actions = []
    if n > 0:
        third = max(1, n // 3)
        groups = [working[:third], working[third:2 * third], working[2 * third:]]
        for group, tp in zip(groups, [tp1, tp2, tp3]):
            for p in group:
                actions.append({"type": "modify", "ticket": p["ticket"], "tp": tp})

    return jsonify({
        "status": "ok", "actions": actions,
        "runners": [p["ticket"] for p in left_alone],
    })


@app.route("/admin/issue-key", methods=["POST"])
def issue_key():
    data = request.get_json(force=True, silent=True) or {}
    if data.get("admin_secret") != ADMIN_SECRET:
        return jsonify({"status": "error", "message": "Invalid admin secret."}), 403

    plan = data.get("plan", "monthly")
    if data.get("lifetime") or plan == "lifetime":
        plan = "lifetime"
        expires_at = None  # never expires - see check_license()
    else:
        days = int(data.get("days", 30))
        expires_at = int(time.time()) + days * 86400

    key = "KILLSHOT-" + "-".join(secrets.token_hex(2).upper() for _ in range(3))

    conn = get_db()
    conn.execute(
        "INSERT INTO licenses (license_key, plan, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (key, plan, expires_at, int(time.time())),
    )
    conn.commit()
    conn.close()

    return jsonify({"status": "ok", "license_key": key, "plan": plan, "expires_at": expires_at})


@app.route("/admin/revoke-key", methods=["POST"])
def revoke_key():
    """Deactivates a license key so it can no longer activate or validate."""
    data = request.get_json(force=True, silent=True) or {}
    if data.get("admin_secret") != ADMIN_SECRET:
        return jsonify({"status": "error", "message": "Invalid admin secret."}), 403

    license_key = data.get("license_key", "")
    if not license_key:
        return jsonify({"status": "error", "message": "license_key is required."}), 400

    conn = get_db()
    row = conn.execute("SELECT * FROM licenses WHERE license_key = ?", (license_key,)).fetchone()
    if row is None:
        conn.close()
        return jsonify({"status": "error", "message": "License key not found."}), 404

    conn.execute("UPDATE licenses SET active = 0 WHERE license_key = ?", (license_key,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "message": f"{license_key} has been revoked."})


@app.route("/admin/update-key", methods=["POST"])
def update_key():
    """
    Changes the plan and/or expiry of an EXISTING key, without generating a
    new key string - useful for switching a client between weekly/monthly/
    annual plans without having them re-enter a new license key.
    """
    data = request.get_json(force=True, silent=True) or {}
    if data.get("admin_secret") != ADMIN_SECRET:
        return jsonify({"status": "error", "message": "Invalid admin secret."}), 403

    license_key = data.get("license_key", "")
    if not license_key:
        return jsonify({"status": "error", "message": "license_key is required."}), 400

    conn = get_db()
    row = conn.execute("SELECT * FROM licenses WHERE license_key = ?", (license_key,)).fetchone()
    if row is None:
        conn.close()
        return jsonify({"status": "error", "message": "License key not found."}), 404

    plan = data.get("plan", row["plan"])
    if data.get("lifetime") or plan == "lifetime":
        plan = "lifetime"
        expires_at = None  # never expires - see check_license()
    elif "days" in data:
        try:
            days = int(data["days"])
        except (TypeError, ValueError):
            conn.close()
            return jsonify({"status": "error", "message": "days must be a whole number."}), 400
        expires_at = int(time.time()) + days * 86400
    else:
        expires_at = row["expires_at"]

    conn.execute(
        "UPDATE licenses SET plan = ?, expires_at = ? WHERE license_key = ?",
        (plan, expires_at, license_key),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "license_key": license_key, "plan": plan, "expires_at": expires_at})


@app.route("/admin/unbind-key", methods=["POST"])
def unbind_key():
    """Clears the hardware lock on a key, e.g. so a client can move to a new PC."""
    data = request.get_json(force=True, silent=True) or {}
    if data.get("admin_secret") != ADMIN_SECRET:
        return jsonify({"status": "error", "message": "Invalid admin secret."}), 403

    license_key = data.get("license_key", "")
    if not license_key:
        return jsonify({"status": "error", "message": "license_key is required."}), 400

    conn = get_db()
    row = conn.execute("SELECT * FROM licenses WHERE license_key = ?", (license_key,)).fetchone()
    if row is None:
        conn.close()
        return jsonify({"status": "error", "message": "License key not found."}), 404

    conn.execute("UPDATE licenses SET hardware_id = NULL WHERE license_key = ?", (license_key,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "message": f"{license_key} can now activate on a new machine."})


@app.route("/")
def health():
    return jsonify({"status": "Killshot FX license server is running"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
