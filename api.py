from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from typing import Any, Dict, List
import simpy
import random


app = FastAPI(title="Digital Twin as a Service", version="0.1")


# ============================================================
# 1) Component library (minimal)
# ============================================================
COMPONENTS = {
    "source": {
        "name": "Source",
        "params": {
            "interarrival_s": {
                "type": "number",
                "minimum": 0.1,
                "default": 5,
                "title": "Interarrival time (s)",
            }
        },
    },
    "station": {
        "name": "Station",
        "params": {
            "type": {"type": "string", "enum": ["assembly", "welding"], "title": "Station type"},
            "cycle_time_s": {"type": "number", "minimum": 1, "maximum": 600, "title": "Cycle time (s)"},
            "availability_pct": {"type": "number", "minimum": 50, "maximum": 99.9, "title": "Availability (%)"},
            "scrap_rate_pct": {"type": "number", "minimum": 0, "maximum": 20, "default": 0.0, "title": "Scrap (%)"},
        },
    },
    "buffer": {
        "name": "Buffer",
        "params": {
            "capacity": {"type": "integer", "minimum": 0, "maximum": 500, "default": 10, "title": "Capacity (pcs)"}
        },
    },
    "sink": {"name": "Sink", "params": {}},
}


@app.get("/components")
def list_components():
    return [{"id": k, "name": v["name"]} for k, v in COMPONENTS.items()]


# ============================================================
# 2) Template Builder (dynamic schema from "case")
# ============================================================
class CasePayload(BaseModel):
    case: Dict[str, Any]


def build_template_schema(case: dict) -> dict:
    """
    case esempio:
    {
      "line_type": "flow_line",
      "num_stations": 3,
      "buffer_between": true
    }
    """
    num_stations = int(case.get("num_stations", 1))
    buffer_between = bool(case.get("buffer_between", True))

    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["line", "stations", "buffers", "sim"],
        "properties": {
            "line": {
                "type": "object",
                "required": ["line_name"],
                "properties": {
                    "line_name": {"type": "string", "minLength": 2, "title": "Line name"}
                },
            },
            "stations": {
                "type": "array",
                "minItems": num_stations,
                "maxItems": num_stations,
                "items": {
                    "type": "object",
                    "required": ["id", "type", "cycle_time_s", "availability_pct"],
                    "properties": {
                        "id": {"type": "string", "title": "Station ID"},
                        **COMPONENTS["station"]["params"],
                    },
                },
            },
            "buffers": {
                "type": "array",
                "minItems": (num_stations - 1) if buffer_between and num_stations > 1 else 0,
                "maxItems": (num_stations - 1) if buffer_between and num_stations > 1 else 0,
                "items": {
                    "type": "object",
                    "required": ["id", "capacity"],
                    "properties": {
                        "id": {"type": "string", "title": "Buffer ID"},
                        **COMPONENTS["buffer"]["params"],
                    },
                },
            },
            "sim": {
                "type": "object",
                "required": ["horizon_s", "interarrival_s"],
                "properties": {
                    "horizon_s": {"type": "number", "minimum": 10, "default": 3600, "title": "Simulation horizon (s)"},
                    "interarrival_s": COMPONENTS["source"]["params"]["interarrival_s"],
                },
            },
        },
    }
    return schema


@app.post("/template-builder/schema")
def template_builder_schema(payload: CasePayload):
    return build_template_schema(payload.case)


@app.get("/ui-builder", response_class=HTMLResponse)
def ui_builder():
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>DTaaS – Template Builder UI</title>
  <style>
    body { font-family: system-ui, Arial; max-width: 980px; margin: 40px auto; padding: 0 16px; }
    .row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
    button { padding: 10px 14px; cursor: pointer; }
    pre { background:#f6f6f6; padding:12px; overflow:auto; }
    #editor_holder { margin-top: 16px; }
    label { font-weight: 600; }
    input, select { padding: 6px; }
  </style>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@json-editor/json-editor@latest/dist/css/jsoneditor.min.css">
</head>
<body>
  <h2>DTaaS – Template Builder</h2>
  <p>Scegli il caso d’uso → la piattaforma genera il template → compili il form → simulazione.</p>

  <div class="row">
    <label>Num stations</label>
    <input id="num_stations" type="number" min="1" max="10" value="3" />
    <label>Buffers</label>
    <select id="buffers">
      <option value="true" selected>Yes</option>
      <option value="false">No</option>
    </select>
    <button onclick="build()">Generate Form</button>
    <button onclick="simulate()">Run SimPy Simulation</button>
    <span id="status"></span>
  </div>

  <div id="editor_holder"></div>

  <h3>Output</h3>
  <pre id="out">—</pre>

  <script src="https://cdn.jsdelivr.net/npm/@json-editor/json-editor@latest/dist/jsoneditor.min.js"></script>
  <script>
    let editor = null;

    async function build() {
      const status = document.getElementById('status');
      status.textContent = 'Generating...';

      const num_stations = parseInt(document.getElementById('num_stations').value || '1');
      const buffer_between = (document.getElementById('buffers').value === 'true');

      const res = await fetch('/template-builder/schema', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ case: { line_type: "flow_line", num_stations, buffer_between } })
      });
      const schema = await res.json();

      if (editor) { editor.destroy(); editor = null; }

      editor = new JSONEditor(document.getElementById('editor_holder'), {
        schema: schema,
        disable_collapse: true,
        disable_properties: true,
        no_additional_properties: true,
        required_by_default: true
      });

      const initStations = Array.from({length:num_stations}).map((_,i)=>({
        id: `S${i+1}`, type: (i%2===0?'assembly':'welding'),
        cycle_time_s: 20, availability_pct: 92, scrap_rate_pct: 1.0
      }));
      const initBuffers = buffer_between ? Array.from({length:Math.max(0,num_stations-1)}).map((_,i)=>({
        id: `B${i+1}${i+2}`, capacity: 10
      })) : [];

      editor.setValue({
        line: { line_name: "Line_A" },
        stations: initStations,
        buffers: initBuffers,
        sim: { horizon_s: 3600, interarrival_s: 5 }
      });

      status.textContent = 'Ready';
      document.getElementById('out').textContent = '—';
    }

    async function simulate() {
      const status = document.getElementById('status');
      const out = document.getElementById('out');
      out.textContent = '—';

      if (!editor) { status.textContent = 'Generate form first'; return; }

      const errors = editor.validate();
      if (errors.length) { status.textContent = 'Fix validation errors'; out.textContent = JSON.stringify(errors,null,2); return; }

      status.textContent = 'Running simulation...';
      const instance = editor.getValue();

      const res = await fetch('/simulate', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ instance })
      });
      const data = await res.json();

      status.textContent = res.ok ? 'OK' : ('Error ' + res.status);
      out.textContent = JSON.stringify(data, null, 2);
    }

    build();
  </script>
</body>
</html>
"""


# ============================================================
# 3) SimPy flow line simulation (multi-station)
# ============================================================
class InstancePayload2(BaseModel):
    instance: Dict[str, Any]


def sim_flow_line(instance: Dict[str, Any]) -> Dict[str, Any]:
    horizon_s = float(instance["sim"]["horizon_s"])
    interarrival_s = float(instance["sim"]["interarrival_s"])

    stations = instance["stations"]
    buffers = instance.get("buffers", [])

    env = simpy.Environment()
    res = {s["id"]: simpy.Resource(env, capacity=1) for s in stations}

    stores: List[simpy.Store] = []
    for b in buffers:
        cap = int(b["capacity"])
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
                yield env.timeout(ct)  # extra delay
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


@app.post("/simulate")
def simulate(payload: InstancePayload2):
    return {"result": sim_flow_line(payload.instance)}


# ============================================================
# 4) Single-station "digital twin" compile + KPI
# ============================================================
class InstancePayload(BaseModel):
    instance: Dict[str, Any]


def compile_twin(instance: Dict[str, Any]) -> Dict[str, Any]:
    station = instance["station"]
    twin = {
        "twin_id": instance["line"]["line_name"],
        "type": "single_station_cell",
        "nodes": [
            {"id": "SRC", "kind": "source"},
            {
                "id": station["id"],
                "kind": "station",
                "station_type": station["type"],
                "params": {
                    "cycle_time_s": station["cycle_time_s"],
                    "availability_pct": station["availability_pct"],
                    "setup_time_s": station.get("setup_time_s", 0),
                    "scrap_rate_pct": station.get("scrap_rate_pct", 0.0),
                },
            },
            {"id": "SNK", "kind": "sink"},
        ],
        "edges": [{"from": "SRC", "to": station["id"]}, {"from": station["id"], "to": "SNK"}],
        "quality": instance.get("quality", {}),
        "data": instance.get("data", {}),
    }
    return twin


def compute_kpis(twin: Dict[str, Any]) -> Dict[str, Any]:
    station_node = next(n for n in twin["nodes"] if n["kind"] == "station")
    p = station_node["params"]

    cycle = float(p["cycle_time_s"])
    availability = float(p["availability_pct"]) / 100.0
    scrap = float(p.get("scrap_rate_pct", 0.0)) / 100.0

    throughput_pph = (3600.0 / cycle) * availability * (1.0 - scrap)

    return {
        "throughput_pph": round(throughput_pph, 2),
        "cycle_time_s": cycle,
        "availability": availability,
        "scrap_rate": scrap,
        "bottleneck": station_node["id"],
    }


@app.get("/status")
def status():
    return {"status": "ok"}


@app.post("/generate-twin")
def generate_twin(payload: InstancePayload):
    twin = compile_twin(payload.instance)
    return {"twin": twin}


@app.post("/compute-kpi")
def compute_kpi(payload: InstancePayload):
    twin = compile_twin(payload.instance)
    kpis = compute_kpis(twin)
    return {"twin_id": twin["twin_id"], "kpis": kpis, "twin": twin}


# ============================================================
# 5) Simple UI (textarea JSON)
# ============================================================
@app.get("/ui", response_class=HTMLResponse)
def ui():
    sample = r'''{
  "instance": {
    "line": {
      "line_name": "Assembly_Cell_A",
      "shift_hours": 8,
      "target_throughput_pph": 180
    },
    "station": {
      "id": "S1",
      "type": "assembly",
      "cycle_time_s": 20,
      "availability_pct": 92,
      "setup_time_s": 5,
      "scrap_rate_pct": 1.5
    },
    "quality": {
      "inspection_enabled": true,
      "rework_enabled": false,
      "rework_cycle_time_s": 60
    },
    "data": {
      "mode": "simulation"
    }
  }
}'''
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>DTaaS – Simple UI</title>
  <style>
    body {{ font-family: system-ui, Arial; max-width: 980px; margin: 40px auto; padding: 0 16px; }}
    textarea {{ width: 100%; height: 260px; font-family: ui-monospace, Menlo, Consolas, monospace; }}
    button {{ padding: 10px 14px; cursor: pointer; }}
    pre {{ background: #f6f6f6; padding: 12px; overflow: auto; }}
    .row {{ display:flex; gap: 12px; align-items:center; flex-wrap: wrap; }}
  </style>
</head>
<body>
  <h2>DTaaS – Simple UI</h2>
  <p>Incolla (o modifica) la <b>instance</b> e premi <b>Compute KPI</b>.</p>

  <textarea id="payload">{sample}</textarea>

  <div class="row" style="margin-top:12px;">
    <button onclick="compute()">Compute KPI</button>
    <span id="status"></span>
  </div>

  <h3>Output</h3>
  <pre id="out">—</pre>

  <script>
    async function compute() {{
      const status = document.getElementById("status");
      const out = document.getElementById("out");
      status.textContent = "Running...";
      out.textContent = "—";

      let payload;
      try {{
        payload = JSON.parse(document.getElementById("payload").value);
      }} catch (e) {{
        status.textContent = "JSON non valido";
        out.textContent = String(e);
        return;
      }}

      try {{
        const res = await fetch("/compute-kpi", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify(payload)
        }});

        const data = await res.json();
        status.textContent = res.ok ? "OK" : ("Error " + res.status);
        out.textContent = JSON.stringify(data, null, 2);
      }} catch (e) {{
        status.textContent = "Request failed";
        out.textContent = String(e);
      }}
    }}
  </script>

  <p style="margin-top:20px;">
    Docs API: <a href="/docs">/docs</a>
  </p>
</body>
</html>
"""


# ============================================================
# 6) Templates (static library) + Template-based UI (single-station)
# ============================================================
TEMPLATES = {
    "single_station_v1": {
        "template_id": "single_station_v1",
        "name": "Single station (assembly/welding)",
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": ["line", "station", "quality", "data"],
            "properties": {
                "line": {
                    "type": "object",
                    "required": ["line_name", "shift_hours", "target_throughput_pph"],
                    "properties": {
                        "line_name": {"type": "string", "minLength": 2, "title": "Line name"},
                        "shift_hours": {"type": "number", "minimum": 0.5, "maximum": 24, "title": "Shift duration (hours)"},
                        "target_throughput_pph": {"type": "number", "minimum": 1, "title": "Target throughput (pcs/hour)"},
                    },
                },
                "station": {
                    "type": "object",
                    "title": "Station",
                    "required": ["id", "type", "cycle_time_s", "availability_pct"],
                    "properties": {
                        "id": {"type": "string", "default": "S1", "title": "Station ID"},
                        "type": {"type": "string", "enum": ["assembly", "welding"], "title": "Station type"},
                        "cycle_time_s": {"type": "number", "minimum": 1, "maximum": 600, "title": "Cycle time (s)"},
                        "availability_pct": {"type": "number", "minimum": 50, "maximum": 99.9, "title": "Availability (%)"},
                        "setup_time_s": {"type": "number", "minimum": 0, "maximum": 900, "default": 0, "title": "Setup/Changeover (s)"},
                        "scrap_rate_pct": {"type": "number", "minimum": 0, "maximum": 20, "default": 0.5, "title": "Waste (%)"},
                    },
                },
                "quality": {
                    "type": "object",
                    "required": ["inspection_enabled", "rework_enabled"],
                    "properties": {
                        "inspection_enabled": {"type": "boolean", "default": True, "title": "Inspection enabled"},
                        "rework_enabled": {"type": "boolean", "default": False, "title": "Rework active"},
                        "rework_cycle_time_s": {"type": "number", "minimum": 1, "maximum": 900, "default": 60, "title": "Rework cycle time (s)"},
                    },
                },
                "data": {
                    "type": "object",
                    "required": ["mode"],
                    "properties": {
                        "mode": {"type": "string", "enum": ["simulation", "realtime"], "default": "simulation", "title": "Mode"},
                        "opcua_endpoint": {"type": "string", "title": "OPC-UA endpoint (if realtime)"},
                        "mqtt_topic_prefix": {"type": "string", "title": "MQTT topic prefix (if realtime)"},
                    },
                },
            },
        },
    }
}


@app.get("/templates")
def list_templates():
    return [{"template_id": t["template_id"], "name": t["name"]} for t in TEMPLATES.values()]


@app.get("/templates/{template_id}/schema")
def get_template_schema(template_id: str):
    if template_id not in TEMPLATES:
        return {"error": "template not found"}
    return TEMPLATES[template_id]["schema"]


@app.get("/ui-template", response_class=HTMLResponse)
def ui_template():
    default_template = "single_station_v1"

    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>DTaaS – Template UI</title>
  <style>
    body {{ font-family: system-ui, Arial; max-width: 980px; margin: 40px auto; padding: 0 16px; }}
    .row {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; }}
    button {{ padding: 10px 14px; cursor: pointer; }}
    pre {{ background:#f6f6f6; padding:12px; overflow:auto; }}
    #editor_holder {{ margin-top: 16px; }}
  </style>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@json-editor/json-editor@latest/dist/css/jsoneditor.min.css">
</head>
<body>
  <h2>DTaaS – Template-based UI</h2>
  <p>Qui l’utente compila un form generato dal template (JSON Schema). Nessun JSON manuale.</p>

  <div class="row">
    <label for="template">Template:</label>
    <select id="template"></select>
    <button onclick="loadTemplate()">Load</button>
    <button onclick="compute()">Compute KPI</button>
    <span id="status"></span>
  </div>

  <div id="editor_holder"></div>

  <h3>Output</h3>
  <pre id="out">—</pre>

  <p style="margin-top:20px;">
    API Docs: <a href="/docs">/docs</a>
  </p>

  <script src="https://cdn.jsdelivr.net/npm/@json-editor/json-editor@latest/dist/jsoneditor.min.js"></script>
  <script>
    let editor = null;

    async function fetchJSON(url) {{
      const res = await fetch(url);
      return await res.json();
    }}

    async function initTemplates() {{
      const templates = await fetchJSON('/templates');
      const sel = document.getElementById('template');
      sel.innerHTML = '';
      templates.forEach(t => {{
        const opt = document.createElement('option');
        opt.value = t.template_id;
        opt.textContent = t.template_id + ' — ' + t.name;
        sel.appendChild(opt);
      }});
      sel.value = "{default_template}";
      await loadTemplate();
    }}

    async function loadTemplate() {{
      const status = document.getElementById('status');
      status.textContent = 'Loading template...';
      const templateId = document.getElementById('template').value;
      const schema = await fetchJSON(`/templates/${{templateId}}/schema`);

      if (schema.error) {{
        status.textContent = 'Template not found';
        return;
      }}

      if (editor) {{
        editor.destroy();
        editor = null;
      }}

      editor = new JSONEditor(document.getElementById('editor_holder'), {{
        schema: schema,
        disable_collapse: true,
        disable_properties: true,
        no_additional_properties: true,
        required_by_default: true
      }});

      status.textContent = 'Template loaded';
      document.getElementById('out').textContent = '—';
    }}

    async function compute() {{
      const status = document.getElementById('status');
      const out = document.getElementById('out');
      out.textContent = '—';

      if (!editor) {{
        status.textContent = 'No editor';
        return;
      }}

      const errors = editor.validate();
      if (errors.length) {{
        status.textContent = 'Fix validation errors';
        out.textContent = JSON.stringify(errors, null, 2);
        return;
      }}

      const instance = editor.getValue();
      status.textContent = 'Running...';

      try {{
        const res = await fetch('/compute-kpi', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ instance }})
        }});
        const data = await res.json();
        status.textContent = res.ok ? 'OK' : ('Error ' + res.status);
        out.textContent = JSON.stringify(data, null, 2);
      }} catch (e) {{
        status.textContent = 'Request failed';
        out.textContent = String(e);
      }}
    }}

    initTemplates();
  </script>
</body>
</html>
"""


# ============================================================
# 7) Root redirect (single source of truth)
# ============================================================
@app.get("/")
def root():
    return RedirectResponse("/ui-builder")
