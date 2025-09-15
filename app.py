import os
import json
import re
import sqlite3
import difflib
from datetime import datetime
from flask import Flask, render_template, request, jsonify, g
from cryptography.fernet import Fernet

# ---------- Configuration ----------
DB_FILE = "logs.db"
FOOD_DB_FILE = "food_db.json"
FERNET_KEY_FILE = "fernet.key"

# Recommended daily intake (simple defaults; adjust as needed)
RDI = {
    "calories": 2000,
    "protein": 50,
    "carbs": 275,
    "fat": 70,
    "fiber": 28,
    "vitamin_c": 90
}

app = Flask(__name__)

# ---------- Load food DB ----------
with open(FOOD_DB_FILE, "r") as f:
    FOOD_DB = json.load(f)

FOOD_KEYS = list(FOOD_DB.keys())

# ---------- Setup encryption key ----------
def get_or_create_key():
    if os.path.exists(FERNET_KEY_FILE):
        return open(FERNET_KEY_FILE, "rb").read()
    key = Fernet.generate_key()
    with open(FERNET_KEY_FILE, "wb") as f:
        f.write(key)
    return key

FERNET_KEY = get_or_create_key()
fernet = Fernet(FERNET_KEY)

# ---------- Simple DB helper ----------
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_FILE)
        db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.execute(
        "CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY, timestamp TEXT, encrypted_input BLOB, totals_json TEXT)"
    )
    db.commit()

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

# ---------- Parsing helpers ----------
# rough unit -> grams mapping (fallbacks)
UNIT_TO_G = {
    "g": 1, "gram": 1, "grams": 1, "kg": 1000,
    "cup": 240, "cups": 240, "tbsp": 15, "tablespoon": 15, "tsp": 5,
    "slice": 30, "slices": 30, "serving": 100, "servings": 100, "piece": 80, "pieces": 80, "egg": 50, "eggs": 50
}

number_unit_food_re = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>g|grams|gram|kg|cup|cups|tbsp|tablespoon|tsp|teaspoon|slice|slices|serving|servings|piece|pieces|egg|eggs)?\s*(?:of)?\s*(?P<food>.+)", re.I)
grams_re = re.compile(r"(?P<grams>\d+(?:\.\d+)?)\s*g\b", re.I)

def find_best_food_token(token):
    token = token.strip().lower()
    # direct contains check
    for key in FOOD_KEYS:
        if token == key.lower():
            return key, 1.0
    # close match
    matches = difflib.get_close_matches(token, FOOD_KEYS, n=1, cutoff=0.6)
    if matches:
        return matches[0], 0.8
    # partial match
    for key in FOOD_KEYS:
        if token in key.lower():
            return key, 0.7
    return None, 0.0

def parse_meal_text(text):
    """
    Input: "2 eggs, 1 cup rice, banana"
    Output: list of dicts {raw:..., food_key:..., grams:..., confidence:...}
    """
    items = []
    parts = re.split(r",|\band\b", text)
    for part in parts:
        p = part.strip()
        if not p:
            continue

        # grams explicit?
        gm = grams_re.search(p)
        if gm:
            grams = float(gm.group("grams"))
            # remove grams from token
            token = grams_re.sub("", p).strip()
            key, conf = find_best_food_token(token)
            if key is None:
                # fallback: use token as is
                key = token
            items.append({"raw": p, "food_key": key, "grams": grams, "confidence": conf})
            continue

        m = number_unit_food_re.match(p)
        if m:
            num = float(m.group("number")) if m.group("number") else None
            unit = (m.group("unit") or "").lower()
            food_token = (m.group("food") or "").strip().lower()
            key, conf = find_best_food_token(food_token)
            # determine grams
            grams = None
            if num and unit:
                if unit in UNIT_TO_G:
                    grams = num * UNIT_TO_G[unit]
                else:
                    grams = num * 100
            elif num and not unit:
                # assume number of servings
                if key in FOOD_DB:
                    grams = num * FOOD_DB[key].get("serving_g", 100)
                else:
                    grams = num * 100
            else:
                # no number found
                if key in FOOD_DB:
                    grams = FOOD_DB[key].get("serving_g", 100)
                else:
                    grams = 100
            items.append({"raw": p, "food_key": key, "grams": grams, "confidence": conf})
            continue

        # fallback: token is food name
        token = p.lower()
        key, conf = find_best_food_token(token)
        grams = FOOD_DB[key]["serving_g"] if key in FOOD_DB else 100
        items.append({"raw": p, "food_key": key, "grams": grams, "confidence": conf})

    return items

# ---------- Nutrition calculations ----------
NUTRIENTS = ["calories", "protein", "carbs", "fat", "fiber", "vitamin_c"]

def calc_nutrition_for_item(food_key, grams):
    """
    returns per-nutrient dict for the given grams
    """
    if food_key not in FOOD_DB:
        # unknown food: return zeros
        return {n: 0.0 for n in NUTRIENTS}
    base = FOOD_DB[food_key]
    res = {}
    for n in NUTRIENTS:
        per100 = base.get(n, 0.0)
        res[n] = (grams / 100.0) * per100
    return res

def sum_totals(item_nut_list):
    totals = {n: 0.0 for n in NUTRIENTS}
    for ni in item_nut_list:
        for n in NUTRIENTS:
            totals[n] += ni.get(n, 0.0)
    # round nicely
    for k in totals:
        totals[k] = round(totals[k], 2)
    return totals

def analyze_meal_text(text):
    parsed = parse_meal_text(text)
    parsed_with_nut = []
    item_nut_list = []
    for p in parsed:
        food_key = p["food_key"]
        grams = p["grams"]
        nut = calc_nutrition_for_item(food_key, grams)
        entry = {
            "raw": p["raw"],
            "food_key": food_key,
            "grams": round(grams, 1),
            "confidence": round(p.get("confidence", 0.0),2),
            "nutrition": {k: round(v,2) for k,v in nut.items()}
        }
        parsed_with_nut.append(entry)
        item_nut_list.append(nut)

    totals = sum_totals(item_nut_list)

    # flags & suggestions
    flags = []
    suggestions = []

    for n in NUTRIENTS:
        r = RDI.get(n)
        if r:
            pct = (totals[n] / r) * 100
            if pct < 80:
                flags.append(f"Low {n} ({round(pct,1)}% of RDI)")
                # simple suggestions
                if n == "protein":
                    suggestions.append("Add a protein source: egg, paneer, dal, or chicken.")
                if n == "fiber":
                    suggestions.append("Add vegetables or fruit (spinach, apple, banana).")
                if n == "vitamin_c":
                    suggestions.append("Add vitamin C rich food: orange, spinach, potato or fruits.")
                if n == "calories":
                    suggestions.append("Increase portion sizes or add an energy-dense item (nuts, paneer).")
            elif pct > 120:
                flags.append(f"High {n} ({round(pct,1)}% of RDI)")
                if n == "fat":
                    suggestions.append("Reduce fried items or oil; prefer grilled or steamed.")
                if n == "calories":
                    suggestions.append("Reduce portion or swap to lower-calorie alternatives.")
    # de-duplicate suggestions
    suggestions = list(dict.fromkeys(suggestions))

    return {
        "parsed_items": parsed_with_nut,
        "totals": totals,
        "flags": flags,
        "suggestions": suggestions
    }

# ---------- Logging (encrypted at rest) ----------
def save_log(raw_text, totals_dict):
    db = get_db()
    enc = fernet.encrypt(raw_text.encode("utf-8"))
    db.execute("INSERT INTO logs (timestamp, encrypted_input, totals_json) VALUES (?, ?, ?)",
               (datetime.utcnow().isoformat(), enc, json.dumps(totals_dict)))
    db.commit()

# ---------- Routes ----------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.get_json()
    text = data.get("meal_text", "")
    if not text.strip():
        return jsonify({"error": "Empty input"}), 400
    result = analyze_meal_text(text)
    # save encrypted log
    try:
        save_log(text, result["totals"])
    except Exception as e:
        app.logger.warning("Failed to save log: %s", e)
    return jsonify(result)

@app.route("/logs", methods=["GET"])
def get_logs():
    """
    For demo only: returns last 20 logs (decrypted). Protect this endpoint in real deployments.
    """
    db = get_db()
    rows = db.execute("SELECT id, timestamp, encrypted_input, totals_json FROM logs ORDER BY id DESC LIMIT 20").fetchall()
    out = []
    for r in rows:
        try:
            dec = fernet.decrypt(r["encrypted_input"]).decode("utf-8")
        except Exception:
            dec = "<decryption failed>"
        out.append({"id": r["id"], "timestamp": r["timestamp"], "input": dec, "totals": json.loads(r["totals_json"])})
    return jsonify(out)

# ---------- App startup ----------
if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(debug=True, port=5000)
