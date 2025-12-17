from fastapi import FastAPI
from pydantic import BaseModel
from typing import Any, Dict

app = FastAPI(title="Digital Twin as a Service", version="0.1")


# ---- Helpers (uguali a quello che hai già fatto) ----

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
                    "scrap_rate_pct": station.get("scrap_rate_pct", 0.0)
                }
            },
            {"id": "SNK", "kind": "sink"}
        ],
        "edges": [
            {"from": "SRC", "to": station["id"]},
            {"from": station["id"], "to": "SNK"}
        ],
        "quality": instance.get("quality", {}),
        "data": instance.get("data", {})
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
        "bottleneck": station_node["id"]
    }


# ---- API (service) ----

class InstancePayload(BaseModel):
    instance: Dict[str, Any]


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

@app.get("/")
def root():
    return {"status": "ok", "service": "DTaaS", "docs": "/docs"}

from fastapi.responses import HTMLResponse

@app.get("/ui", response_class=HTMLResponse)
def ui():
    # JSON di esempio pre-caricato nella textarea
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


