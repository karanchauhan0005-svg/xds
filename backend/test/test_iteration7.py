"""
Iteration 7 backend tests for Apka Munim:
- SMS parser (/api/sms/parse) — 8 scenarios (a–h)
- Data Export (/api/auth/me/export)
- Account Delete (/api/auth/me DELETE) via a throwaway user
- Auth-required checks on new endpoints
Runs serially; demo user is NOT deleted.
"""
import os
import uuid
import pytest
import requests

# ---- Base URL ----
BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
API = f"{BASE_URL}/api"

DEMO_EMAIL = "demo@paisabook.com"
DEMO_PASSWORD = "demo1234"


def _login(email, password):
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    s.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
    return s


def _register(email, password, name="Throwaway"):
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(f"{API}/auth/register",
               json={"email": email, "password": password, "name": name, "currency": "INR"})
    assert r.status_code == 200, f"register: {r.status_code} {r.text}"
    s.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
    return s, r.json()


# ---- Fixtures ----
@pytest.fixture(scope="module")
def demo():
    s = _login(DEMO_EMAIL, DEMO_PASSWORD)
    # ensure on personal ledger before iteration7 tests run
    me = s.get(f"{API}/auth/me").json()
    if me["current_ledger_id"] != me["personal_ledger_id"]:
        s.post(f"{API}/ledgers/{me['personal_ledger_id']}/switch")
    return s


# ============================================================
# 1) SMS PARSER — /api/sms/parse
# ============================================================
class TestSmsParser:
    """8 scenarios from the review request (a–h)."""

    def test_a_zomato_hdfc_debit(self, demo):
        text = "Rs.499.00 debited from HDFC Bank A/c XX1234 on 22-Feb-26 UPI/Zomato/Order#8823"
        r = demo.post(f"{API}/sms/parse", json={"text": text})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["type"] == "expense", d
        assert d["amount"] == 499.0, d
        assert d["merchant"].lower() == "zomato", d
        assert d["account_last4"] == "1234", d
        assert d["category"] == "Food", d
        assert d["confidence"] >= 0.85, d

    def test_b_uber_icici(self, demo):
        text = "Rs.245 paid to Uber via UPI from ICICI XX9012"
        r = demo.post(f"{API}/sms/parse", json={"text": text})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["type"] == "expense"
        assert d["amount"] == 245.0
        assert "uber" in d["merchant"].lower()
        assert d["category"] == "Transport"

    def test_c_salary_credit(self, demo):
        text = "INR 85,000.00 credited to A/c XX5678 towards SALARY"
        r = demo.post(f"{API}/sms/parse", json={"text": text})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["type"] == "income"
        assert d["amount"] == 85000.0
        assert d["category"] == "Salary"

    def test_d_swiggy_instamart_amount_1200_not_120(self, demo):
        text = "Rs.1200 debited from HDFC A/c XX1234 UPI/Swiggy Instamart"
        r = demo.post(f"{API}/sms/parse", json={"text": text})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["amount"] == 1200.0, f"amount misread as {d['amount']}"
        assert d["category"] == "Food"

    def test_e_amazon_axis(self, demo):
        text = "Rs.2,499 paid to Amazon via UPI from Axis XX3344"
        r = demo.post(f"{API}/sms/parse", json={"text": text})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["amount"] == 2499.0
        assert "amazon" in d["merchant"].lower()
        assert d["category"] == "Shopping"

    def test_f_empty_text_400(self, demo):
        r = demo.post(f"{API}/sms/parse", json={"text": ""})
        assert r.status_code == 400
        r2 = demo.post(f"{API}/sms/parse", json={"text": "   "})
        assert r2.status_code == 400

    def test_g_netflix_entertainment(self, demo):
        text = "Netflix Rs.649 debited from HDFC XX1234"
        r = demo.post(f"{API}/sms/parse", json={"text": text})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["category"] == "Entertainment"

    def test_h_unauthenticated_returns_401(self):
        r = requests.post(f"{API}/sms/parse", json={"text": "Rs.100 debited"})
        assert r.status_code == 401


# ============================================================
# 2) DATA EXPORT — /api/auth/me/export
# ============================================================
class TestExportMyData:
    def test_export_requires_auth(self):
        r = requests.get(f"{API}/auth/me/export")
        assert r.status_code == 401

    def test_export_shape_and_keys(self, demo):
        r = demo.get(f"{API}/auth/me/export")
        assert r.status_code == 200, r.text
        d = r.json()
        for k in ("exported_at", "user", "ledgers", "accounts",
                  "transactions", "udhaar", "recurring", "budgets"):
            assert k in d, f"missing top-level key: {k}"
        assert isinstance(d["ledgers"], list)
        assert isinstance(d["accounts"], list)
        assert isinstance(d["transactions"], list)
        # demo has known data — sanity checks
        assert d["user"]["email"] == DEMO_EMAIL
        assert len(d["ledgers"]) >= 1  # at least personal
        assert len(d["accounts"]) >= 1
        assert len(d["transactions"]) >= 1
        # Ensure no MongoDB _id leaks
        for coll in ("ledgers", "accounts", "transactions",
                     "udhaar", "recurring", "budgets"):
            for row in d[coll]:
                assert "_id" not in row, f"{coll} contains mongo _id"

    def test_export_isolates_to_current_user(self, demo):
        """Verify all returned data belongs to this user or their ledgers."""
        me = demo.get(f"{API}/auth/me").json()
        uid = me["id"]
        r = demo.get(f"{API}/auth/me/export")
        d = r.json()
        my_ledger_ids = {l["id"] for l in d["ledgers"]}
        for row in d["accounts"] + d["transactions"] + d["udhaar"] + d["recurring"] + d["budgets"]:
            # Must belong via user_id OR owner_id in my ledgers
            if "owner_id" in row:
                assert (row.get("owner_id") in my_ledger_ids) or (row.get("user_id") == uid), \
                    f"data isolation breach: {row}"


# ============================================================
# 3) ACCOUNT DELETE — /api/auth/me DELETE  (throwaway user)
# ============================================================
class TestDeleteMyAccount:
    def test_delete_requires_auth(self):
        r = requests.delete(f"{API}/auth/me")
        assert r.status_code == 401

    def test_full_delete_flow(self):
        # 1. Register throwaway user
        email = f"delete-test-{uuid.uuid4().hex[:6]}@paisabook.com"
        pwd = "test1234"
        s, reg = _register(email, pwd, name="ToDelete")
        uid = reg["id"]

        # 2. Add data across scopes: account, txn, udhaar, budget, recurring
        acc = s.post(f"{API}/accounts", json={
            "name": "TEST_DelAcc", "type": "cash", "opening_balance": 1000
        }).json()
        txn = s.post(f"{API}/transactions", json={
            "account_id": acc["id"], "type": "expense", "amount": 50,
            "category": "TEST_DelCat", "note": "TEST_del"
        }).json()
        assert "transaction" in txn
        s.post(f"{API}/udhaar", json={
            "person_name": "TEST_Ram", "type": "lene", "amount": 100
        })
        s.post(f"{API}/budgets", json={"category": "TEST_DelCat", "amount": 500})
        s.post(f"{API}/recurring", json={
            "account_id": acc["id"], "type": "expense", "amount": 10,
            "category": "TEST_DelCat", "frequency": "monthly"
        })

        # 3. Also create a shared ledger owned solely by this user
        shared = s.post(f"{API}/ledgers", json={"name": "TEST_SoloShared"}).json()
        shared_id = shared["id"]
        # Switch to shared and add a transaction there (verify sole-owner-shared cascade)
        s.post(f"{API}/ledgers/{shared_id}/switch")
        shared_acc = s.post(f"{API}/accounts", json={
            "name": "TEST_SharedAcc", "type": "cash", "opening_balance": 0
        }).json()
        # Switch back to personal before deletion
        me = s.get(f"{API}/auth/me").json()
        s.post(f"{API}/ledgers/{me['personal_ledger_id']}/switch")

        # 4. DELETE /auth/me
        r = s.delete(f"{API}/auth/me")
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True

        # 5. Subsequent authenticated call should 401 (token still in header
        #    but user is gone) — server should reject.
        r_me = s.get(f"{API}/auth/me")
        assert r_me.status_code == 401, f"after delete /auth/me should 401, got {r_me.status_code}"

        # 6. Cannot log in with same creds anymore
        r_login = requests.post(f"{API}/auth/login",
                                json={"email": email, "password": pwd})
        assert r_login.status_code == 401

        # 7. Verify sole-owner shared ledger + its account cleaned up.
        #    We re-login as demo and try to join by the (now stale) code — should 404.
        stale_code = shared.get("invite_code")
        demo_s = _login(DEMO_EMAIL, DEMO_PASSWORD)
        r_join = demo_s.post(f"{API}/ledgers/join", json={"invite_code": stale_code})
        assert r_join.status_code == 404, \
            f"stale ledger should be deleted; join code returned {r_join.status_code}"


# ============================================================
# 4) LEGAL PAGES (public) — verified via frontend route or backend absence
# ============================================================
class TestLegalRoutes:
    """/privacy and /terms are frontend routes; verified via frontend HTML."""

    def test_frontend_privacy_route_reachable(self):
        # Frontend SPA — index.html served at any path
        r = requests.get(f"{BASE_URL}/privacy", timeout=15)
        assert r.status_code == 200
        # SPA shell — look for the React root div
        assert "<div id=\"root\">" in r.text or "<div id='root'>" in r.text

    def test_frontend_terms_route_reachable(self):
        r = requests.get(f"{BASE_URL}/terms", timeout=15)
        assert r.status_code == 200
        assert "<div id=\"root\">" in r.text or "<div id='root'>" in r.text

    def test_manifest_has_sms_shortcut_and_categories(self):
        r = requests.get(f"{BASE_URL}/manifest.json", timeout=15)
        assert r.status_code == 200
        m = r.json()
        assert "manual entry only" in (m.get("description") or "").lower()
        names = [s.get("name", "") for s in m.get("shortcuts", [])]
        assert any("SMS" in n for n in names), f"no SMS shortcut in {names}"
        assert len(m.get("shortcuts", [])) == 4
        cats = m.get("categories", [])
        assert "finance" in cats and "productivity" in cats


# ============================================================
# 5) REGRESSION — quick sanity of core endpoints still working
# ============================================================
class TestRegressionQuick:
    def test_auth_me(self, demo):
        r = demo.get(f"{API}/auth/me")
        assert r.status_code == 200
        assert r.json()["email"] == DEMO_EMAIL

    def test_transactions_list(self, demo):
        r = demo.get(f"{API}/transactions")
        assert r.status_code == 200

    def test_analytics_summary(self, demo):
        r = demo.get(f"{API}/analytics/summary")
        assert r.status_code == 200
