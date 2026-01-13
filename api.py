from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
import sqlite3
import json
import time
import simpy
import random

app = FastAPI(title="DTaaS", version="0.2")

DB_PATH = "dtaas.db"


# ============================================================
# DB helpers (SQLite)
# ============================================================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS twins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        model_type TEXT NOT NULL,
        config_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS scenarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        twin_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        overrides_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY(twin_id) REFERENCES twins(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        twin_id INTEGER NOT NULL,
        scenario_id INTEGER,
        result_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY(twin_id) REFERENCES twins(id),
        FOREIGN KEY(scenario_id) REFERENCES scenarios(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        twin_id INTEGER NOT NULL,
        ts REAL NOT NULL,
        event_type TEXT NOT NULL,
        station_id TEXT,
        payload_json TEXT,
        FOREIGN KEY(twin_id) REFERENCES twins(id)
    )
    """)

    conn.commit()
    conn.close()

init_db()


# ============================================================
# SimPy flow line simulation (multi-station)
# Uses instance like UI-builder output:
# { line, stations[], buffers[], sim{horizon_s, interarrival_s} }
# ============================================================
def sim_flow_line(instance: Dict[str, Any]) -> Dict[str, Any]:
    horizon_s = float(instance["sim"]["horizon_s"])
    interarrival_s = float(instance["sim"]["interarrival_s"])

    stations = instance["stations"]
    buffers = instance.get("buffers", [])

    env = simpy.Environment()
    res = {s["id"]: simpy.Resource(env, capacity=1) for s in stations}

    stores: List[simpy.Store] = []
    for b in buffers:
        cap = int(b.get("capacity", 0))
        stores.append(simpy.Store(env, capacity=cap if cap > 0 else 1))

    completed = 0
    started = 0

    def process_station(s, job_id):
        avail = float(s["availability_pct"]) / 100.0
        ct = float(s["cycle_time_s"])
        scrap = float(s.get("scrap_rate_pct", 0.0)) / 100.0

        with res[s["id"]].request() as req:
            yield req
            if random.random() > avail:
                yield env.timeout(ct)  # downtime proxy
            yield env.timeout(ct)

        good = (random.random() > scrap)
        return good

    def job(job_id):
        nonlocal completed, started
        started += 1

        for i, s in enumerate(stations):
            good = yield env.process(process_station(s, job_id))
            if not good:
                return

            if i < len(stations) - 1 and len(stores) > 0:
                yield stores[i].put(job_id)
                _ = yield stores[i].get()

        completed += 1

    def source():
        job_id = 0
        while env.now < horizon_s:
            job_id += 1
            env.process(job(job_id))
            yield env.timeout(interarrival_s)

    env.process(source())
    env.run(until=horizon_s)

    throughput_pph = (completed / horizon_s) * 3600.0
    return {
        "horizon_s": horizon_s,
        "started_jobs": started,
        "completed_good": completed,
        "throughput_pph": round(throughput_pph, 2),
    }


# ============================================================
# Models / payloads
# ============================================================
class CreateTwinPayload(BaseModel):
    name: str
    model_type: str = "flow_line"
    config: Dict[str, Any]  # base instance/config for the twin

class CreateScenarioPayload(BaseModel):
    name: str
    overrides: Dict[str, Any]  # partial override to merge into twin config

class RunPayload(BaseModel):
    sim: Optional[Dict[str, Any]] = None  # optional override of sim settings

class EventPayload(BaseModel):
    event_type: str
    ts: Optional[float] = None
    station_id: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None

def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ============================================================
# Twin CRUD
# ============================================================
@app.post("/twins")
def create_twin(p: CreateTwinPayload):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO twins(name, model_type, config_json, created_at) VALUES (?,?,?,?)",
        (p.name, p.model_type, json.dumps(p.config), time.time()),
    )
    twin_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"twin_id": twin_id}

@app.get("/twins")
def list_twins():
    conn = db()
    rows = conn.execute("SELECT id, name, model_type, created_at FROM twins ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/twins/{twin_id}")
def get_twin(twin_id: int):
    conn = db()
    r = conn.execute("SELECT * FROM twins WHERE id=?", (twin_id,)).fetchone()
    conn.close()
    if not r:
        return {"error": "twin not found"}
    d = dict(r)
    d["config"] = json.loads(d.pop("config_json"))
    return d


# ============================================================
# What-if scenarios
# ============================================================
@app.post("/twins/{twin_id}/scenarios")
def create_scenario(twin_id: int, p: CreateScenarioPayload):
    conn = db()
    # verify twin exists
    t = conn.execute("SELECT id FROM twins WHERE id=?", (twin_id,)).fetchone()
    if not t:
        conn.close()
        return {"error": "twin not found"}

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scenarios(twin_id, name, overrides_json, created_at) VALUES (?,?,?,?)",
        (twin_id, p.name, json.dumps(p.overrides), time.time()),
    )
    scenario_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"scenario_id": scenario_id}

@app.get("/twins/{twin_id}/scenarios")
def list_scenarios(twin_id: int):
    conn = db()
    rows = conn.execute(
        "SELECT id, name, created_at FROM scenarios WHERE twin_id=? ORDER BY id DESC",
        (twin_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/twins/{twin_id}/scenarios/{scenario_id}/run")
def run_scenario(twin_id: int, scenario_id: int, p: RunPayload = RunPayload()):
    conn = db()
    t = conn.execute("SELECT config_json FROM twins WHERE id=?", (twin_id,)).fetchone()
    s = conn.execute("SELECT overrides_json FROM scenarios WHERE id=? AND twin_id=?", (scenario_id, twin_id)).fetchone()
    if not t or not s:
        conn.close()
        return {"error": "twin or scenario not found"}

    base_cfg = json.loads(t["config_json"])
    overrides = json.loads(s["overrides_json"])
    instance = deep_merge(base_cfg, overrides)

    if p.sim:
        instance["sim"] = deep_merge(instance.get("sim", {}), p.sim)

    result = sim_flow_line(instance)

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO runs(twin_id, scenario_id, result_json, created_at) VALUES (?,?,?,?)",
        (twin_id, scenario_id, json.dumps(result), time.time()),
    )
    run_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"run_id": run_id, "result": result}

@app.post("/twins/{twin_id}/run")
def run_base(twin_id: int, p: RunPayload = RunPayload()):
    conn = db()
    t = conn.execute("SELECT config_json FROM twins WHERE id=?", (twin_id,)).fetchone()
    if not t:
        conn.close()
        return {"error": "twin not found"}

    instance = json.loads(t["config_json"])
    if p.sim:
        instance["sim"] = deep_merge(instance.get("sim", {}), p.sim)

    result = sim_flow_line(instance)

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO runs(twin_id, scenario_id, result_json, created_at) VALUES (?,?,?,?)",
        (twin_id, None, json.dumps(result), time.time()),
    )
    run_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"run_id": run_id, "result": result}

@app.get("/twins/{twin_id}/compare")
def compare_runs(twin_id: int, last_n: int = 5):
    conn = db()
    rows = conn.execute(
        "SELECT id, scenario_id, result_json, created_at FROM runs WHERE twin_id=? ORDER BY id DESC LIMIT ?",
        (twin_id, last_n),
    ).fetchall()
    conn.close()

    out = []
    for r in rows:
        d = dict(r)
        d["result"] = json.loads(d.pop("result_json"))
        out.append(d)
    return out


# ============================================================
# Monitoring (events -> live KPIs)
# Minimal but already useful: throughput, scrap, downtime, uptime
# ============================================================
@app.post("/twins/{twin_id}/events")
def post_event(twin_id: int, e: EventPayload):
    ts = float(e.ts) if e.ts is not None else time.time()

    conn = db()
    t = conn.execute("SELECT id FROM twins WHERE id=?", (twin_id,)).fetchone()
    if not t:
        conn.close()
        return {"error": "twin not found"}

    conn.execute(
        "INSERT INTO events(twin_id, ts, event_type, station_id, payload_json) VALUES (?,?,?,?,?)",
        (twin_id, ts, e.event_type, e.station_id, json.dumps(e.payload) if e.payload else None),
    )
    conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/twins/{twin_id}/kpis/live")
def live_kpis(twin_id: int, window_s: int = 3600):
    now = time.time()
    tmin = now - window_s

    conn = db()
    rows = conn.execute(
        "SELECT ts, event_type, station_id, payload_json FROM events WHERE twin_id=? AND ts>=? ORDER BY ts ASC",
        (twin_id, tmin),
    ).fetchall()
    conn.close()

    completed = 0
    scrap = 0

    # downtime per station: track last down start
    down_start: Dict[str, float] = {}
    down_time: Dict[str, float] = {}

    for r in rows:
        et = r["event_type"]
        sid = r["station_id"] or "UNK"
        ts = float(r["ts"])

        if et == "part_completed":
            completed += 1
        elif et == "scrap":
            scrap += 1
        elif et == "machine_down":
            down_start[sid] = ts
        elif et == "machine_up":
            if sid in down_start:
                dt = ts - down_start.pop(sid)
                down_time[sid] = down_time.get(sid, 0.0) + max(0.0, dt)

    # If still down at end of window
    for sid, start_ts in down_start.items():
        dt = now - start_ts
        down_time[sid] = down_time.get(sid, 0.0) + max(0.0, dt)

    hours = window_s / 3600.0
    throughput_pph = completed / hours if hours > 0 else 0.0
    scrap_rate = (scrap / completed) if completed > 0 else 0.0

    return {
        "window_s": window_s,
        "completed": completed,
        "scrap": scrap,
        "throughput_pph": round(throughput_pph, 2),
        "scrap_rate": round(scrap_rate, 4),
        "downtime_s_by_station": {k: round(v, 1) for k, v in down_time.items()},
    }


# ============================================================
# Minimal UI: create twin + run + scenarios + monitoring demo
# ============================================================
@app.get("/ui-ops", response_class=HTMLResponse)
def ui_ops():
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>DTaaS – Ops</title>
  <style>
    body { font-family: system-ui, Arial; max-width: 980px; margin: 40px auto; padding: 0 16px; }
    textarea { width: 100%; height: 220px; font-family: ui-monospace, Menlo, Consolas, monospace; }
    button { padding: 10px 14px; cursor: pointer; }
    pre { background:#f6f6f6; padding:12px; overflow:auto; }
    .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin:10px 0; }
    input { padding: 6px; }
  </style>
</head>
<body>
  <h2>DTaaS – What-if + Monitoring (v0.2)</h2>

  <h3>1) Create a Twin (flow line)</h3>
  <p>Config base (instance) che verrà salvata come twin.</p>
  <textarea id="cfg">{
  "line": { "line_name": "Line_A" },
  "stations": [
    {"id":"S1","type":"assembly","cycle_time_s":20,"availability_pct":92,"scrap_rate_pct":1.0},
    {"id":"S2","type":"welding","cycle_time_s":25,"availability_pct":90,"scrap_rate_pct":1.5},
    {"id":"S3","type":"assembly","cycle_time_s":22,"availability_pct":93,"scrap_rate_pct":0.8}
  ],
  "buffers": [
    {"id":"B12","capacity":10},
    {"id":"B23","capacity":10}
  ],
  "sim": { "horizon_s": 3600, "interarrival_s": 5 }
}</textarea>
  <div class="row">
    <input id="tname" value="Twin_Line_A" />
    <button onclick="createTwin()">Create Twin</button>
    <span id="twinid"></span>
  </div>

  <h3>2) Run base + create scenario + run scenario</h3>
  <div class="row">
    <button onclick="runBase()">Run Base</button>
    <button onclick="createScenario()">Create Scenario (S2 faster)</button>
    <button onclick="runScenario()">Run Scenario</button>
  </div>

  <h3>3) Monitoring demo (send events)</h3>
  <div class="row">
    <button onclick="sendEvent('part_completed')">+ part_completed</button>
    <button onclick="sendEvent('scrap')">+ scrap</button>
    <button onclick="sendEvent('machine_down')">machine_down S2</button>
    <button onclick="sendEvent('machine_up')">machine_up S2</button>
    <button onclick="liveKpis()">Get live KPIs (last hour)</button>
  </div>

  <h3>Output</h3>
  <pre id="out">—</pre>

<script>
let TWIN_ID = null;
let SCENARIO_ID = null;

function out(x){ document.getElementById('out').textContent = JSON.stringify(x,null,2); }

async function createTwin(){
  const cfg = JSON.parse(document.getElementById('cfg').value);
  const name = document.getElementById('tname').value || 'Twin';
  const res = await fetch('/twins', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ name, model_type:'flow_line', config: cfg })
  });
  const data = await res.json();
  TWIN_ID = data.twin_id;
  document.getElementById('twinid').textContent = 'twin_id=' + TWIN_ID;
  out(data);
}

async function runBase(){
  if(!TWIN_ID) return out({error:'create twin first'});
  const res = await fetch(`/twins/${TWIN_ID}/run`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({})});
  out(await res.json());
}

async function createScenario(){
  if(!TWIN_ID) return out({error:'create twin first'});
  // example: make S2 faster by overriding stations[1].cycle_time_s
  const overrides = {
    "stations": [
      {"id":"S1","type":"assembly","cycle_time_s":20,"availability_pct":92,"scrap_rate_pct":1.0},
      {"id":"S2","type":"welding","cycle_time_s":18,"availability_pct":90,"scrap_rate_pct":1.5},
      {"id":"S3","type":"assembly","cycle_time_s":22,"availability_pct":93,"scrap_rate_pct":0.8}
    ]
  };
  const res = await fetch(`/twins/${TWIN_ID}/scenarios`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ name:'S2 faster', overrides })
  });
  const data = await res.json();
  SCENARIO_ID = data.scenario_id;
  out(data);
}

async function runScenario(){
  if(!TWIN_ID || !SCENARIO_ID) return out({error:'create scenario first'});
  const res = await fetch(`/twins/${TWIN_ID}/scenarios/${SCENARIO_ID}/run`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({})
  });
  out(await res.json());
}

async function sendEvent(type){
  if(!TWIN_ID) return out({error:'create twin first'});
  const payload = (type==='machine_down' || type==='machine_up') ? {station_id:'S2'} : {};
  const res = await fetch(`/twins/${TWIN_ID}/events`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ event_type:type, station_id: payload.station_id || null })
  });
  out(await res.json());
}

async function liveKpis(){
  if(!TWIN_ID) return out({error:'create twin first'});
  const res = await fetch(`/twins/${TWIN_ID}/kpis/live?window_s=3600`);
  out(await res.json());
}
</script>

  <p style="margin-top:20px;">
    API Docs: <a href="/docs">/docs</a>
  </p>
</body>
</html>
"""

@app.get("/status")
def status():
    return {"status": "ok", "service": "DTaaS", "docs": "/docs"}

@app.get("/")
def root():
    return RedirectResponse("/ui-ops")
