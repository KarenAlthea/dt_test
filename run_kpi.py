import json
import os

base_dir = os.path.dirname(os.path.abspath(__file__))
twin_path = os.path.join(base_dir, "twin.json")

with open(twin_path, "r", encoding="utf-8") as f:
    twin = json.load(f)

# Prendo la stazione dal twin
station_node = next(n for n in twin["nodes"] if n["kind"] == "station")
p = station_node["params"]

cycle = float(p["cycle_time_s"])
availability = float(p["availability_pct"]) / 100.0
scrap = float(p.get("scrap_rate_pct", 0.0)) / 100.0

# KPI semplici ma sensati:
# throughput effettivo = (3600/cycle) * availability * (1 - scrap)
throughput_pph = (3600.0 / cycle) * availability * (1.0 - scrap)

print("=== KPI OUTPUT ===")
print("twin_id:", twin["twin_id"])
print("station_type:", station_node["station_type"])
print("cycle_time_s:", cycle)
print("availability:", availability)
print("scrap_rate:", scrap)
print("throughput_pph:", round(throughput_pph, 1))
