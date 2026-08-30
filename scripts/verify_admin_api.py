"""Drive the new admin API the way the screens will, and check what it does."""
import json
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://localhost:8000"
SERVICE_KEY = ""
for line in Path(r"D:\email-automation\.env").read_text(encoding="utf-8").splitlines():
    if line.startswith("WHATSAPP_SERVICE_KEY="):
        SERVICE_KEY = line.split("=", 1)[1].strip()

GREEN, RED, RESET = "\033[32m", "\033[31m", "\033[0m"
failures = 0


def check(ok, name, detail=""):
    global failures
    if not ok:
        failures += 1
    print(f"  {GREEN + 'ok  ' + RESET if ok else RED + 'FAIL' + RESET}  {name}  {detail}")


def call(method, path, body=None, token=None, service=False):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if service:
        headers["X-Service-Key"] = SERVICE_KEY
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, {"raw": raw.decode(errors="replace")[:200]}


print("\nsigning in")
status, body = call("POST", "/auth/login", {"email": "adira@gmail.com", "password": "adira@2026"})
token = body.get("token", "")
check(status == 200 and token, "admin login", str(status))
check("pages" in body.get("user", {}), "the session carries its page list", str(body.get("user", {}).get("pages", []))[:60] + "…")

print("\njob designations")
status, body = call("GET", "/job-designations", token=token)
check(status == 200 and len(body.get("items", [])) >= 12, "seeded jobs are listed", f"{len(body.get('items', []))} jobs")

# A brand-new job, exactly as the spec describes it.
status, body = call(
    "POST",
    "/job-designations",
    {
        "title": "CNC Operator",
        "cv_required_default": True,
        "cv_overrides": {"Malaysia": False},
        "bot_visible": True,
        "bot_order": 12,
    },
    token=token,
)
check(status == 200, "a new job is created", str(status))
job = body.get("item", {})
check(job.get("id") == "cnc_operator", "the id is derived from the title", str(job.get("id")))
new_job_id = job.get("id")

status, body = call("POST", "/job-designations", {"title": "CNC Operator"}, token=token)
check(status == 409, "creating it twice is refused", str(status))

print("\nthe CV rule the admin just wrote")
status, body = call("GET", f"/job-designations/{new_job_id}/cv-matrix", token=token)
matrix = {row["country"]: row for row in body.get("matrix", [])}
check(status == 200, "the resolved matrix is served", f"{len(matrix)} countries")
check(matrix.get("Malaysia", {}).get("cv_required") is False, "Malaysia: no CV (the override)", matrix.get("Malaysia", {}).get("reason", ""))
check(matrix.get("Singapore", {}).get("cv_required") is True, "Singapore: CV (the job default)", matrix.get("Singapore", {}).get("reason", ""))
check(matrix.get("Qatar", {}).get("cv_required") is True, "Qatar: CV (the job default)", matrix.get("Qatar", {}).get("reason", ""))

print("\nand the answer the bot gets for it")
status, body = call("GET", f"/policy/cv-required?destination_country=Malaysia&job_category={new_job_id}", service=True)
check(status == 200 and body.get("cv_required") is False, "policy endpoint agrees for Malaysia", json.dumps(body))
status, body = call("GET", f"/policy/cv-required?destination_country=Singapore&job_category={new_job_id}", service=True)
check(status == 200 and body.get("cv_required") is True, "policy endpoint agrees for Singapore", json.dumps(body))

print("\nexisting jobs keep the behaviour they had")
for country, job_id, expected in [
    ("Malaysia", "general_worker", False),
    ("Singapore", "general_worker", False),
    ("Malaysia", "technician", True),
    ("Qatar", "construction", True),
]:
    status, body = call("GET", f"/policy/cv-required?destination_country={country}&job_category={job_id}", service=True)
    check(body.get("cv_required") is expected, f"{country} + {job_id} = {expected}", str(body.get("cv_required")))

print("\ncountries")
status, body = call("POST", "/countries", {"name": "Kuwait", "bot_order": 6}, token=token)
check(status == 200, "a country can be added", str(body.get("item", {}).get("id")))

print("\nquestions about a job")
status, body = call(
    "POST",
    "/job-questions",
    {"job_id": new_job_id, "text": "Which controllers have you run — Fanuc, Siemens, Haas?", "kind": "text", "required": True, "order": 1},
    token=token,
)
check(status == 200, "a question is attached to the job", str(status))
question_id = body.get("item", {}).get("id")

status, body = call("POST", "/job-questions", {"job_id": "not_a_job", "text": "x"}, token=token)
check(status == 404, "a question for a job that does not exist is refused", str(status))

status, body = call("GET", f"/jobs/{new_job_id}/questions", service=True)
check(status == 200 and len(body.get("questions", [])) == 1, "the bot can read the job's questions", json.dumps(body.get("questions", []))[:80])

print("\nwhat the bot sees")
status, body = call("GET", "/taxonomy", service=True)
jobs = body.get("jobs", [])
countries = body.get("countries", [])
check(status == 200, "taxonomy served to the service key", f"{len(jobs)} jobs, {len(countries)} countries")
check(any(j["id"] == new_job_id for j in jobs), "the new job is in it", "cnc_operator present")
check(any(c["name"] == "Kuwait" for c in countries), "the new country is in it", "Kuwait present")
check(body.get("bot_list_limit") == 10, "the WhatsApp row limit is stated", str(body.get("bot_list_limit")))
status, _ = call("GET", "/taxonomy")
check(status == 401, "and it is not public", str(status))

print("\nuser management")
status, body = call("GET", "/users", token=token)
check(status == 200 and body.get("pages"), "users and the page vocabulary are listed", f"{len(body.get('items', []))} users, {len(body.get('pages', []))} pages")

status, body = call(
    "POST",
    "/users",
    {"email": "probe.user@example.com", "password": "probe-pass-123", "name": "Probe User", "role": "staff", "page_grants": ["job-orders", "sourcing"]},
    token=token,
)
check(status == 201, "a user is created", str(status))
probe = body.get("user", {})
probe_id = probe.get("id")
check(sorted(probe.get("pages", [])) == sorted(["my-queue", "job-orders", "sourcing"]), "role floor plus grants", str(probe.get("pages")))

status, body = call("PATCH", f"/users/{probe_id}", {"page_grants": ["candidates"]}, token=token)
check(sorted(body.get("user", {}).get("pages", [])) == sorted(["my-queue", "candidates"]), "grants can be changed", str(body.get("user", {}).get("pages")))
check(body.get("user", {}).get("name") == "Probe User", "an unrelated field is not blanked", body.get("user", {}).get("name", ""))

# The guard that matters.
status, body = call("GET", "/users", token=token)
admin_id = next((u["id"] for u in body["items"] if u["role"] == "admin" and u["active"]), None)
status, body = call("PATCH", f"/users/{admin_id}", {"role": "staff"}, token=token)
check(status == 409, "the last admin cannot demote themselves", str(status))

print("\ncleanup")
call("DELETE", f"/job-questions/{question_id}", token=token)
call("DELETE", f"/job-designations/{new_job_id}", token=token)
call("DELETE", "/countries/kuwait", token=token)
from pymongo import MongoClient

c = MongoClient("mongodb://localhost:27017")
c["resume_ats"]["users"].delete_one({"email": "probe.user@example.com"})
c["resume_ats"]["job_designations"].delete_one({"_id": "cnc_operator"})
c["resume_ats"]["countries"].delete_one({"_id": "kuwait"})
print("  removed the probe job, country, question and user")

print(f"\n{GREEN + 'all checks passed' + RESET if not failures else RED + str(failures) + ' failed' + RESET}\n")
raise SystemExit(1 if failures else 0)
