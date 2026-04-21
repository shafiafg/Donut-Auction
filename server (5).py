"""
Donut SMP Market Terminal — Server
===================================
Works locally AND on Render.com (free tier).
- Local:  python server.py  → http://localhost:7771
- Render: automatically uses $PORT env var → your-app.onrender.com

SETUP:
1. Set API_KEY in Render dashboard → Environment → API_KEY
2. Optionally set CLAUDE_KEY for AI Brain
3. Push to GitHub, deploy on render.com as a Web Service
"""

import os, sqlite3, json, time, threading, logging, ssl
import urllib.request, urllib.error
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder="static")
CORS(app)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("terminal")

# ══════════════════════════════════════
#  CONFIG  (set via Render env vars, or edit here for local)
# ══════════════════════════════════════
API_KEY_1      = os.environ.get("API_KEY",       "YOUR_API_KEY_HERE")
API_KEY_2      = os.environ.get("API_KEY_2",     "")
CLAUDE_KEY     = os.environ.get("CLAUDE_KEY",    "")
DB_PATH        = os.environ.get("DB_PATH",       "market.db")
MAX_PAGES      = int(os.environ.get("MAX_PAGES", "250"))
FETCH_INTERVAL = int(os.environ.get("FETCH_INTERVAL", "90"))
FLIP_PROFIT    = 0.15
WHALE_MIN      = 6
PAGE_DELAY     = 0.25

HIGH_VALUE_ITEMS = {
    "minecraft:elytra",
    "minecraft:netherite_ingot","minecraft:netherite_block","minecraft:netherite_scrap",
    "minecraft:ancient_debris",
    "minecraft:netherite_sword","minecraft:netherite_pickaxe","minecraft:netherite_axe",
    "minecraft:netherite_shovel","minecraft:netherite_hoe",
    "minecraft:netherite_chestplate","minecraft:netherite_helmet",
    "minecraft:netherite_leggings","minecraft:netherite_boots",
    "minecraft:netherite_upgrade_smithing_template",
    "minecraft:end_crystal","minecraft:dragon_egg","minecraft:dragon_head",
    "minecraft:wither_skeleton_skull","minecraft:piglin_head",
    "minecraft:nether_star","minecraft:beacon",
    "minecraft:totem_of_undying","minecraft:shulker_shell",
    "minecraft:diamond","minecraft:diamond_block",
    "minecraft:gold_ingot","minecraft:gold_block",
    "minecraft:enchanted_book","minecraft:enchanted_golden_apple",
    "minecraft:trident","minecraft:mace",
    "minecraft:ender_pearl","minecraft:ender_eye",
    "minecraft:conduit","minecraft:heart_of_the_sea","minecraft:nautilus_shell",
    "minecraft:sponge",
    "minecraft:echo_shard","minecraft:music_disc_pigstep","minecraft:music_disc_otherside",
    "minecraft:crying_obsidian","minecraft:respawn_anchor",
    "minecraft:iron_ingot","minecraft:iron_block",
    "minecraft:emerald","minecraft:emerald_block",
    "minecraft:shulker_box","minecraft:white_shulker_box","minecraft:black_shulker_box",
}

RECIPES = {
    "minecraft:gold_block":      [("minecraft:gold_ingot",9)],
    "minecraft:iron_block":      [("minecraft:iron_ingot",9)],
    "minecraft:diamond_block":   [("minecraft:diamond",9)],
    "minecraft:netherite_block": [("minecraft:netherite_ingot",9)],
    "minecraft:emerald_block":   [("minecraft:emerald",9)],
    "minecraft:coal_block":      [("minecraft:coal",9)],
    "minecraft:copper_block":    [("minecraft:copper_ingot",9)],
    "minecraft:lapis_block":     [("minecraft:lapis_lazuli",9)],
    "minecraft:redstone_block":  [("minecraft:redstone",9)],
    "minecraft:quartz_block":    [("minecraft:quartz",4)],
}

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode    = ssl.CERT_NONE

_key_idx  = 0
_key_lock = threading.Lock()
def next_key():
    global _key_idx
    keys = [k for k in [API_KEY_1, API_KEY_2] if k and "YOUR_" not in k and k.strip()]
    if not keys: return API_KEY_1
    with _key_lock:
        k = keys[_key_idx % len(keys)]; _key_idx += 1
    return k

# ══════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS listings (
            uid TEXT PRIMARY KEY,
            item_id TEXT NOT NULL, item_name TEXT NOT NULL,
            seller TEXT NOT NULL, price REAL NOT NULL,
            count INTEGER DEFAULT 1, time_left INTEGER DEFAULT 0,
            has_enchants INTEGER DEFAULT 0, is_hv INTEGER DEFAULT 0,
            fetched_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL, item_name TEXT NOT NULL,
            price REAL NOT NULL, count INTEGER DEFAULT 1,
            seller TEXT, recorded_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type TEXT NOT NULL, item_name TEXT,
            message TEXT NOT NULL, data TEXT DEFAULT '{}',
            created_at REAL NOT NULL, dismissed INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS daily_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL, date TEXT NOT NULL,
            avg_price REAL, min_price REAL, max_price REAL, volume INTEGER,
            UNIQUE(item_id, date)
        );
        CREATE INDEX IF NOT EXISTS idx_lst_item  ON listings(item_id);
        CREATE INDEX IF NOT EXISTS idx_lst_hv    ON listings(is_hv);
        CREATE INDEX IF NOT EXISTS idx_hist_item ON price_history(item_id);
        CREATE INDEX IF NOT EXISTS idx_hist_time ON price_history(recorded_at);
    """)
    conn.commit(); conn.close()
    log.info("DB ready → %s", DB_PATH)

def get_conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

# ══════════════════════════════════════
#  API FETCH
# ══════════════════════════════════════
def api_get(path, body=None):
    url  = "https://api.donutsmp.net/v1" + path
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(url, data=data, headers={
        "accept":         "application/json",
        "Authorization":  next_key(),
        "Content-Type":   "application/json",
        "User-Agent":     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer":        "https://donutsmp.net/",
        "Origin":         "https://donutsmp.net",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=12, context=SSL_CTX) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        log.warning("HTTP %d %s: %s", e.code, path, e.read().decode(errors="replace")[:80])
    except Exception as e:
        log.warning("Request %s: %s", path, e)
    return None

def fetch_all():
    all_items, page, empty_streak = [], 1, 0
    while page <= MAX_PAGES:
        r = api_get(f"/auction/list/{page}", body={"sort": "lowest_price"})
        items = r.get("result", []) if r and r.get("status") == 200 else []
        if not items:
            empty_streak += 1
            if empty_streak >= 2: break
        else:
            empty_streak = 0
            all_items.extend(items)
            if page % 20 == 0:
                log.info("  p%d total=%d", page, len(all_items))
        page += 1
        time.sleep(PAGE_DELAY)
    return all_items

# ══════════════════════════════════════
#  NORMALISE & SAVE
# ══════════════════════════════════════
def pretty(iid): return iid.replace("minecraft:","").replace("_"," ").title()

def normalise(raw):
    s    = raw.get("seller", {}); item = raw.get("item", {})
    iid  = (item.get("id") or "unknown").strip()
    enc  = item.get("enchants", {}) or {}
    henc = 1 if (enc.get("enchantments") or {}).get("levels") else 0
    disp = (item.get("display_name") or "").strip()
    name = disp if disp else pretty(iid)
    sn   = s.get("name", "Unknown")
    pr   = float(raw.get("price") or 0)
    tl   = int(raw.get("time_left") or 0)
    ct   = int(item.get("count") or 1)
    return dict(uid=f"{sn}:{iid}:{pr}:{tl}", item_id=iid, item_name=name,
                seller=sn, price=pr, count=ct, time_left=tl,
                has_enchants=henc, is_hv=1 if iid in HIGH_VALUE_ITEMS else 0)

def save(raw_list):
    now  = time.time()
    conn = get_conn(); c = conn.cursor()
    c.execute("DELETE FROM listings")
    saved = 0
    for raw in raw_list:
        n = normalise(raw)
        if n["price"] <= 0: continue
        c.execute("""INSERT OR REPLACE INTO listings
            (uid,item_id,item_name,seller,price,count,time_left,has_enchants,is_hv,fetched_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (n["uid"],n["item_id"],n["item_name"],n["seller"],
             n["price"],n["count"],n["time_left"],n["has_enchants"],n["is_hv"],now))
        c.execute("""INSERT INTO price_history(item_id,item_name,price,count,seller,recorded_at)
            SELECT ?,?,?,?,?,? WHERE NOT EXISTS(SELECT 1 FROM price_history
            WHERE item_id=? AND price=? AND seller=? AND recorded_at>?)""",
            (n["item_id"],n["item_name"],n["price"],n["count"],n["seller"],now,
             n["item_id"],n["price"],n["seller"],now-300))
        saved += 1
    today = time.strftime("%Y-%m-%d", time.gmtime())
    for iid in HIGH_VALUE_ITEMS:
        row = conn.execute("""SELECT AVG(price),MIN(price),MAX(price),COUNT(*)
            FROM price_history WHERE item_id=? AND DATE(recorded_at,'unixepoch')=?""",
            (iid,today)).fetchone()
        if row and row[0]:
            c.execute("""INSERT OR REPLACE INTO daily_stats(item_id,date,avg_price,min_price,max_price,volume)
                VALUES(?,?,?,?,?,?)""", (iid,today,row[0],row[1],row[2],row[3]))
    conn.commit(); conn.close()
    log.info("Saved %d listings", saved)

# ══════════════════════════════════════
#  ANALYSIS HELPERS
# ══════════════════════════════════════
def avg_24h(conn, iid):
    r = conn.execute("SELECT AVG(price) FROM price_history WHERE item_id=? AND recorded_at>?",
                     (iid, time.time()-86400)).fetchone()
    return r[0] if r and r[0] else None

def median_7d(conn, iid):
    rows = conn.execute("SELECT price FROM price_history WHERE item_id=? AND recorded_at>? ORDER BY price",
                        (iid, time.time()-7*86400)).fetchall()
    p = [r[0] for r in rows]
    if not p: return None
    m = len(p)//2
    return p[m] if len(p)%2 else (p[m-1]+p[m])/2

def pct_change_24h(conn, iid):
    now = time.time()
    old = conn.execute("SELECT AVG(price) FROM price_history WHERE item_id=? AND recorded_at BETWEEN ? AND ?",
                       (iid, now-86400, now-82800)).fetchone()[0]
    new_ = conn.execute("SELECT AVG(price) FROM price_history WHERE item_id=? AND recorded_at>?",
                        (iid, now-3600)).fetchone()[0]
    if not old or not new_: return None
    return (new_-old)/old*100

def get_trend(conn, iid):
    chg = pct_change_24h(conn, iid)
    if chg is None: return "Stable"
    if chg > 10:  return "Mooning"
    if chg < -10: return "Crashing"
    return "Stable"

def run_engines(conn):
    alerts = []; now = time.time()
    for out_id, inputs in RECIPES.items():
        or_ = conn.execute("SELECT MIN(price) FROM listings WHERE item_id=?", (out_id,)).fetchone()
        if not or_ or not or_[0]: continue
        op = or_[0]; cost=0; ings=[]; valid=True
        for ig, qty in inputs:
            ir = conn.execute("SELECT MIN(price) FROM listings WHERE item_id=?", (ig,)).fetchone()
            if not ir or not ir[0]: valid=False; break
            ip=ir[0]; cost+=ip*qty; ings.append({"item":pretty(ig),"qty":qty,"unit":ip,"total":ip*qty})
        if not valid or cost<=0: continue
        profit=op-cost; pct=profit/cost*100
        if pct > FLIP_PROFIT*100:
            alerts.append(("craft_flip",pretty(out_id),
                f"🔨 CRAFT FLIP: {pretty(out_id)} — mats ₿{cost:,.0f} → sell ₿{op:,.0f} (+{pct:.1f}%)",
                json.dumps({"out":pretty(out_id),"cost":cost,"sell":op,"profit":profit,"pct":pct,"ings":ings}),now))
    for row in conn.execute("""SELECT seller,item_id,item_name,COUNT(*) cnt,AVG(price) avg_p
        FROM listings GROUP BY seller,item_id HAVING cnt>=?""",(WHALE_MIN,)).fetchall():
        ex = conn.execute("SELECT id FROM alerts WHERE item_name=? AND alert_type='whale' AND created_at>? AND dismissed=0",
                          (row["item_name"],now-600)).fetchone()
        if not ex:
            alerts.append(("whale",row["item_name"],
                f"🐋 WHALE: {row['seller']} has {row['cnt']}× {row['item_name']} @ avg ₿{row['avg_p']:,.0f}",
                json.dumps({"seller":row["seller"],"cnt":row["cnt"],"avg":row["avg_p"]}),now))
    if alerts:
        c = conn.cursor()
        for a in alerts:
            c.execute("INSERT INTO alerts(alert_type,item_name,message,data,created_at) VALUES(?,?,?,?,?)",a)
        conn.commit()
        log.info("Generated %d alerts", len(alerts))

def fetch_loop():
    log.info("Fetch loop starting: every %ds, up to %d pages", FETCH_INTERVAL, MAX_PAGES)
    while True:
        try:
            log.info("── Fetching ──")
            raw = fetch_all()
            if raw:
                save(raw)
                conn = get_conn(); run_engines(conn); conn.close()
                log.info("Fetch complete: %d listings", len(raw))
            else:
                log.warning("No listings — check API_KEY environment variable")
        except Exception as e:
            log.error("Fetch loop error: %s", e, exc_info=True)
        time.sleep(FETCH_INTERVAL)

# ══════════════════════════════════════
#  AI BRAIN
# ══════════════════════════════════════
def build_brain_context():
    conn = get_conn(); hv_data = []
    for iid in HIGH_VALUE_ITEMS:
        cnt = conn.execute("SELECT COUNT(*) FROM price_history WHERE item_id=?",(iid,)).fetchone()[0]
        cur = conn.execute("SELECT MIN(price) FROM listings WHERE item_id=?",(iid,)).fetchone()[0]
        if not cnt and not cur: continue
        hv_data.append({
            "item": pretty(iid), "id": iid,
            "median_7d":   round(median_7d(conn,iid))  if median_7d(conn,iid)  else None,
            "avg_24h":     round(avg_24h(conn,iid))    if avg_24h(conn,iid)    else None,
            "current_min": round(cur)                  if cur                  else None,
            "pct_24h":     round(pct_change_24h(conn,iid),2) if pct_change_24h(conn,iid) else None,
            "active_listings": conn.execute("SELECT COUNT(*) FROM listings WHERE item_id=?",(iid,)).fetchone()[0],
        })
    hv_data.sort(key=lambda x: x.get("median_7d") or x.get("current_min") or 0, reverse=True)
    alerts_raw = [dict(r) for r in conn.execute(
        "SELECT alert_type,item_name,message FROM alerts WHERE dismissed=0 ORDER BY created_at DESC LIMIT 15").fetchall()]
    conn.close()
    return {"high_value_items": hv_data[:30], "active_alerts": alerts_raw,
            "data_as_of": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}

def call_claude(question, ctx):
    if not CLAUDE_KEY or "YOUR_" in CLAUDE_KEY:
        return "⚠ AI Brain not configured. Set CLAUDE_KEY in Render environment variables."
    system = """You are an expert Donut SMP market analyst with access to real-time auction data.
Give direct, specific, actionable advice. Use ₿ for currency. Format numbers with commas.
Always prioritize high-value items (Elytra, Netherite, Dragon Egg, Beacon etc.)."""
    prompt = f"""Market snapshot: {ctx['data_as_of']}

HIGH VALUE ITEMS (by median price):
{json.dumps(ctx['high_value_items'], indent=2)}

ACTIVE ALERTS:
{json.dumps(ctx['active_alerts'], indent=2)}

User: {question}

Give specific buy/sell/hold advice based on this data."""
    body = json.dumps({"model":"claude-sonnet-4-20250514","max_tokens":1024,
                        "system":system,"messages":[{"role":"user","content":prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "Content-Type":"application/json","x-api-key":CLAUDE_KEY,"anthropic-version":"2023-06-01"
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())["content"][0]["text"]
    except Exception as e:
        return f"Claude error: {e}"

# ══════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════

@app.route("/")
def index():
    # Serve terminal.html from static folder if it exists, otherwise return status
    if os.path.exists("static/index.html"):
        return send_from_directory("static", "index.html")
    return jsonify({"status": "Donut SMP Terminal running", "endpoints": [
        "/api/status", "/api/market", "/api/investments", "/api/hv",
        "/api/flips", "/api/alerts", "/api/history", "/api/search", "/api/brain"
    ]})

@app.route("/api/status")
def r_status():
    conn = get_conn()
    lst  = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    hist = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    alrt = conn.execute("SELECT COUNT(*) FROM alerts WHERE dismissed=0").fetchone()[0]
    last = conn.execute("SELECT MAX(fetched_at) FROM listings").fetchone()[0]
    hv   = conn.execute("SELECT COUNT(DISTINCT item_id) FROM price_history WHERE item_id IN (%s)"
                        % ",".join("?"*len(HIGH_VALUE_ITEMS)), list(HIGH_VALUE_ITEMS)).fetchone()[0]
    conn.close()
    db_kb = os.path.getsize(DB_PATH)//1024 if os.path.exists(DB_PATH) else 0
    return jsonify({
        "status": "online", "listings": lst, "history_rows": hist,
        "active_alerts": alrt, "last_fetch": last, "db_kb": db_kb,
        "key1_set": "YOUR_" not in API_KEY_1 and bool(API_KEY_1),
        "key2_set": bool(API_KEY_2) and "YOUR_" not in API_KEY_2,
        "brain_set": bool(CLAUDE_KEY) and "YOUR_" not in CLAUDE_KEY,
        "hv_items_tracked": hv, "max_pages": MAX_PAGES, "fetch_interval": FETCH_INTERVAL,
    })

@app.route("/api/market")
def r_market():
    conn = get_conn()
    rows = conn.execute("""SELECT item_id,item_name,COUNT(*) listings,MIN(price) min_price,
        MAX(price) max_price,AVG(price) avg_price,SUM(count) total_qty,is_hv
        FROM listings GROUP BY item_id ORDER BY avg_price DESC""").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["median_7d"] = median_7d(conn, d["item_id"])
        d["avg_24h"]   = avg_24h(conn, d["item_id"])
        d["pct_24h"]   = pct_change_24h(conn, d["item_id"])
        d["trend"]     = get_trend(conn, d["item_id"])
        result.append(d)
    result.sort(key=lambda x: x.get("median_7d") or x.get("avg_price") or 0, reverse=True)
    conn.close()
    return jsonify(result)

@app.route("/api/investments")
def r_investments():
    """
    KEY FIX: Works on a FRESH database.
    Falls back to current listings data if no price history exists yet.
    This is why the terminal showed 'Initializing' forever before.
    """
    conn = get_conn(); result = []
    for iid in HIGH_VALUE_ITEMS:
        # Check listings first (available immediately after first fetch)
        lst_row = conn.execute("""SELECT item_name,MIN(price) min_p,AVG(price) avg_p,COUNT(*) cnt
            FROM listings WHERE item_id=? GROUP BY item_id""", (iid,)).fetchone()
        hist_cnt = conn.execute("SELECT COUNT(*) FROM price_history WHERE item_id=?",(iid,)).fetchone()[0]
        # Skip items with no data at all
        if not lst_row and hist_cnt == 0: continue
        m7  = median_7d(conn, iid)
        a24 = avg_24h(conn, iid)
        chg = pct_change_24h(conn, iid)
        cur = lst_row["min_p"] if lst_row else None
        name = lst_row["item_name"] if lst_row else pretty(iid)
        pts = conn.execute("""SELECT recorded_at,AVG(price) p FROM price_history
            WHERE item_id=? AND recorded_at>? GROUP BY CAST(recorded_at/3600 AS INT)
            ORDER BY recorded_at ASC""", (iid, time.time()-7*86400)).fetchall()
        result.append({
            "item_id": iid, "item_name": name,
            "current_min": cur,
            "median_7d":   m7 or cur,      # fallback to current price if no history yet
            "avg_24h":     a24 or cur,
            "pct_24h":     chg,
            "trend":       get_trend(conn, iid),
            "history_count": hist_cnt,
            "points":      [{"t": r[0], "p": r[1]} for r in pts],
        })
    conn.close()
    result.sort(key=lambda x: x.get("median_7d") or x.get("current_min") or 0, reverse=True)
    return jsonify(result)

@app.route("/api/hv")
def r_hv():
    conn = get_conn()
    rows = conn.execute("""SELECT item_id,item_name,COUNT(*) listings,MIN(price) min_price,
        MAX(price) max_price,AVG(price) avg_price,SUM(count) total_qty
        FROM listings WHERE is_hv=1 GROUP BY item_id ORDER BY avg_price DESC""").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["avg_24h"] = avg_24h(conn, d["item_id"])
        d["trend"]   = get_trend(conn, d["item_id"])
        d["median_7d"] = median_7d(conn, d["item_id"])
        d["pct_24h"] = pct_change_24h(conn, d["item_id"])
        result.append(d)
    conn.close()
    return jsonify(result)

@app.route("/api/search")
def r_search():
    q = request.args.get("q","").strip()
    if not q: return jsonify([])
    conn = get_conn()
    rows = conn.execute("""SELECT item_id,item_name,COUNT(*) listings,MIN(price) min_price,
        MAX(price) max_price,AVG(price) avg_price,SUM(count) total_qty
        FROM listings WHERE LOWER(item_name) LIKE ? OR LOWER(item_id) LIKE ?
        GROUP BY item_id ORDER BY avg_price DESC LIMIT 30""",
        (f"%{q.lower()}%", f"%{q.lower()}%")).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["median_7d"] = median_7d(conn, d["item_id"])
        d["avg_24h"]   = avg_24h(conn, d["item_id"])
        d["pct_24h"]   = pct_change_24h(conn, d["item_id"])
        d["trend"]     = get_trend(conn, d["item_id"])
        d["stack_price"] = (d["min_price"]*64) if d["min_price"] else None
        cutoff = time.time()-86400
        rng = conn.execute("SELECT MIN(price),MAX(price) FROM price_history WHERE item_id=? AND recorded_at>?",
                           (d["item_id"],cutoff)).fetchone()
        d["low_24h"]  = rng[0]; d["high_24h"] = rng[1]
        result.append(d)
    conn.close()
    return jsonify(result)

@app.route("/api/flips")
def r_flips():
    conn = get_conn(); flips = []
    for out_id, inputs in RECIPES.items():
        or_ = conn.execute("SELECT MIN(price) FROM listings WHERE item_id=?",(out_id,)).fetchone()
        if not or_ or not or_[0]: continue
        op=or_[0]; cost=0; ings=[]; valid=True
        for ig,qty in inputs:
            ir=conn.execute("SELECT MIN(price) FROM listings WHERE item_id=?",(ig,)).fetchone()
            if not ir or not ir[0]: valid=False; break
            ip=ir[0]; cost+=ip*qty; ings.append({"item":pretty(ig),"qty":qty,"unit":ip,"total":ip*qty})
        if not valid or cost<=0: continue
        profit=op-cost; pct=profit/cost*100
        flips.append({"output":pretty(out_id),"craft_cost":cost,"sell_price":op,
                      "profit":profit,"profit_pct":pct,"hot":pct>FLIP_PROFIT*100,"ingredients":ings})
    conn.close()
    flips.sort(key=lambda x:x["profit_pct"],reverse=True)
    return jsonify(flips)

@app.route("/api/alerts")
def r_alerts():
    limit = int(request.args.get("limit", 30))
    conn  = get_conn()
    rows  = conn.execute("SELECT * FROM alerts WHERE dismissed=0 ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/alerts/dismiss", methods=["POST"])
def r_dismiss():
    aid = request.json.get("id")
    if not aid: return jsonify({"error":"id required"}), 400
    conn = get_conn()
    conn.execute("UPDATE alerts SET dismissed=1 WHERE id=?", (aid,))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/history")
def r_history():
    item   = request.args.get("item","").strip()
    days   = int(request.args.get("days", 7))
    if not item: return jsonify({"error":"item required"}), 400
    cutoff = time.time() - days*86400
    conn   = get_conn()
    rows   = conn.execute("""SELECT recorded_at,AVG(price) p,MIN(price) lo,MAX(price) hi
        FROM price_history WHERE (item_id=? OR LOWER(item_name) LIKE ?) AND recorded_at>?
        GROUP BY CAST(recorded_at/1800 AS INT) ORDER BY recorded_at ASC""",
        (item, f"%{item.lower()}%", cutoff)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/brain", methods=["POST"])
def r_brain():
    q   = (request.json or {}).get("question", "Give me a full market analysis.")
    ctx = build_brain_context()
    ans = call_claude(q, ctx)
    return jsonify({"answer": ans, "context": ctx})

@app.route("/api/fetch", methods=["POST"])
def r_force_fetch():
    threading.Thread(target=lambda: (
        save(fetch_all()),
        run_engines(get_conn())
    ), daemon=True).start()
    return jsonify({"ok": True, "message": "Fetch started"})

# ══════════════════════════════════════
#  MAIN
# ══════════════════════════════════════
if __name__ == "__main__":
    init_db()
    threading.Thread(target=fetch_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 7771))   # Render sets PORT automatically
    log.info("="*50)
    log.info("  Donut SMP Terminal")
    log.info("  Port: %d", port)
    log.info("  Key set: %s", "YES" if "YOUR_" not in API_KEY_1 else "NO — set API_KEY env var")
    log.info("="*50)
    app.run(host="0.0.0.0", port=port, debug=False)
