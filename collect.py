"""Poll the public machine-status APIs of Laverie Richter and a competitor and log changes.

Usage: python collect.py [--polls N] [--interval SECONDS]

Files written (one per month, UTC):
  data/polls-YYYY-MM.csv            Laverie Richter (Wi-line): one line per successful poll
  data/changes-YYYY-MM.csv          one line per machine whose status or virtu_off changed
  data/last_state.json              state seen at the previous poll
  data/factory/...                  same three files for Laverie Factory Port Marianne (Touch'n Pay)
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SERIAL = os.environ.get("WILINE_SERIAL", "509ca15b5536523c8d573fe951b56709")
URL = "https://status.wi-line.fr/update_machine_ext.php"
TNP_UID = os.environ.get("TNP_UID", "u3v4a8kwl09g4xp3")  # Laverie Factory Port Marianne
TNP_URL = "https://api.touchnpay.fr/public/"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
FACTORY_DIR = os.path.join(DATA_DIR, "factory")
HEADERS = {"User-Agent": "laundry-monitor/1.0 (personal usage stats)"}
CHANGE_FIELDS = ["ts_utc", "machine_no", "type", "status", "temps", "time_before_off", "virtu_off"]
TNP_FIELDS = ["ts_utc", "machine_no", "name", "kind", "spec", "price_eur", "state", "in_service", "started_at", "duration_min"]


def fetch():
    body = urllib.parse.urlencode({"action": "READ_LIST_STATUS", "serial_centrale": SERIAL}).encode()
    req = urllib.request.Request(URL, data=body, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["machine_info_status"]["machine_list"]


def fetch_touchnpay():
    req = urllib.request.Request(TNP_URL + TNP_UID, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def append_csv(path, header, rows):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)


def log_poll(data_dir, ts, month, state, changes, change_fields):
    """Append the poll and its changes, then remember the state for the next poll."""
    append_csv(os.path.join(data_dir, f"polls-{month}.csv"), ["ts_utc", "n_machines"], [[ts, len(state)]])
    if changes:
        append_csv(os.path.join(data_dir, f"changes-{month}.csv"), change_fields, changes)
    with open(os.path.join(data_dir, "last_state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=0, sort_keys=True)


def load_state(data_dir):
    try:
        with open(os.path.join(data_dir, "last_state.json"), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def poll_richter(ts, month):
    try:
        machines = fetch()
    except Exception as e:  # network hiccup: skip this poll, don't fail the job
        print(f"richter fetch failed: {e}", file=sys.stderr)
        return
    last = load_state(DATA_DIR)
    state, changes = {}, []
    for m in machines:
        no = str(m["selecteur_machine"])
        virtu_off = ((m.get("date_virtu_off") or {}).get("date") or "")[:19]
        cur = {"status": m["status"], "virtu_off": virtu_off}
        state[no] = cur
        if last.get(no) != cur:
            changes.append([ts, no, m["nom_type"], m["status"], m["temps"], m["time_before_off"], virtu_off])
    log_poll(DATA_DIR, ts, month, state, changes, CHANGE_FIELDS)
    print(f"{ts}: richter {len(machines)} machines, {len(changes)} changes")


def poll_factory(ts, month):
    try:
        machines = fetch_touchnpay()
    except Exception as e:
        print(f"factory fetch failed: {e}", file=sys.stderr)
        return
    os.makedirs(FACTORY_DIR, exist_ok=True)
    last = load_state(FACTORY_DIR)
    state, changes = {}, []
    for m in machines:
        no = str(m["machine_nbr"])
        started_at = (m.get("started_at") or "")[:19]
        cur = {
            "state": m["machine_state"],
            "in_service": bool(m["machine_in_service"]),
            "started_at": started_at,
            "price": m["machine_price"],
        }
        state[no] = cur
        if last.get(no) != cur:
            changes.append([
                ts, no, m["machine_name"], m["machine_type"], m["special_val"], int(m["machine_price"]) / 100,
                m["machine_state"], int(bool(m["machine_in_service"])), started_at, m["duration_estimate"] or "",
            ])
    log_poll(FACTORY_DIR, ts, month, state, changes, TNP_FIELDS)
    print(f"{ts}: factory {len(machines)} machines, {len(changes)} changes")


def poll_once():
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    month = now.strftime("%Y-%m")
    poll_richter(ts, month)
    poll_factory(ts, month)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--polls", type=int, default=1, help="number of polls in this run")
    ap.add_argument("--interval", type=int, default=300, help="seconds between polls")
    args = ap.parse_args()
    os.makedirs(DATA_DIR, exist_ok=True)
    for i in range(args.polls):
        if i:
            time.sleep(args.interval)
        poll_once()


if __name__ == "__main__":
    main()
