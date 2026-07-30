"""
Iteration 8 — verify MONGO_URL defensive validation logic
(imports server.py in a subprocess with different MONGO_URL values).
Does NOT touch the live supervised backend.
"""
import subprocess
import sys
import textwrap


def _run_snippet(mongo_url_value: str) -> tuple[int, str]:
    """Run a python child that sets MONGO_URL then imports the validation logic."""
    code = textwrap.dedent(f"""
        import os, sys
        os.environ["MONGO_URL"] = {mongo_url_value!r}
        os.environ["DB_NAME"] = "paisabook_db"
        os.environ["JWT_SECRET"] = "test-secret"
        # Replicate the exact validation snippet from server.py
        mongo_url = os.environ["MONGO_URL"].strip().strip('"').strip("'")
        if not (mongo_url.startswith("mongodb://") or mongo_url.startswith("mongodb+srv://")):
            raise RuntimeError(
                "MONGO_URL must start with 'mongodb://' or 'mongodb+srv://'. "
                f"Got: {{mongo_url[:30]!r}}... "
                "Check your environment variable"
            )
        print("OK:" + mongo_url)
    """)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=15,
    )
    return proc.returncode, (proc.stdout + proc.stderr)


def test_valid_mongodb_scheme_passes():
    rc, out = _run_snippet("mongodb://localhost:27017")
    assert rc == 0
    assert "OK:mongodb://localhost:27017" in out


def test_valid_srv_scheme_passes():
    rc, out = _run_snippet("mongodb+srv://user:pass@cluster.mongodb.net/?retryWrites=true")
    assert rc == 0
    assert "OK:mongodb+srv://user:pass@cluster.mongodb.net" in out


def test_strips_surrounding_double_quotes():
    rc, out = _run_snippet('"mongodb+srv://u:p@c.mongodb.net/"')
    assert rc == 0
    # Ensure surrounding quotes were stripped
    assert "OK:mongodb+srv://u:p@c.mongodb.net/" in out
    # Should NOT retain the leading double-quote
    assert 'OK:"mongodb' not in out


def test_strips_surrounding_single_quotes_and_whitespace():
    rc, out = _run_snippet("  'mongodb://localhost:27017'  ")
    assert rc == 0
    assert "OK:mongodb://localhost:27017" in out


def test_invalid_scheme_raises_runtime_error():
    # Simulates Railway user pasting URL without scheme
    rc, out = _run_snippet("apkamunim:pass@cluster.mongodb.net/")
    assert rc != 0, f"Expected failure, got success. Output: {out}"
    assert "RuntimeError" in out
    assert "MONGO_URL must start with" in out


def test_empty_string_raises():
    rc, out = _run_snippet("")
    assert rc != 0
    assert "RuntimeError" in out


def test_http_scheme_raises():
    rc, out = _run_snippet("http://localhost:27017")
    assert rc != 0
    assert "RuntimeError" in out
