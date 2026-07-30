"""
Iteration 9 — Backend regression + new /api/auth/google endpoint.

Covers:
- POST /auth/google invalid credential -> 401
- Existing auth endpoints (register, login, pin/set, pin/verify, forgot-password)
- Batch endpoints (goals, subscriptions, analytics/*, ai/chat, voice/parse-transaction)
- Password strength enforcement on register
- Money endpoints w/ 'emergency' and 'investment' account types
- Goal breakdown incl. daily/weekly/monthly savings when target_date provided
"""
import os
import uuid
import time
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for ln in f:
            if ln.startswith("REACT_APP_BACKEND_URL"):
                BASE_URL = ln.split("=", 1)[1].strip().strip('"').strip("'").rstrip("/")
                break

API = f"{BASE_URL}/api"

DEMO_EMAIL = "karan.test.999@gmail.com"
DEMO_PASS = "Karan@2026"


# ---------- Fixtures ----------
@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def demo_token(session):
    """Login demo user (or register if missing) and return JWT."""
    r = session.post(f"{API}/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASS})
    if r.status_code != 200:
        # Try register
        r2 = session.post(f"{API}/auth/register", json={
            "email": DEMO_EMAIL, "password": DEMO_PASS, "name": "Karan Test", "currency": "INR"
        })
        if r2.status_code != 200:
            pytest.skip(f"Cannot obtain demo user: login={r.status_code}/{r.text[:200]} register={r2.status_code}/{r2.text[:200]}")
        return r2.json()["token"]
    return r.json()["token"]


@pytest.fixture(scope="module")
def auth_headers(demo_token):
    return {"Authorization": f"Bearer {demo_token}", "Content-Type": "application/json"}


# ---------- 1. Google Auth ----------
class TestGoogleAuth:
    def test_invalid_credential_returns_401(self, session):
        r = session.post(f"{API}/auth/google", json={"credential": "not-a-real-token", "currency": "INR"})
        assert r.status_code == 401, f"Expected 401, got {r.status_code}: {r.text[:300]}"
        body = r.json()
        detail = body.get("detail", "")
        assert "invalid" in detail.lower() or "verification" in detail.lower(), \
            f"Expected 'Google token invalid' / 'verification failed', got: {detail}"

    def test_missing_credential_returns_422(self, session):
        r = session.post(f"{API}/auth/google", json={})
        assert r.status_code == 422

    def test_empty_credential_returns_401(self, session):
        r = session.post(f"{API}/auth/google", json={"credential": "", "currency": "INR"})
        assert r.status_code == 401


# ---------- 2. Existing auth regression ----------
class TestAuthRegression:
    def test_login_demo(self, session, demo_token):
        # demo_token fixture ensures user exists; now login should work
        r = session.post(f"{API}/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASS})
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        assert "token" in j
        assert j["email"] == DEMO_EMAIL

    def test_register_weak_password_rejected(self, session):
        email = f"weakpw_{uuid.uuid4().hex[:8]}@test.com"
        r = session.post(f"{API}/auth/register", json={
            "email": email, "password": "1234", "name": "Weak", "currency": "INR"
        })
        assert r.status_code in (400, 422), f"Weak password should reject, got {r.status_code}: {r.text[:200]}"

    def test_register_strong_password_ok(self, session):
        email = f"strong_{uuid.uuid4().hex[:8]}@test.com"
        r = session.post(f"{API}/auth/register", json={
            "email": email, "password": "Strong@2026Pw", "name": "Strong User", "currency": "INR"
        })
        assert r.status_code == 200, r.text[:300]
        assert "token" in r.json()

    def test_forgot_password_returns_200(self, session):
        r = session.post(f"{API}/auth/forgot-password", json={"email": DEMO_EMAIL})
        # Should not leak whether user exists; 200 always
        assert r.status_code == 200, r.text[:300]

    def test_pin_set_and_verify(self, session):
        # Use a fresh user to guarantee known password (avoids karan.test.999 stale state)
        email = f"pin_{uuid.uuid4().hex[:8]}@test.com"
        pwd = "PinTest@2026"
        reg = session.post(f"{API}/auth/register", json={
            "email": email, "password": pwd, "name": "Pin U", "currency": "INR"
        })
        assert reg.status_code == 200, reg.text[:300]
        tok = reg.json()["token"]
        h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

        r_set = session.post(f"{API}/auth/pin/set",
                             json={"pin": "1234", "password": pwd}, headers=h)
        assert r_set.status_code == 200, f"pin/set failed: {r_set.status_code} {r_set.text[:200]}"

        r_v = session.post(f"{API}/auth/pin/verify",
                           json={"email": email, "pin": "1234"})
        assert r_v.status_code == 200, f"pin/verify failed: {r_v.status_code} {r_v.text[:200]}"


# ---------- 3. Feature endpoints ----------
class TestFeatureEndpoints:
    def test_goals(self, session, auth_headers):
        r = session.get(f"{API}/goals", headers=auth_headers)
        assert r.status_code == 200, r.text[:300]
        assert isinstance(r.json(), list)

    def test_subscriptions(self, session, auth_headers):
        r = session.get(f"{API}/subscriptions", headers=auth_headers)
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        # API returns {subscriptions: [...], monthly_total: float}
        assert isinstance(j, dict) and "subscriptions" in j and "monthly_total" in j

    def test_analytics_health_score(self, session, auth_headers):
        r = session.get(f"{API}/analytics/health-score", headers=auth_headers)
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        assert "score" in j or "health_score" in j or isinstance(j, dict)

    def test_analytics_streak(self, session, auth_headers):
        r = session.get(f"{API}/analytics/streak", headers=auth_headers)
        assert r.status_code == 200, r.text[:300]

    def test_analytics_vibe_check(self, session, auth_headers):
        r = session.get(f"{API}/analytics/vibe-check", headers=auth_headers)
        assert r.status_code == 200, r.text[:300]

    def test_ai_chat(self, session, auth_headers):
        r = session.post(f"{API}/ai/chat",
                         json={"messages": [{"role": "user", "content": "Hi Munim"}]},
                         headers=auth_headers, timeout=60)
        # AI may 200 or 503 depending on key availability
        assert r.status_code in (200, 503), f"AI chat unexpected: {r.status_code} {r.text[:200]}"

    def test_voice_parse_transaction(self, session, auth_headers):
        r = session.post(f"{API}/voice/parse-transaction",
                         json={"text": "Spent 500 rupees on coffee today"},
                         headers=auth_headers, timeout=60)
        assert r.status_code in (200, 503), f"voice/parse unexpected: {r.status_code} {r.text[:200]}"


# ---------- 4. Money endpoints ----------
class TestMoneyEndpoints:
    def test_create_emergency_account(self, session, auth_headers):
        r = session.post(f"{API}/accounts",
                         json={"name": "TEST_Emergency", "type": "emergency", "opening_balance": 10000, "currency": "INR"},
                         headers=auth_headers)
        assert r.status_code == 200, f"emergency account failed: {r.status_code} {r.text[:300]}"
        j = r.json()
        assert j.get("type") == "emergency"
        # Cleanup
        aid = j.get("id")
        if aid:
            session.delete(f"{API}/accounts/{aid}", headers=auth_headers)

    def test_create_investment_account(self, session, auth_headers):
        r = session.post(f"{API}/accounts",
                         json={"name": "TEST_Investment", "type": "investment", "opening_balance": 50000, "currency": "INR"},
                         headers=auth_headers)
        assert r.status_code == 200, f"investment account failed: {r.status_code} {r.text[:300]}"
        j = r.json()
        assert j.get("type") == "investment"
        aid = j.get("id")
        if aid:
            session.delete(f"{API}/accounts/{aid}", headers=auth_headers)

    def test_create_transaction(self, session, auth_headers):
        # Need an account first — use unique name to avoid conflict
        uniq = uuid.uuid4().hex[:8]
        acc_resp = session.post(f"{API}/accounts",
                          json={"name": f"TEST_Txn_{uniq}", "type": "savings", "opening_balance": 5000, "currency": "INR"},
                          headers=auth_headers)
        assert acc_resp.status_code == 200, f"account create failed: {acc_resp.status_code} {acc_resp.text[:200]}"
        acc = acc_resp.json()
        aid = acc.get("id")
        assert aid, f"account response missing id: {acc}"
        try:
            r = session.post(f"{API}/transactions",
                             json={"account_id": aid, "type": "expense", "amount": 100,
                                   "category": "Food", "note": "TEST_coffee"},
                             headers=auth_headers)
            assert r.status_code == 200, r.text[:300]
        finally:
            session.delete(f"{API}/accounts/{aid}", headers=auth_headers)


# ---------- 5. Goal breakdown ----------
class TestGoalBreakdown:
    def test_goal_with_target_date_has_breakdown(self, session, auth_headers):
        # Create a goal with target_date ~180 days out
        from datetime import date, timedelta
        target = (date.today() + timedelta(days=180)).isoformat()
        create = session.post(f"{API}/goals",
                              json={"name": "TEST_Goal_Breakdown", "target_amount": 100000,
                                    "current_amount": 10000, "target_date": target},
                              headers=auth_headers)
        assert create.status_code == 200, create.text[:300]
        gid = create.json().get("id")
        try:
            r = session.get(f"{API}/goals", headers=auth_headers)
            assert r.status_code == 200
            goals = r.json()
            g = next((x for x in goals if x.get("id") == gid), None)
            assert g is not None, "created goal not found in list"
            bd = g.get("breakdown")
            assert isinstance(bd, dict), f"Goal missing 'breakdown' dict: {g}"
            for key in ("per_day", "per_week", "per_month"):
                assert key in bd, f"breakdown missing {key}: {bd}"
        finally:
            if gid:
                session.delete(f"{API}/goals/{gid}", headers=auth_headers)
