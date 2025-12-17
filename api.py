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


