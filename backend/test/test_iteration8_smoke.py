"""
Iteration 8 smoke test — verify MONGO_URL defensive validation
does not break existing behavior on preview backend where MONGO_URL
is valid (mongodb://localhost:27017).

Scope (per review_request):
- Backend startup OK
- GET /api/ returns {app: 'Apka Munim', status: 'ok'}
- Login demo user works, returns token + sets access_token cookie
- GET /api/auth/me returns personal_ledger_id
- GET /api/accounts, /api/transactions, /api/analytics/summary all 200
"""
import os
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # fallback for local pytest runs
    with open("/app/frontend/.env") as f:
        for ln in f:
            if ln.startswith("REACT_APP_BACKEND_URL"):
                BASE_URL = ln.split("=", 1)[1].strip().strip('"').strip("'").rstrip("/")
                break

API = f"{BASE_URL}/api"

DEMO_EMAIL = "demo@paisabook.com"
DEMO_PASS = "demo1234"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def auth_session(session):
    r = session.post(f"{API}/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASS}, timeout=15)
    assert r.status_code == 200, f"Demo login failed: {r.status_code} {r.text}"
    data = r.json()
    token = data.get("token")
    assert token, "No token in login response"
    session.headers.update({"Authorization": f"Bearer {token}"})
    # cookie also should be set
    assert "access_token" in session.cookies or "access_token" in r.cookies, \
        "access_token cookie not set on login"
    return session


# --- Health / root ---
def test_root_health():
    r = requests.get(f"{API}/", timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert body.get("app") == "Apka Munim"
    assert body.get("status") == "ok"


# --- Auth ---
def test_login_demo_user(session):
    r = session.post(f"{API}/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASS}, timeout=15)
    assert r.status_code == 200, f"Body: {r.text}"
    d = r.json()
    assert "token" in d and isinstance(d["token"], str) and len(d["token"]) > 20
    assert d.get("email") == DEMO_EMAIL


def test_auth_me_returns_personal_ledger(auth_session):
    r = auth_session.get(f"{API}/auth/me", timeout=10)
    assert r.status_code == 200
    d = r.json()
    assert d.get("email") == DEMO_EMAIL
    assert d.get("personal_ledger_id"), "personal_ledger_id missing on /auth/me"
    assert d.get("current_ledger_id"), "current_ledger_id missing on /auth/me"


# --- Core endpoints smoke ---
def test_accounts_list(auth_session):
    r = auth_session.get(f"{API}/accounts", timeout=10)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_transactions_list(auth_session):
    r = auth_session.get(f"{API}/transactions", timeout=15)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_analytics_summary(auth_session):
    r = auth_session.get(f"{API}/analytics/summary", timeout=15)
    assert r.status_code == 200
    d = r.json()
    # required keys
    for k in ("total_income", "total_expense", "net_balance",
              "udhaar_lene", "udhaar_dene", "expense_by_category"):
        assert k in d, f"missing key {k} in analytics summary"
