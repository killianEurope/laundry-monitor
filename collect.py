"""Poll the Wi-line public status API once and log changes.

Files written (one per month, UTC):
  data/polls-YYYY-MM.csv    one line per successful poll (coverage log)
  data/changes-YYYY-MM.csv  one line per machine whose status or virtu_off changed
  data/last_state.json      state seen at the previous poll
"""
import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SERIAL = os.environ.get("WILINE_SERIAL", "509ca15b5536523c8d573fe951b56709")
URL = "https://status.wi-line.fr/update_machine_ext.php"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
STATE_FILE = os.path.join(DATA_DIR, "last_state.json")
CHANGE_FIELDS = ["ts_utc", "machine_no", "type", "status", "temps", "time_before_off", "virtu_off"]


def fetch():
    body = urllib.parse.urlencode({"action": "READ_LIST_STATUS", "serial_centrale": SERIAL}).encode()
    req = urllib.request.Request(URL, data=body, headers={"User-Agent": "laundry-monitor/1.0 (personal usage stats)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["machine_info_status"]["machine_list"]


def append_csv(path, header, rows):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        machines = fetch()
    except Exception as e:  # network hiccup: skip this poll, don't fail the job
        print(f"fetch failed: {e}", file=sys.stderr)
        return

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    month = now.strftime("%Y-%m")

    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            last = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        last = {}

    state, changes = {}, []
    for m in machines:
        no = str(m["selecteur_machine"])
        virtu_off = ((m.get("date_virtu_off") or {}).get("date") or "")[:19]
        cur = {"status": m["status"], "virtu_off": virtu_off}
        state[no] = cur
        if last.get(no) != cur:
            changes.append([ts, no, m["nom_type"], m["status"], m["temps"], m["time_before_off"], virtu_off])

    append_csv(os.path.join(DATA_DIR, f"polls-{month}.csv"), ["ts_utc", "n_machines"], [[ts, len(machines)]])
    if changes:
        append_csv(os.path.join(DATA_DIR, f"changes-{month}.csv"), CHANGE_FIELDS, changes)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=0, sort_keys=True)
    print(f"{ts}: {len(machines)} machines, {len(changes)} changes")


if __name__ == "__main__":
    main()
