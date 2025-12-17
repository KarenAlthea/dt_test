import json
import os

# cartella dove si trova questo file .py
base_dir = os.path.dirname(os.path.abspath(__file__))

# percorso completo di instance.json
json_path = os.path.join(base_dir, "instance.json")

with open(json_path, "r", encoding="utf-8") as f:
    instance = json.load(f)

station = instance["station"]

cycle = station["cycle_time_s"]
availability = station["availability_pct"] / 100.0
throughput_pph = (3600.0 / cycle) * availability

print("DIGITAL TWIN OUTPUT (from file)")
print("Line:", instance["line"]["line_name"])
print("Station type:", station["type"])
print("Theoretical throughput:", round(throughput_pph, 1), "pcs/hour")
