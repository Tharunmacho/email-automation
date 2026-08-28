"""Every request the three new screens make, in the order they make it.

A screen that typechecks can still call an endpoint that does not exist, or read
a field the API does not send. This drives the exact calls the components issue
and checks the shapes they destructure.
"""
import json
import urllib.error
import urllib.request

BASE = "http://localhost:8000"
GREEN, RED, RESET = "\033[32m", "\033[31m", "\033[0m"
failures = 0


def check(ok, name, detail=""):
    global failures
    if not ok:
        failures += 1
    print(f"  {GREEN + 'ok  ' + RESET if ok else RED + 'FAIL' + RESET}  {name}  {detail}")


def call(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
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
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


status, body = call("POST", "/auth/login", {"email": "adira@gmail.com", "password": "adira@2026"})
token = body.get("token", "")
user = body.get("user", {})

print("\nthe rail after signing in")
check(bool(token), "signed in")
check("data-management" in user.get("pages", []), "Data Management is on the admin's rail")
check("users" in user.get("pages", []), "User Management is on the admin's rail")

print("\nDataManagementScreen — its three opening calls")
s1, jobs = call("GET", "/job-designations", token=token)
s2, countries = call("GET", "/countries", token=token)
s3, questions = call("GET", "/job-questions", token=token)
check(s1 == 200 and s2 == 200 and s3 == 200, "all three load", f"{s1}/{s2}/{s3}")

# The fields the table destructures.
job = (jobs.get("items") or [{}])[0]
for field in ("id", "title", "active", "bot_visible", "bot_order", "cv_required_default", "cv_overrides"):
    check(field in job, f"a job row carries {field}", repr(job.get(field))[:40])

country = (countries.get("items") or [{}])[0]
for field in ("id", "name", "active", "bot_visible"):
    check(field in country, f"a country row carries {field}", repr(country.get(field))[:30])

print("\nthe CV matrix the screen opens on a job")
s, matrix = call("GET", f"/job-designations/{job.get('id')}/cv-matrix", token=token)
row = (matrix.get("matrix") or [{}])[0]
check(s == 200, "the matrix loads", f"{len(matrix.get('matrix', []))} rows")
for field in ("country", "cv_required", "reason", "is_override"):
    check(field in row, f"a matrix row carries {field}", repr(row.get(field))[:40])

print("\nUserManagementScreen — its opening call")
s, users = call("GET", "/users", token=token)
check(s == 200, "users load", f"{len(users.get('items', []))} accounts")
check(isinstance(users.get("pages"), list) and users["pages"], "the page vocabulary comes with it", str(len(users.get("pages", []))) + " pages")
u = (users.get("items") or [{}])[0]
for field in ("id", "email", "name", "role", "active", "created_at", "page_grants", "pages"):
    check(field in u, f"a user row carries {field}", repr(u.get(field))[:40])

print("\nSourcingHub — agents are storable")
s, saved = call(
    "POST",
    "/sourcing-clients",
    {
        "id": "AGT-probe-1",
        "name": "ZZ Probe Agent",
        "type": "agent",
        "contact": "Ravi",
        "phone": "+910000000000",
        "email": "agent@example.invalid",
        "date": "2026-08-21",
        "status": "ACTIVE",
    },
    token=token,
)
check(s == 200, "an agent record is accepted", str(s))
s, listed = call("GET", "/sourcing-clients", token=token)
agent = next((r for r in listed.get("items", []) if r.get("id") == "AGT-probe-1"), None)
check(agent is not None, "and comes back in the list")
check((agent or {}).get("type") == "agent", "with its type intact", str((agent or {}).get("type")))
call("DELETE", "/sourcing-clients/AGT-probe-1", token=token)
print("  removed the probe agent")

print(f"\n{GREEN + 'all checks passed' + RESET if not failures else RED + str(failures) + ' failed' + RESET}\n")
raise SystemExit(1 if failures else 0)
