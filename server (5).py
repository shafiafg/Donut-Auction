import json
import sqlite3
import time
import threading
import requests
import os
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app) # This allows your website to talk to the server

# --- CONFIG ---
DB_NAME = "market.db"
# This now pulls from your Render/GitHub environment variables!
API_KEY = os.getenv("API_KEY", "fallback_if_missing")
API_URL = "https://api.donutsmp.net/market/all"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS prices 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  item_id TEXT, item_name TEXT, price REAL, recorded_at INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS alerts 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  message TEXT, alert_type TEXT, created_at INTEGER)''')
    conn.commit()
    conn.close()

# --- SCRAPER (Runs in background) ---
def fetch_market_data():
    while True:
        try:
            # Uses the environment variable from Render
            headers = {"Authorization": API_KEY} if API_KEY != "fallback_if_missing" else {}
            res = requests.get(API_URL, headers=headers, timeout=10)
            if res.status_code == 200:
                data = res.json()
                conn = sqlite3.connect(DB_NAME)
                c = conn.cursor()
                now = int(time.time())
                for item in data.get('listings', []):
                    c.execute("INSERT INTO prices (item_id, item_name, price, recorded_at) VALUES (?, ?, ?, ?)",
                              (item['id'], item['name'], item['price'], now))
                conn.commit()
                conn.close()
            time.sleep(120)
        except Exception as e:
            print(f"Scraper Error: {e}")
            time.sleep(30)

# --- ROUTES ---
@app.route('/api/status')
def get_status():
    conn = sqlite3.connect(DB_NAME)
    count = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    conn.close()
    return jsonify({"listings": count})

@app.route('/api/investments')
def get_investments():
    return jsonify([])

@app.route('/api/alerts')
def get_alerts():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM alerts ORDER BY created_at DESC LIMIT 20").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# Start the scraper thread once when the app starts
init_db()
threading.Thread(target=fetch_market_data, daemon=True).start()

if __name__ == "__main__":
    # Render sets a PORT env var automatically
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
