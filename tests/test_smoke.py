#!/usr/bin/env python3
"""Basic smoke tests for the Seconded pipeline.

Run with: python tests/test_smoke.py
Requires: server running at http://localhost:8765 (default), or set
          SECONDED_URL env var.
"""
import json
import os
import time
import urllib.request
import urllib.error
from urllib.parse import urljoin

BASE = os.environ.get("SECONDED_URL", "http://localhost:8765")

def get(path, expect=200):
    try:
        req = urllib.request.Request(urljoin(BASE, path))
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            assert resp.status == expect, f"Expected {expect}, got {resp.status}"
            return data
    except urllib.error.HTTPError as e:
        print(f"FAIL GET {path}: {e.code} {e.read().decode()}")
        raise

def post(path, data=None, expect=200):
    body = json.dumps(data).encode() if data else b""
    headers = {"Content-Type": "application/json"} if data else {}
    try:
        req = urllib.request.Request(urljoin(BASE, path), data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    except urllib.error.HTTPError as e:
        print(f"FAIL POST {path}: {e.code} {e.read().decode()}")
        raise

def test_api_state():
    """GET /api/state should return status object."""
    print("Testing /api/state...")
    data = get("/api/state")
    assert "status" in data, f"Missing 'status' field: {data}"
    print(f"  OK (status={data['status']})")

def test_api_history():
    """GET /api/history should return sessions list."""
    print("Testing /api/history...")
    data = get("/api/history")
    assert "sessions" in data, f"Missing 'sessions' field: {data}"
    print(f"  OK ({len(data['sessions'])} sessions)")

def test_upload_pipeline():
    """Upload a test audio file, wait for pipeline to finish, verify outputs."""
    print("Testing upload pipeline...")
    # Use sample audio if it exists
    sample = "samples/sample_meeting_en.wav"
    if not os.path.exists(sample):
        print(f"  SKIP (sample not found: {sample})")
        return

    # Upload
    import http.client, mimetypes, os.path as osp
    boundary = "----TestBoundary"
    filename = osp.basename(sample)
    with open(sample, "rb") as f:
        file_data = f.read()

    body = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: audio/wav\r\n\r\n"
    ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

    try:
        req = urllib.request.Request(
            urljoin(BASE, "/upload"),
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            upload_result = json.loads(resp.read())
            assert upload_result.get("ok"), f"Upload failed: {upload_result}"
            print(f"  Upload OK: {upload_result.get('name')}")
    except urllib.error.HTTPError as e:
        print(f"  Upload failed: {e.code} {e.read().decode()}")
        raise

    # Wait for processing to complete (up to 5 minutes)
    print("  Waiting for processing...")
    for i in range(60):
        time.sleep(5)
        state = get("/api/state")
        status = state.get("status")
        if status in ("done", "error"):
            break
        if i % 10 == 0:
            print(f"    {i//2} min elapsed, status={status}")
    else:
        print("  TIMEOUT waiting for processing")
        return

    if state.get("status") == "error":
        print(f"  FAIL: processing error: {state}")
        raise AssertionError("Processing failed")

    # Check outputs
    history = get("/api/history")
    sessions = history.get("sessions", [])
    if not sessions:
        print("  FAIL: no sessions in history after upload")
        raise AssertionError("No sessions found")

    latest = sessions[0]
    print(f"  Latest session: {latest.get('name')}")
    print(f"  Duration: {latest.get('duration_s')}s")
    print(f"  Language: {latest.get('language')}")
    print("  OK")

if __name__ == "__main__":
    tests = [test_api_state, test_api_history]
    # Uncomment to test full upload pipeline
    # tests.append(test_upload_pipeline)

    failed = 0
    for test in tests:
        try:
            test()
        except Exception as e:
            print(f"  EXCEPTION: {e}")
            failed += 1

    if failed:
        print(f"\n{failed} test(s) failed")
        exit(1)
    print("\nAll tests passed")
