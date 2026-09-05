"""Browser -> HTTP -> Supervisor -> durable stores -> real artifact.

Run explicitly with the e2e extra installed. No network/model providers are used.
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest

pw = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def server(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    processes = []

    def start(crash=False):
        code = """
import os, sys, uvicorn
from agenthandler.workbench import create_workbench_app
app = create_workbench_app(sys.argv[1], "test-only-key")
if sys.argv[3] == "crash":
    store = app.state.task_runner.store
    save = store._save
    def crash_after_publish(record, **kwargs):
        if record.milestones.get("publish", {}).get("state") == "executed":
            os._exit(77)
        save(record, **kwargs)
    store._save = crash_after_publish
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[2]), log_level="error")
"""
        log = (tmp_path / f"server-{len(processes)}.log").open("w")
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path), str(port), "crash" if crash else "normal"],
            cwd=Path(__file__).resolve().parents[2],
            stdout=log,
            stderr=log,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        )
        log.close()
        processes.append(process)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(
                    "Server exited: " + (tmp_path / f"server-{len(processes) - 1}.log").read_text()
                )
            try:
                with urlopen(f"http://127.0.0.1:{port}/", timeout=1):
                    return process
            except (URLError, TimeoutError):
                time.sleep(0.1)
        pytest.fail("Server did not start")

    yield start, f"http://127.0.0.1:{port}", tmp_path
    for process in processes:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.fixture
def page():
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 1000})
        yield page
        browser.close()


def connect(page, url):
    page.goto(url)
    page.get_by_label("Server API key").fill("test-only-key")
    page.get_by_role("button", name="Connect", exact=True).click()
    pw.expect(page.locator("#connection")).to_have_text("Connected")


def test_create_verify_download_and_reload(page, server):
    start, url, root = server
    start()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url)
    page.get_by_label("Server API key").fill("wrong-key")
    page.get_by_role("button", name="Connect", exact=True).click()
    pw.expect(page.locator("#error")).not_to_be_empty()
    pw.expect(page.get_by_role("button", name="Create and run")).to_be_disabled()
    connect(page, url)
    page.get_by_role("button", name="Create and run").click()
    pw.expect(page.locator("#status")).to_have_text("verified")
    pw.expect(page.locator("#total")).to_have_text("Verified total: $12.60")
    receipt = next((root / "reports").glob("*.json"))
    original_mtime = receipt.stat().st_mtime_ns
    with page.expect_download() as download:
        page.get_by_role("button", name="Download report").click()
    assert json.loads(Path(download.value.path()).read_text()) == json.loads(receipt.read_text())
    if os.environ.get("AGENTHANDLER_E2E_SCREENSHOT"):
        page.screenshot(path=os.environ["AGENTHANDLER_E2E_SCREENSHOT"], full_page=True)
    connect(page, url)
    page.locator("#jobs button").first.click()
    pw.expect(page.locator("#status")).to_have_text("verified")
    assert receipt.stat().st_mtime_ns == original_mtime
    assert errors == []


def test_process_dies_after_write_and_browser_resumes_same_job(page, server):
    start, url, root = server
    crashed = start(crash=True)
    connect(page, url)
    page.get_by_role("button", name="Create and run").click()
    pw.expect(page.locator("#status")).to_have_text("interrupted")
    assert crashed.wait(timeout=5) == 77
    receipt = next((root / "reports").glob("*.json"))
    original = receipt.read_bytes()
    original_mtime = receipt.stat().st_mtime_ns
    start()
    connect(page, url)
    page.locator("#jobs button").first.click()
    page.get_by_role("button", name="Resume job").click()
    pw.expect(page.locator("#status")).to_have_text("verified")
    pw.expect(page.locator("#budget")).to_contain_text("Calls reserved: 5 / 12")
    assert receipt.read_bytes() == original
    assert receipt.stat().st_mtime_ns == original_mtime
    assert len(list((root / "reports").glob("*.json"))) == 1


def test_invalid_orders_never_create_job(page, server):
    start, url, root = server
    start()
    connect(page, url)
    page.get_by_label("Orders CSV").fill("item,quantity,unit_price\nX,1,NaN")
    page.get_by_role("button", name="Create and run").click()
    pw.expect(page.locator("#error")).to_contain_text("Invalid order row")
    page.get_by_role("button", name="Refresh jobs").click()
    pw.expect(page.locator("#jobs")).to_have_text("No jobs yet.")
    assert list((root / "reports").glob("*.json")) == []
