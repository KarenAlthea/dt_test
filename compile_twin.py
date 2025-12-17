import json
import os

base_dir = os.path.dirname(os.path.abspath(__file__))
instance_path = os.path.join(base_dir, "instance.json")
twin_path = os.path.join(base_dir, "twin.json")

with open(instance_path, "r", encoding="utf-8") as f:
    instance = json.load(f)

station = instance["station"]

# Compilazione: instance -> twin graph minimale
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

with open(twin_path, "w", encoding="utf-8") as f:
    json.dump(twin, f, indent=2)

print("OK: generated twin.json at")
print(twin_path)
