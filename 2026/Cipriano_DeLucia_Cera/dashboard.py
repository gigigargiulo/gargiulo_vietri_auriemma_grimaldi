#!/usr/bin/env python3

import json
import os
import time

from flask import Flask, jsonify, render_template

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATS_FILE = os.path.join(BASE_DIR, "stats.json")
DASHBOARD_PORT = 5050

app = Flask(__name__)


def read_stats():
    """Legge stats.json in modo tollerante: se il controller non ha
    ancora scritto nulla, o il file e' in scrittura, ritorna uno stato
    vuoto invece di far fallire la richiesta."""
    empty_state = {
        "timestamp": None,
        "config": {},
        "slices": {},
        "blocked_hosts": [],
        "dos_events": [],
        "switches_connected": [],
        "stale": True,
        "controller_online": False
    }

    if not os.path.exists(STATS_FILE):
        return empty_state

    try:
        with open(STATS_FILE, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return empty_state

    # Se l'ultimo aggiornamento e' piu' vecchio di ~10s, il controller
    # e' probabilmente spento o Mininet non e' in esecuzione.
    age = time.time() - data.get("timestamp", 0)
    data["stale"] = age > 10
    data["controller_online"] = not data["stale"]
    return data


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/stats")
def api_stats():
    return jsonify(read_stats())


if __name__ == "__main__":
    print(f"Dashboard su http://0.0.0.0:{DASHBOARD_PORT}  (leggo {STATS_FILE})")
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False)
