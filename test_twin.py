import json

instance = {
  "line": {"line_name":"Cell_1","shift_hours":8,"target_throughput_pph":200},
  "station":{"id":"S1","type":"assembly","cycle_time_s":18,"availability_pct":95,"setup_time_s":5,"scrap_rate_pct":0.8},
  "quality":{"inspection_enabled":True,"rework_enabled":False,"rework_cycle_time_s":60},
  "data":{"mode":"simulation"}
}

station = instance["station"]

cycle = station["cycle_time_s"]
availability = station["availability_pct"] / 100

throughput_pph = (3600 / cycle) * availability

print("DIGITAL TWIN OUTPUT")
print("Station type:", station["type"])
print("Theoretical throughput:", round(throughput_pph, 1), "pcs/hour")
