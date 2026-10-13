#!/usr/bin/env python3
"""Unit tests for the opt-in token auth (server.py).

Runs against Flask's test client — no port, no models loaded.
Run with: python tests/test_auth.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


def client():
    server.app.config["TESTING"] = True
    return server.app.test_client()


def test_auth_disabled_by_default():
    print("Testing auth off by default...")
    server.AUTH_TOKEN = None
    c = client()
    assert c.get("/api/state").status_code == 200
    assert c.get("/api/history").status_code == 200
    print("  OK")


def test_auth_required_when_set():
    print("Testing auth required when token set...")
    server.AUTH_TOKEN = "test-tok-123"
    c = client()
    assert c.get("/api/state").status_code == 401
    assert c.get("/").status_code == 200          # login page
    assert b"access token" in c.get("/").data
    assert c.get("/rootca").status_code in (200, 404, 500)  # exempt (404/500: no cert in test env)
    print("  OK")


def test_token_acceptance_paths():
    print("Testing token acceptance paths...")
    server.AUTH_TOKEN = "test-tok-123"
    c = client()
    assert c.get("/api/state", headers={"X-Seconded-Token": "test-tok-123"}).status_code == 200
    assert c.get("/api/state", headers={"Authorization": "Bearer test-tok-123"}).status_code == 200
    q = "token" + chr(61) + "test-tok-123"
    assert c.get("/api/state?" + q).status_code == 200
    assert c.get("/api/state", headers={"X-Seconded-Token": "wrong"}).status_code == 401
    assert c.get("/api/state?token=***").status_code == 401
    print("  OK")


def test_login_flow():
    print("Testing login flow...")
    server.AUTH_TOKEN = "test-tok-123"
    c = client()
    # bad token
    r = c.post("/login", json={"token": "wrong"})
    assert r.status_code == 401
    # good token (JSON) -> cookie set
    r = c.post("/login", json={"token": "test-tok-123"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert any("seconded_token" in str(v) for v in r.headers.getlist("Set-Cookie"))
    # good token (form, browser Accept) -> 302 to /
    r = c.post("/login", data={"token": "test-tok-123"},
               headers={"Accept": "text/html"})
    assert r.status_code == 302 and r.headers["Location"] == "/"
    # cookie round-trip
    c.get("/login", json={"token": "test-tok-123"})   # sets cookie on client
    assert c.get("/").status_code == 200
    assert b"access token" not in c.get("/").data     # app, not login page
    print("  OK")


def test_nonascii_token_no_500():
    """Non-ASCII tokens must 401, not crash compare_digest (regression)."""
    print("Testing non-ASCII token handling...")
    server.AUTH_TOKEN = "test-tok-123"
    c = client()
    r = c.post("/login", json={"token": "tökén-ünïcode-ééé"})
    assert r.status_code == 401, f"got {r.status_code}"
    r = c.get("/api/state", headers={"X-Seconded-Token": "ünïcode"})
    assert r.status_code == 401
    print("  OK")


if __name__ == "__main__":
    tests = [
        test_auth_disabled_by_default,
        test_auth_required_when_set,
        test_token_acceptance_paths,
        test_login_flow,
        test_nonascii_token_no_500,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            failed += 1
    print("\nAll tests passed" if failed == 0 else f"\n{failed} test(s) failed")
    sys.exit(1 if failed else 0)
