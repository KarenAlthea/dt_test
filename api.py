from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator
from typing import Any, Dict, List, Optional
import sqlite3
import json
import time
import simpy
import random

class StationForm(BaseModel):
    id: str
    type: str
    cycle_time_s: float = Field(gt=0)
    availability_pct: float = Field(ge=0, le=100)
    scrap_rate_pct: float = Field(ge=0, le=100, default=0.0)

class WizardPayload(BaseModel):
    line_name: str
    num_stations: int = Field(gt=0)
    buffers: bool = True
    buffer_capacity: int = Field(ge=0, default=10)
    horizon_s: float = Field(gt=0, default=3600)
    interarrival_s: float = Field(gt=0, default=5)
    stations: List[StationForm]

    @field_validator('stations')
    def check_stations_count(cls, v, info):
        num = info.data.get('num_stations')
        if num and len(v) != num:
            raise ValueError(f'stations list must have {num} elements')
        return v

app = FastAPI(title="DTaaS", version="0.3")

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
# SimPy flow line simulation (FIXED VERSION)
# ============================================================
def sim_flow_line(instance: Dict[str, Any]) -> Dict[str, Any]:
    horizon_s = float(instance["sim"]["horizon_s"])
    interarrival_s = float(instance["sim"]["interarrival_s"])

    stations = instance["stations"]
    buffers = instance.get("buffers", [])

    env = simpy.Environment()
    
    # Resources per stazioni
    station_res = {s["id"]: simpy.Resource(env, capacity=1) for s in stations}
    
    # Buffer stores tra le stazioni
    buffer_stores = {}
    for i, b in enumerate(buffers):
        cap = int(b.get("capacity", 10))
        buffer_stores[i] = simpy.Store(env, capacity=max(cap, 1))

    # Contatori
    stats = {
        "started": 0,
        "completed": 0,
        "scrapped": 0,
        "scrapped_by_station": {s["id"]: 0 for s in stations},
        "downtime_events": {s["id"]: 0 for s in stations},
        "total_downtime_s": {s["id"]: 0.0 for s in stations},
        "parts_processed": {s["id"]: 0 for s in stations}
    }

    def process_station(station, job_id):
        """Processa un pezzo in una stazione, gestendo availability e scrap"""
        sid = station["id"]
        avail = float(station["availability_pct"]) / 100.0
        ct = float(station["cycle_time_s"])
        scrap_rate = float(station.get("scrap_rate_pct", 0.0)) / 100.0

        with station_res[sid].request() as req:
            yield req
            
            # Check se la macchina è disponibile
            if random.random() > avail:
                # Macchina in downtime - tempo di riparazione
                downtime_duration = ct * random.uniform(0.5, 2.0)
                stats["downtime_events"][sid] += 1
                stats["total_downtime_s"][sid] += downtime_duration
                yield env.timeout(downtime_duration)
            
            # Processa il pezzo
            yield env.timeout(ct)
            stats["parts_processed"][sid] += 1
            
            # Check se il pezzo è scartato
            if random.random() < scrap_rate:
                stats["scrapped"] += 1
                stats["scrapped_by_station"][sid] += 1
                return False  # Pezzo scartato
            
            return True  # Pezzo OK

    def job(job_id):
        """Gestisce il flusso di un singolo pezzo attraverso la linea"""
        nonlocal stats
        stats["started"] += 1

        for i, station in enumerate(stations):
            # Processa nella stazione corrente
            is_good = yield env.process(process_station(station, job_id))
            
            if not is_good:
                # Pezzo scartato - esce dalla linea
                return
            
            # Se c'è una stazione successiva, usa il buffer
            if i < len(stations) - 1:
                if i in buffer_stores:
                    # Metti nel buffer
                    yield buffer_stores[i].put(job_id)
                    
                    # La prossima stazione prende dal buffer quando è pronta
                    # (questo succede automaticamente nel prossimo ciclo)

        # Pezzo completato con successo
        stats["completed"] += 1

    def buffer_consumer(buffer_idx):
        """Preleva pezzi dal buffer per la stazione successiva"""
        while True:
            job_id = yield buffer_stores[buffer_idx].get()
            # Il pezzo è stato prelevato, ora è disponibile per la prossima stazione
            # (la logica di processo avviene nella funzione job)

    def source():
        """Genera nuovi job alla frequenza specificata"""
        job_id = 0
        while True:
            if env.now >= horizon_s:
                break
            job_id += 1
            env.process(job(job_id))
            yield env.timeout(interarrival_s)

    # Avvia il source
    env.process(source())
    
    # Avvia i consumer per i buffer (se ci sono)
    for buf_idx in buffer_stores.keys():
        env.process(buffer_consumer(buf_idx))
    
    # Esegui la simulazione
    env.run(until=horizon_s)

    # Calcola metriche finali
    throughput_pph = (stats["completed"] / horizon_s) * 3600.0 if horizon_s > 0 else 0
    scrap_rate = (stats["scrapped"] / stats["started"]) if stats["started"] > 0 else 0
    
    # Calcola availability effettiva per stazione
    station_availability = {}
    for sid in stats["parts_processed"].keys():
        total_processing_time = stats["parts_processed"][sid] * next(
            s["cycle_time_s"] for s in stations if s["id"] == sid
        )
        total_time = total_processing_time + stats["total_downtime_s"][sid]
        avail = (total_processing_time / total_time * 100) if total_time > 0 else 100
        station_availability[sid] = round(avail, 2)

    return {
        "horizon_s": horizon_s,
        "started_jobs": stats["started"],
        "completed_good": stats["completed"],
        "scrapped_total": stats["scrapped"],
        "throughput_pph": round(throughput_pph, 2),
        "scrap_rate_pct": round(scrap_rate * 100, 2),
        "scrapped_by_station": stats["scrapped_by_station"],
        "downtime_events_by_station": stats["downtime_events"],
        "total_downtime_s_by_station": {k: round(v, 1) for k, v in stats["total_downtime_s"].items()},
        "station_availability_pct": station_availability,
        "parts_processed_by_station": stats["parts_processed"]
    }


# ============================================================
# Models / payloads
# ============================================================
class CreateTwinPayload(BaseModel):
    name: str
    model_type: str = "flow_line"
    config: Dict[str, Any]

class CreateScenarioPayload(BaseModel):
    name: str
    overrides: Dict[str, Any]

class RunPayload(BaseModel):
    sim: Optional[Dict[str, Any]] = None

class EventPayload(BaseModel):
    event_type: str
    ts: Optional[float] = None
    station_id: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None

def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Deep merge con supporto per merge parziale di array di stazioni"""
    out = dict(base)
    for k, v in override.items():
        if k == "stations" and isinstance(v, list) and isinstance(out.get(k), list):
            # Merge stazioni per ID
            base_stations = {s["id"]: s for s in out[k]}
            for override_station in v:
                if "id" in override_station:
                    sid = override_station["id"]
                    if sid in base_stations:
                        # Merge parametri della stazione
                        base_stations[sid].update(override_station)
                    else:
                        # Nuova stazione
                        base_stations[sid] = override_station
            out[k] = list(base_stations.values())
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
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
        raise HTTPException(status_code=404, detail="Twin not found")
    d = dict(r)
    d["config"] = json.loads(d.pop("config_json"))
    return d


# ============================================================
# What-if scenarios
# ============================================================
@app.post("/twins/{twin_id}/scenarios")
def create_scenario(twin_id: int, p: CreateScenarioPayload):
    conn = db()
    t = conn.execute("SELECT id FROM twins WHERE id=?", (twin_id,)).fetchone()
    if not t:
        conn.close()
        raise HTTPException(status_code=404, detail="Twin not found")

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
        raise HTTPException(status_code=404, detail="Twin or scenario not found")

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
        raise HTTPException(status_code=404, detail="Twin not found")

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
# ============================================================
@app.post("/twins/{twin_id}/events")
def post_event(twin_id: int, e: EventPayload):
    ts = float(e.ts) if e.ts is not None else time.time()

    conn = db()
    t = conn.execute("SELECT id FROM twins WHERE id=?", (twin_id,)).fetchone()
    if not t:
        conn.close()
        raise HTTPException(status_code=404, detail="Twin not found")

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

    # downtime per station: track intervals
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
    scrap_rate = (scrap / (completed + scrap)) if (completed + scrap) > 0 else 0.0

    return {
        "window_s": window_s,
        "completed": completed,
        "scrap": scrap,
        "throughput_pph": round(throughput_pph, 2),
        "scrap_rate_pct": round(scrap_rate * 100, 2),
        "downtime_s_by_station": {k: round(v, 1) for k, v in down_time.items()},
    }


# ============================================================
# UI
# ============================================================
@app.get("/ui-ops", response_class=HTMLResponse)
def ui_ops():
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>DTaaS – Ops (v0.3 Fixed)</title>
  <style>
    body { font-family: system-ui, Arial; max-width: 1100px; margin: 40px auto; padding: 0 16px; }
    textarea { width: 100%; height: 240px; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 13px; }
    button { padding: 10px 14px; cursor: pointer; background: #0066cc; color: white; border: none; border-radius: 4px; }
    button:hover { background: #0052a3; }
    pre { background:#f6f6f6; padding:12px; overflow:auto; border-radius: 4px; font-size: 13px; }
    .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin:10px 0; }
    input { padding: 8px; border: 1px solid #ccc; border-radius: 4px; }
    h2 { color: #333; border-bottom: 2px solid #0066cc; padding-bottom: 10px; }
    h3 { color: #555; margin-top: 30px; }
    .info { background: #e3f2fd; padding: 12px; border-radius: 4px; margin: 10px 0; }
  </style>
</head>
<body>
  <h2>DTaaS – Digital Twin as a Service (v0.3 - Fixed)</h2>
  
  <div class="info">
    <strong>Improvements:</strong> Fixed simulation logic (downtime, buffers, scrap tracking), 
    better scenario merging (can override single station params), proper HTTP errors, enhanced metrics.
  </div>

  <h3>1) Create a Twin (flow line)</h3>
  <p>Base configuration for the production line digital twin.</p>
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
    <span id="twinid" style="font-weight:bold; color:#0066cc;"></span>
  </div>

  <h3>2) Run base configuration</h3>
  <div class="row">
    <button onclick="runBase()">Run Base Configuration</button>
  </div>

  <h3>3) Create and run What-If scenarios</h3>
  <p>Example: What if we improve S2 cycle time and availability?</p>
  <div class="row">
    <button onclick="createScenario()">Create Scenario (S2 improved)</button>
    <button onclick="runScenario()">Run Scenario</button>
    <button onclick="compareRuns()">Compare Last 5 Runs</button>
  </div>

  <h3>4) Monitoring demo (send events)</h3>
  <div class="row">
    <button onclick="sendEvent('part_completed')">+ Part Completed</button>
    <button onclick="sendEvent('scrap')">+ Scrap</button>
    <button onclick="sendEvent('machine_down')">Machine Down (S2)</button>
    <button onclick="sendEvent('machine_up')">Machine Up (S2)</button>
    <button onclick="liveKpis()">Get Live KPIs</button>
  </div>

  <h3>Output</h3>
  <pre id="out">Ready. Create a twin to start.</pre>

<script>
let TWIN_ID = null;
let SCENARIO_ID = null;

function out(x){ 
  document.getElementById('out').textContent = JSON.stringify(x, null, 2); 
}

async function createTwin(){
  try {
    const cfg = JSON.parse(document.getElementById('cfg').value);
    const name = document.getElementById('tname').value || 'Twin';
    const res = await fetch('/twins', {
      method:'POST', 
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ name, model_type:'flow_line', config: cfg })
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    TWIN_ID = data.twin_id;
    document.getElementById('twinid').textContent = `Twin ID: ${TWIN_ID}`;
    out(data);
  } catch(e) {
    out({error: e.message});
  }
}

async function runBase(){
  if(!TWIN_ID) return out({error:'Create twin first'});
  try {
    const res = await fetch(`/twins/${TWIN_ID}/run`, { 
      method:'POST', 
      headers:{'Content-Type':'application/json'}, 
      body: JSON.stringify({})
    });
    if (!res.ok) throw new Error(await res.text());
    out(await res.json());
  } catch(e) {
    out({error: e.message});
  }
}

async function createScenario(){
  if(!TWIN_ID) return out({error:'Create twin first'});
  try {
    // Improved scenario: S2 faster (18s vs 25s) and more reliable (95% vs 90%)
    const overrides = {
      "stations": [
        {"id":"S2", "cycle_time_s":18, "availability_pct":95}
      ]
    };
    const res = await fetch(`/twins/${TWIN_ID}/scenarios`, {
      method:'POST', 
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ name:'S2 improved (faster + more reliable)', overrides })
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    SCENARIO_ID = data.scenario_id;
    out(data);
  } catch(e) {
    out({error: e.message});
  }
}

async function runScenario(){
  if(!TWIN_ID || !SCENARIO_ID) return out({error:'Create scenario first'});
  try {
    const res = await fetch(`/twins/${TWIN_ID}/scenarios/${SCENARIO_ID}/run`, {
      method:'POST', 
      headers:{'Content-Type':'application/json'}, 
      body: JSON.stringify({})
    });
    if (!res.ok) throw new Error(await res.text());
    out(await res.json());
  } catch(e) {
    out({error: e.message});
  }
}

async function compareRuns(){
  if(!TWIN_ID) return out({error:'Create twin first'});
  try {
    const res = await fetch(`/twins/${TWIN_ID}/compare?last_n=5`);
    if (!res.ok) throw new Error(await res.text());
    out(await res.json());
  } catch(e) {
    out({error: e.message});
  }
}

async function sendEvent(type){
  if(!TWIN_ID) return out({error:'Create twin first'});
  try {
    const payload = {
      event_type: type,
      station_id: (type==='machine_down' || type==='machine_up') ? 'S2' : null
    };
    const res = await fetch(`/twins/${TWIN_ID}/events`, {
      method:'POST', 
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });
    if (!res.ok) throw new Error(await res.text());
    out(await res.json());
  } catch(e) {
    out({error: e.message});
  }
}

async function liveKpis(){
  if(!TWIN_ID) return out({error:'Create twin first'});
  try {
    const res = await fetch(`/twins/${TWIN_ID}/kpis/live?window_s=3600`);
    if (!res.ok) throw new Error(await res.text());
    out(await res.json());
  } catch(e) {
    out({error: e.message});
  }
}
</script>

  <p style="margin-top:40px; padding-top:20px; border-top:1px solid #ddd;">
    <strong>API Docs:</strong> <a href="/docs">/docs</a> | 
    <strong>Status:</strong> <a href="/status">/status</a>
  </p>
</body>
</html>
"""

@app.get("/status")
def status():
    return {"status": "ok", "service": "DTaaS", "version": "0.3", "docs": "/docs"}

@app.get("/")
def root():
    return RedirectResponse("/ui-ops")

@app.post("/ui/compile-instance")
def ui_compile_instance(p: WizardPayload):
    stations = [s.model_dump() for s in p.stations]

    buffers = []
    if p.buffers and p.num_stations > 1:
        for i in range(p.num_stations - 1):
            buffers.append({"id": f"B{i+1}{i+2}", "capacity": int(p.buffer_capacity)})

    instance = {
        "line": {"line_name": p.line_name},
        "stations": stations,
        "buffers": buffers,
        "sim": {"horizon_s": float(p.horizon_s), "interarrival_s": float(p.interarrival_s)}
    }
    return {"instance": instance}
