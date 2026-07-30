"""
PaisaBook iteration 2 backend tests.
Covers: Edit txn (PATCH), Recurring CRUD + run + toggle, Budgets CRUD + progress calc.
"""
import os
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # fallback to frontend .env
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")

API = f"{BASE_URL}/api"

DEMO_EMAIL = os.environ.get("DEMO_EMAIL", "demo@paisabook.com")
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "demo1234")


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(f"{API}/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    token = r.json().get("token")
    assert token
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s


@pytest.fixture(scope="module")
def account_id(session):
    r = session.get(f"{API}/accounts")
    assert r.status_code == 200
    accounts = r.json()
    if not accounts:
        # create one
        r = session.post(f"{API}/accounts", json={"name": "TEST_Acc", "type": "savings", "opening_balance": 1000})
        assert r.status_code == 200
        return r.json()["id"]
    return accounts[0]["id"]


# ---------- Edit Transaction ----------
class TestEditTransaction:
    def test_patch_transaction_updates_amount_and_note(self, session, account_id):
        # create a transaction
        r = session.post(f"{API}/transactions", json={
            "account_id": account_id, "type": "expense", "amount": 100.0,
            "category": "Food", "note": "TEST_original"
        })
        assert r.status_code == 200
        body = r.json()
        # v3: POST /transactions returns {transaction, budget_alerts}
        txn = body.get("transaction", body)
        txn_id = txn["id"]

        # patch
        r = session.patch(f"{API}/transactions/{txn_id}",
                          json={"amount": 250.5, "note": "TEST_updated"})
        assert r.status_code == 200
        assert r.json().get("ok") is True

        # verify persistence via GET list
        r = session.get(f"{API}/transactions")
        assert r.status_code == 200
        got = next((t for t in r.json() if t["id"] == txn_id), None)
        assert got is not None
        assert got["amount"] == 250.5
        assert got["note"] == "TEST_updated"

        # cleanup
        session.delete(f"{API}/transactions/{txn_id}")

    def test_patch_transaction_change_account_updates_name(self, session, account_id):
        # create 2nd account
        r = session.post(f"{API}/accounts",
                         json={"name": "TEST_Acc2", "type": "cash", "opening_balance": 0})
        assert r.status_code == 200
        acc2 = r.json()

        r = session.post(f"{API}/transactions", json={
            "account_id": account_id, "type": "expense", "amount": 50.0,
            "category": "Food", "note": "TEST_move"
        })
        body = r.json()
        txn_id = body.get("transaction", body)["id"]

        # patch account
        r = session.patch(f"{API}/transactions/{txn_id}", json={"account_id": acc2["id"]})
        assert r.status_code == 200

        r = session.get(f"{API}/transactions")
        got = next(t for t in r.json() if t["id"] == txn_id)
        assert got["account_id"] == acc2["id"]
        assert got["account_name"] == "TEST_Acc2"

        # cleanup
        session.delete(f"{API}/transactions/{txn_id}")
        session.delete(f"{API}/accounts/{acc2['id']}")


# ---------- Recurring ----------
class TestRecurring:
    created_ids = []

    def test_create_recurring_monthly(self, session, account_id):
        r = session.post(f"{API}/recurring", json={
            "account_id": account_id, "type": "expense", "amount": 500,
            "category": "Bills", "note": "TEST_recur",
            "frequency": "monthly", "day_of_month": 1,
            "start_date": "2026-01-05T00:00:00Z", "active": True,
        })
        assert r.status_code == 200, r.text
        doc = r.json()
        assert doc["frequency"] == "monthly"
        assert doc["active"] is True
        assert doc["amount"] == 500
        assert "next_due" in doc
        TestRecurring.created_ids.append(doc["id"])

    def test_list_recurring_contains_created(self, session):
        r = session.get(f"{API}/recurring")
        assert r.status_code == 200
        ids = {x["id"] for x in r.json()}
        assert TestRecurring.created_ids[0] in ids

    def test_run_recurring_returns_created_count(self, session):
        r = session.post(f"{API}/recurring/run")
        assert r.status_code == 200
        body = r.json()
        assert "created" in body
        assert isinstance(body["created"], int)
        assert body["created"] >= 1  # our start_date is in past → should create at least 1

    def test_auto_generated_txn_has_recurring_id(self, session):
        rec_id = TestRecurring.created_ids[0]
        r = session.get(f"{API}/transactions")
        assert r.status_code == 200
        matches = [t for t in r.json() if t.get("recurring_id") == rec_id]
        assert len(matches) >= 1, "no txn with recurring_id found after run"
        assert matches[0]["category"] == "Bills"
        assert matches[0]["amount"] == 500

    def test_toggle_active_off(self, session):
        rec_id = TestRecurring.created_ids[0]
        r = session.patch(f"{API}/recurring/{rec_id}", json={"active": False})
        assert r.status_code == 200
        r = session.get(f"{API}/recurring")
        rec = next(x for x in r.json() if x["id"] == rec_id)
        assert rec["active"] is False

    def test_toggle_active_on(self, session):
        rec_id = TestRecurring.created_ids[0]
        r = session.patch(f"{API}/recurring/{rec_id}", json={"active": True})
        assert r.status_code == 200
        r = session.get(f"{API}/recurring")
        rec = next(x for x in r.json() if x["id"] == rec_id)
        assert rec["active"] is True

    def test_delete_recurring(self, session):
        rec_id = TestRecurring.created_ids[0]
        r = session.delete(f"{API}/recurring/{rec_id}")
        assert r.status_code == 200
        r = session.get(f"{API}/recurring")
        assert rec_id not in {x["id"] for x in r.json()}

    def test_run_recurring_when_no_active(self, session):
        # After delete, if user has other active recurring might create. Just verify endpoint works.
        r = session.post(f"{API}/recurring/run")
        assert r.status_code == 200
        assert "created" in r.json()


# ---------- Budgets ----------
class TestBudgets:
    def test_create_budget(self, session):
        # Use a unique category to avoid clashing with seeded "Food" budget
        cat = "TEST_Travel"
        r = session.post(f"{API}/budgets", json={"category": cat, "amount": 5000})
        assert r.status_code == 200
        body = r.json()
        assert body.get("ok") is True
        assert "id" in body

        r = session.get(f"{API}/budgets")
        assert r.status_code == 200
        rows = r.json()
        b = next((x for x in rows if x["category"] == cat), None)
        assert b is not None
        assert b["amount"] == 5000
        assert "spent" in b and "remaining" in b and "percent" in b

    def test_upsert_same_category_updates(self, session):
        cat = "TEST_Travel"
        r = session.post(f"{API}/budgets", json={"category": cat, "amount": 7000})
        assert r.status_code == 200
        r = session.get(f"{API}/budgets")
        b = next(x for x in r.json() if x["category"] == cat)
        assert b["amount"] == 7000

    def test_budget_progress_calculation(self, session, account_id):
        # Create budget on unique cat (unique per run to avoid data pollution)
        cat = f"TEST_Progress_{uuid.uuid4().hex[:6]}"
        r = session.post(f"{API}/budgets", json={"category": cat, "amount": 1000})
        assert r.status_code == 200

        # Create an expense of 300 in this category (current month)
        r = session.post(f"{API}/transactions", json={
            "account_id": account_id, "type": "expense", "amount": 300,
            "category": cat, "note": "TEST_progress_txn"
        })
        assert r.status_code == 200
        txn_id = r.json().get("transaction", r.json())["id"]

        r = session.get(f"{API}/budgets")
        b = next(x for x in r.json() if x["category"] == cat)
        assert b["spent"] == 300.0
        assert b["remaining"] == 700.0
        assert b["percent"] == 30.0

        # cleanup
        session.delete(f"{API}/transactions/{txn_id}")
        # delete this budget
        session.delete(f"{API}/budgets/{b['id']}")

    def test_delete_budget(self, session):
        # find TEST_Travel and delete it
        r = session.get(f"{API}/budgets")
        b = next((x for x in r.json() if x["category"] == "TEST_Travel"), None)
        assert b is not None
        r = session.delete(f"{API}/budgets/{b['id']}")
        assert r.status_code == 200

        r = session.get(f"{API}/budgets")
        assert not any(x["category"] == "TEST_Travel" for x in r.json())


# ---------- Auth guard on new endpoints ----------
class TestAuthGuards:
    def test_recurring_requires_auth(self):
        r = requests.get(f"{API}/recurring")
        assert r.status_code == 401

    def test_budgets_requires_auth(self):
        r = requests.get(f"{API}/budgets")
        assert r.status_code == 401

    def test_recurring_run_requires_auth(self):
        r = requests.post(f"{API}/recurring/run")
        assert r.status_code == 401
