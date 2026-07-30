"""
PaisaBook iteration 5 backend tests (v3 features):
- /auth/me returns personal_ledger_id and current_ledger_id
- Legacy data backfill (demo user still has accounts/txns)
- Family/Shared Ledger: create, join, switch, isolation, leave rules
- CSV & PDF Monthly Export
- Budget-breach alerts embedded in POST /transactions response
"""
import os
import re
import uuid
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")

API = f"{BASE_URL}/api"

DEMO_EMAIL = os.environ.get("DEMO_EMAIL", "demo@paisabook.com")
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "demo1234")

FAMILY2_EMAIL = f"family2+{uuid.uuid4().hex[:6]}@paisabook.com"
FAMILY2_PASSWORD = "family1234"


def _auth_session(email, password, register=False, name="Family Two"):
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    if register:
        r = s.post(f"{API}/auth/register",
                   json={"email": email, "password": password, "name": name, "currency": "INR"})
        assert r.status_code == 200, f"register: {r.status_code} {r.text}"
        tok = r.json()["token"]
    else:
        r = s.post(f"{API}/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, f"login: {r.status_code} {r.text}"
        tok = r.json()["token"]
    s.headers.update({"Authorization": f"Bearer {tok}"})
    return s


# ---------- Demo user session ----------
@pytest.fixture(scope="module")
def demo():
    return _auth_session(DEMO_EMAIL, DEMO_PASSWORD)


@pytest.fixture(scope="module")
def demo_me(demo):
    r = demo.get(f"{API}/auth/me")
    assert r.status_code == 200, r.text
    return r.json()


# ---------- Second user (fresh) ----------
@pytest.fixture(scope="module")
def user2():
    return _auth_session(FAMILY2_EMAIL, FAMILY2_PASSWORD, register=True)


# ---------- 1) /auth/me shape + backfill ----------
class TestAuthMeShape:
    def test_me_has_ledger_fields(self, demo_me):
        assert "personal_ledger_id" in demo_me
        assert "current_ledger_id" in demo_me
        assert demo_me["personal_ledger_id"], "personal_ledger_id empty"
        assert demo_me["current_ledger_id"], "current_ledger_id empty"

    def test_me_current_ledger_object(self, demo_me):
        cl = demo_me.get("current_ledger")
        assert isinstance(cl, dict)
        assert cl.get("id") == demo_me["current_ledger_id"]
        assert cl.get("type") in ("personal", "shared")

    def test_backfilled_accounts_visible(self, demo):
        # Ensure demo is on personal ledger (test isolation from earlier tests)
        me = demo.get(f"{API}/auth/me").json()
        if me["current_ledger_id"] != me["personal_ledger_id"]:
            demo.post(f"{API}/ledgers/{me['personal_ledger_id']}/switch")
        r = demo.get(f"{API}/accounts")
        assert r.status_code == 200
        assert len(r.json()) > 0, "demo should have backfilled accounts"

    def test_backfilled_transactions_visible(self, demo):
        r = demo.get(f"{API}/transactions")
        assert r.status_code == 200
        # demo has been used across many test runs — should have txns
        assert len(r.json()) > 0, "demo should have backfilled transactions"


# ---------- 2) Ledgers create/join/switch/isolation/leave ----------
class TestLedgers:
    shared_id = None
    invite_code = None

    def test_list_ledgers_has_personal(self, demo):
        r = demo.get(f"{API}/ledgers")
        assert r.status_code == 200
        rows = r.json()
        personals = [x for x in rows if x["type"] == "personal"]
        assert len(personals) >= 1
        assert personals[0].get("is_owner") is True
        assert isinstance(personals[0].get("members_detail"), list)

    def test_create_shared_ledger(self, demo):
        r = demo.post(f"{API}/ledgers", json={"name": "TEST_Family"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["type"] == "shared"
        assert d["name"] == "TEST_Family"
        assert isinstance(d.get("invite_code"), str)
        assert re.fullmatch(r"[A-Z0-9]{6}", d["invite_code"]), f"invite_code shape wrong: {d['invite_code']}"
        assert d.get("owner_user_id")
        assert len(d.get("members", [])) == 1
        assert d.get("is_owner") is True
        TestLedgers.shared_id = d["id"]
        TestLedgers.invite_code = d["invite_code"]

    def test_user2_join_ledger(self, user2):
        assert TestLedgers.invite_code
        r = user2.post(f"{API}/ledgers/join", json={"invite_code": TestLedgers.invite_code})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["id"] == TestLedgers.shared_id
        assert len(d["members"]) == 2

    def test_user2_join_invalid_code_404(self, user2):
        r = user2.post(f"{API}/ledgers/join", json={"invite_code": "XXXXXX"})
        assert r.status_code == 404

    def test_user2_join_idempotent(self, user2):
        # Joining the same ledger twice should be a no-op success
        r = user2.post(f"{API}/ledgers/join", json={"invite_code": TestLedgers.invite_code})
        assert r.status_code == 200

    def test_switch_to_shared(self, demo):
        r = demo.post(f"{API}/ledgers/{TestLedgers.shared_id}/switch")
        assert r.status_code == 200
        assert r.json()["current_ledger_id"] == TestLedgers.shared_id
        me = demo.get(f"{API}/auth/me").json()
        assert me["current_ledger_id"] == TestLedgers.shared_id

    def test_switch_ledger_404_for_non_member(self, demo):
        r = demo.post(f"{API}/ledgers/does-not-exist/switch")
        assert r.status_code == 404

    def test_shared_accounts_empty_initially(self, demo):
        # After switching to fresh shared ledger, no accounts yet
        r = demo.get(f"{API}/accounts")
        assert r.status_code == 200
        assert r.json() == [], "fresh shared ledger should have no accounts leaking from personal"

    def test_data_isolation_and_shared_visibility(self, demo, user2):
        # demo (owner) is in shared ledger. Create an account + txn there.
        r = demo.post(f"{API}/accounts",
                      json={"name": "TEST_SharedAcc", "type": "cash", "opening_balance": 500})
        assert r.status_code == 200
        acc = r.json()

        # user2 switch to shared
        r = user2.post(f"{API}/ledgers/{TestLedgers.shared_id}/switch")
        assert r.status_code == 200

        # user2 sees the shared account
        r = user2.get(f"{API}/accounts")
        assert r.status_code == 200
        assert any(a["id"] == acc["id"] and a["name"] == "TEST_SharedAcc" for a in r.json())

        # user2 creates a shared txn
        r = user2.post(f"{API}/transactions", json={
            "account_id": acc["id"], "type": "expense", "amount": 42,
            "category": "TEST_Groceries", "note": "TEST_by_user2"
        })
        assert r.status_code == 200, r.text
        assert "transaction" in r.json()
        txn_id = r.json()["transaction"]["id"]

        # demo sees user2's txn AND created_by name attached (shared ledger)
        r = demo.get(f"{API}/transactions")
        assert r.status_code == 200
        got = next((t for t in r.json() if t["id"] == txn_id), None)
        assert got is not None
        assert got.get("created_by")  # non-empty in shared

        # user2 switches back to personal — should NOT see this txn
        r = user2.get(f"{API}/auth/me")
        u2_personal = r.json()["personal_ledger_id"]
        r = user2.post(f"{API}/ledgers/{u2_personal}/switch")
        assert r.status_code == 200
        r = user2.get(f"{API}/transactions")
        assert r.status_code == 200
        assert not any(t["id"] == txn_id for t in r.json()), "shared txn leaked into personal!"
        # And user2 shouldn't see the shared account in personal either
        r = user2.get(f"{API}/accounts")
        assert not any(a["id"] == acc["id"] for a in r.json())

    def test_leave_personal_forbidden(self, demo):
        # switch demo back to personal
        me = demo.get(f"{API}/auth/me").json()
        pl = me["personal_ledger_id"]
        demo.post(f"{API}/ledgers/{pl}/switch")
        r = demo.post(f"{API}/ledgers/{pl}/leave")
        assert r.status_code == 400
        assert "personal" in r.json().get("detail", "").lower()

    def test_owner_cannot_leave_while_others_remain(self, demo, user2):
        # user2 joins shared again
        user2.post(f"{API}/ledgers/join", json={"invite_code": TestLedgers.invite_code})
        # demo (owner) tries to leave shared while user2 still member
        r = demo.post(f"{API}/ledgers/{TestLedgers.shared_id}/leave")
        assert r.status_code == 400
        assert "ownership" in r.json().get("detail", "").lower()

    def test_user2_leave_returns_to_personal(self, user2):
        # user2 should be able to leave shared
        r = user2.post(f"{API}/ledgers/{TestLedgers.shared_id}/leave")
        assert r.status_code == 200
        # And now current_ledger_id should be user2 personal
        me = user2.get(f"{API}/auth/me").json()
        assert me["current_ledger_id"] == me["personal_ledger_id"]

    def test_owner_can_leave_when_alone_and_data_deleted(self, demo):
        # After user2 left, demo is alone in shared. Demo can leave; ledger + docs deleted.
        r = demo.post(f"{API}/ledgers/{TestLedgers.shared_id}/leave")
        assert r.status_code == 200
        # Ledger disappears from demo's list
        r = demo.get(f"{API}/ledgers")
        assert not any(x["id"] == TestLedgers.shared_id for x in r.json())


# ---------- 3) CSV / PDF Export ----------
class TestExport:
    def test_csv_export_headers_and_content(self, demo):
        # ensure on personal
        me = demo.get(f"{API}/auth/me").json()
        if me["current_ledger_id"] != me["personal_ledger_id"]:
            demo.post(f"{API}/ledgers/{me['personal_ledger_id']}/switch")
        r = demo.get(f"{API}/export/csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers.get("content-type", "")
        assert "attachment" in r.headers.get("content-disposition", "").lower()
        first_line = r.text.split("\n", 1)[0].strip()
        assert first_line == "Date,Type,Category,Account,Amount,Note", f"unexpected header: {first_line}"

    def test_csv_export_with_month_param(self, demo):
        month = "2026-01"
        r = demo.get(f"{API}/export/csv", params={"month": month})
        assert r.status_code == 200
        assert f"paisabook-{month}.csv" in r.headers.get("content-disposition", "")

    def test_pdf_export_returns_pdf(self, demo):
        r = demo.get(f"{API}/export/pdf")
        assert r.status_code == 200, r.text[:400]
        assert "application/pdf" in r.headers.get("content-type", "")
        # PDF signature
        assert r.content[:4] == b"%PDF", "response is not a PDF"
        assert len(r.content) > 500, "PDF too small"

    def test_pdf_export_with_month(self, demo):
        month = "2026-01"
        r = demo.get(f"{API}/export/pdf", params={"month": month})
        assert r.status_code == 200
        assert r.content[:4] == b"%PDF"
        assert f"paisabook-{month}.pdf" in r.headers.get("content-disposition", "")


# ---------- 4) Budget breach alerts in POST /transactions ----------
class TestBudgetAlerts:
    @pytest.fixture(autouse=True)
    def _ensure_personal(self, demo):
        me = demo.get(f"{API}/auth/me").json()
        if me["current_ledger_id"] != me["personal_ledger_id"]:
            demo.post(f"{API}/ledgers/{me['personal_ledger_id']}/switch")

    def _get_or_create_account(self, s):
        r = s.get(f"{API}/accounts").json()
        if r:
            return r[0]["id"]
        r = s.post(f"{API}/accounts",
                   json={"name": "TEST_BAcc", "type": "cash", "opening_balance": 100000}).json()
        return r["id"]

    def _cleanup(self, s, cat, budget_id=None, txn_ids=None):
        for tid in (txn_ids or []):
            s.delete(f"{API}/transactions/{tid}")
        if budget_id:
            s.delete(f"{API}/budgets/{budget_id}")

    def test_response_shape_wraps_transaction_and_alerts(self, demo):
        acc = self._get_or_create_account(demo)
        r = demo.post(f"{API}/transactions", json={
            "account_id": acc, "type": "expense", "amount": 1,
            "category": f"TEST_NoBudget_{uuid.uuid4().hex[:4]}", "note": "TEST_shape"
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert "transaction" in body and "budget_alerts" in body
        assert isinstance(body["budget_alerts"], list)
        assert body["transaction"]["id"]
        # cleanup
        demo.delete(f"{API}/transactions/{body['transaction']['id']}")

    def test_warning_alert_at_90pct(self, demo):
        acc = self._get_or_create_account(demo)
        cat = f"TEST_BAlert_{uuid.uuid4().hex[:6]}"
        # Budget 1000
        b = demo.post(f"{API}/budgets", json={"category": cat, "amount": 1000}).json()
        # Spend 900 → 90% → warning
        r = demo.post(f"{API}/transactions", json={
            "account_id": acc, "type": "expense", "amount": 900,
            "category": cat, "note": "TEST_warn"
        })
        assert r.status_code == 200, r.text
        alerts = r.json().get("budget_alerts", [])
        assert len(alerts) == 1
        a = alerts[0]
        assert a["category"] == cat
        assert a["level"] == "warning"
        assert 80 <= a["percent"] < 100
        # cleanup
        self._cleanup(demo, cat, budget_id=b["id"], txn_ids=[r.json()["transaction"]["id"]])

    def test_over_alert_when_exceeded(self, demo):
        acc = self._get_or_create_account(demo)
        cat = f"TEST_BOver_{uuid.uuid4().hex[:6]}"
        b = demo.post(f"{API}/budgets", json={"category": cat, "amount": 1000}).json()

        # Spend 900 (warning)
        t1 = demo.post(f"{API}/transactions", json={
            "account_id": acc, "type": "expense", "amount": 900,
            "category": cat, "note": "TEST_over_1"
        }).json()
        # Spend 200 more → total 1100 → over
        r = demo.post(f"{API}/transactions", json={
            "account_id": acc, "type": "expense", "amount": 200,
            "category": cat, "note": "TEST_over_2"
        })
        assert r.status_code == 200, r.text
        alerts = r.json()["budget_alerts"]
        assert len(alerts) == 1
        a = alerts[0]
        assert a["level"] == "over"
        assert a["percent"] >= 100
        # cleanup
        self._cleanup(demo, cat, budget_id=b["id"],
                      txn_ids=[t1["transaction"]["id"], r.json()["transaction"]["id"]])

    def test_no_alert_under_80pct(self, demo):
        acc = self._get_or_create_account(demo)
        cat = f"TEST_BLow_{uuid.uuid4().hex[:6]}"
        b = demo.post(f"{API}/budgets", json={"category": cat, "amount": 1000}).json()
        r = demo.post(f"{API}/transactions", json={
            "account_id": acc, "type": "expense", "amount": 500,
            "category": cat, "note": "TEST_low"
        })
        assert r.status_code == 200
        assert r.json()["budget_alerts"] == []
        self._cleanup(demo, cat, budget_id=b["id"], txn_ids=[r.json()["transaction"]["id"]])

    def test_income_does_not_trigger_alerts(self, demo):
        acc = self._get_or_create_account(demo)
        cat = f"TEST_BIncome_{uuid.uuid4().hex[:6]}"
        b = demo.post(f"{API}/budgets", json={"category": cat, "amount": 100}).json()
        # income of 5000 should NOT trigger alerts even though budget is exceeded (it's not expense)
        r = demo.post(f"{API}/transactions", json={
            "account_id": acc, "type": "income", "amount": 5000,
            "category": cat, "note": "TEST_income"
        })
        assert r.status_code == 200
        assert r.json()["budget_alerts"] == []
        self._cleanup(demo, cat, budget_id=b["id"], txn_ids=[r.json()["transaction"]["id"]])


# ---------- 5) Regression sanity ----------
class TestRegression:
    @pytest.fixture(autouse=True)
    def _ensure_personal(self, demo):
        me = demo.get(f"{API}/auth/me").json()
        if me["current_ledger_id"] != me["personal_ledger_id"]:
            demo.post(f"{API}/ledgers/{me['personal_ledger_id']}/switch")

    def test_analytics_summary_ok(self, demo):
        r = demo.get(f"{API}/analytics/summary")
        assert r.status_code == 200
        d = r.json()
        for k in ("total_income", "total_expense", "net_balance",
                  "udhaar_lene", "udhaar_dene", "expense_by_category"):
            assert k in d

    def test_analytics_monthly_ok(self, demo):
        r = demo.get(f"{API}/analytics/monthly")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_udhaar_list_ok(self, demo):
        r = demo.get(f"{API}/udhaar")
        assert r.status_code == 200

    def test_budgets_list_ok(self, demo):
        r = demo.get(f"{API}/budgets")
        assert r.status_code == 200

    def test_recurring_list_ok(self, demo):
        r = demo.get(f"{API}/recurring")
        assert r.status_code == 200

    def test_ledger_endpoints_require_auth(self):
        r = requests.get(f"{API}/ledgers")
        assert r.status_code == 401
        r = requests.post(f"{API}/ledgers", json={"name": "x"})
        assert r.status_code == 401
        r = requests.get(f"{API}/export/csv")
        assert r.status_code == 401
        r = requests.get(f"{API}/export/pdf")
        assert r.status_code == 401
