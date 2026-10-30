from __future__ import annotations

import json
import os
import re
import subprocess
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from shutil import rmtree

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    InvalidSessionIdException,
    NoSuchWindowException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.selenium_manager import SeleniumManager

from paths import CONFIG, DOWNLOADS, HISTORY, PROFILE, SESSION_FILE, SHOTS
from workflow import SIGNED_IN_TEXT, SignInRequired, debug, say, sign_in_requested, workflow

POWER_BI_HOME = "https://app.powerbi.com/home"
SIGN_OUT_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/logout"
SIGN_IN_ORIGINS = (
    "https://app.powerbi.com",
    "https://login.microsoftonline.com",
    "https://login.live.com",
    "https://login.windows.net",
)
DEFAULT_RESOURCE = "Ilyass ELGHAZI"
DEFAULT_MONTH = "07/2026"


def run(
    secrets: dict | None = None,
    resource_name: str = DEFAULT_RESOURCE,
    month_value: str = DEFAULT_MONTH,
    run_id: str | None = None,
    index: int = 1,
    browser: "Browser | None" = None,
) -> dict:
    """Run the browser script once, as resource `index` of run `run_id`.

    `secrets` overrides `.env` for this run (for example {"password": "..."}).
    The history entry never includes those values.
    Pass a shared `browser` to reuse one Chrome across runs; otherwise this run opens and closes its own.
    """
    owns_browser = browser is None
    browser = browser or Browser()
    resource_name, month_value = normalize_run_parameters(resource_name, month_value)
    secrets = _secrets(secrets)
    run_id = run_id or new_run_id()
    started = _now()
    status = "ok"
    message = ""
    files: list[str] = []
    screenshot = ""
    try:
        paths = workflow(browser.get(), secrets, resource_name, month_value, stamp=get_config()["signature"])
        files = [path.relative_to(DOWNLOADS).as_posix() for path in paths]
        message = f"{len(files)} files exported"
        debug(message)
    except Exception as exc:
        status = "error"
        message = public_message(exc)
        if isinstance(exc, SignInRequired):
            save_session("signed_out", "Power BI asked to sign in during an export. Sign in again before running exports.")
        else:
            traceback.print_exc()
        screenshot = _screenshot(browser, run_id, resource_name)
    finally:
        if owns_browser:
            browser.close()

    entry = {
        "id": f"{run_id}.{index}",
        "run_id": run_id,
        "started_at": started,
        "finished_at": _now(),
        "status": status,
        "message": message,
        "resource_name": resource_name,
        "month_value": month_value,
        "files": files,
    }
    if screenshot:
        entry["screenshot"] = screenshot
    _record(entry)
    return entry


def _record(entry: dict) -> None:
    """Append the entry, or replace this resource's earlier entry in the same run (a retry) in place."""
    entries = history()
    key = (entry["run_id"], entry["resource_name"].casefold())
    position = next((i for i, item in enumerate(entries)
                     if (item.get("run_id"), str(item.get("resource_name", "")).casefold()) == key), None)
    if position is None:
        taken = {item.get("id") for item in entries}
        index = sum(1 for item in entries if item.get("run_id") == entry["run_id"]) + 1
        while f"{entry['run_id']}.{index}" in taken:
            index += 1
        entry["id"] = f"{entry['run_id']}.{index}"
        with HISTORY.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        return
    entries[position] = {**entry, "id": entries[position].get("id", entry["id"]), "retried_at": entry["started_at"]}
    temporary = HISTORY.with_suffix(".jsonl.tmp")
    temporary.write_text("".join(json.dumps(item) + "\n" for item in entries), encoding="utf-8")
    temporary.replace(HISTORY)


def run_month(run_id: str) -> str | None:
    """Month of an existing run, or None if no run has this ID."""
    return next((entry.get("month_value") for entry in history() if entry.get("run_id") == run_id), None)


def run_batch(
    resources: list[str],
    month_value: str = DEFAULT_MONTH,
    on_progress=None,
    run_id: str | None = None,
    on_result=None,
) -> list[dict]:
    """Run the workflow once per resource, in order, in one Chrome session. A failure never stops the rest."""
    run_id = run_id or new_run_id()
    results = []
    browser = Browser()
    try:
        for index, resource_name in enumerate(resources, start=1):
            if on_progress:
                on_progress(resource_name, index, len(resources))
            results.append(run(resource_name=resource_name, month_value=month_value, run_id=run_id, index=index, browser=browser))
            if on_result:
                on_result(results[-1])
    finally:
        browser.close()
    return results


class Browser:
    """One Chrome shared by consecutive runs; it is only relaunched if it stopped responding."""

    def __init__(self) -> None:
        self.driver: webdriver.Chrome | None = None

    def get(self) -> webdriver.Chrome:
        if self.driver is not None and not _responsive(self.driver):
            say("The browser stopped responding, so it was restarted")
            self.close()
        if self.driver is None:
            self.driver = _browser()
        return self.driver

    def close(self) -> None:
        if self.driver is not None:
            close_browser(self.driver)
            self.driver = None


def _screenshot(browser: "Browser", run_id: str, resource_name: str) -> str:
    """Save what the browser showed when the export failed; returns the file name under SHOTS/<run_id>, or ""."""
    driver = browser.driver
    if driver is None or not _responsive(driver):
        return ""
    try:
        png = driver.get_screenshot_as_png()
    except Exception:
        debug("Could not take a screenshot of the failure")
        return ""
    name = f"{re.sub(r'[^A-Za-z0-9]+', '-', resource_name).strip('-') or 'resource'}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.png"
    folder = SHOTS / run_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(png)
    return name


def _responsive(driver: webdriver.Chrome) -> bool:
    try:
        driver.execute_script("return 1")
        return True
    except Exception:  # a dead chromedriver surfaces as a urllib3 connection error, not a WebDriverException
        return False


RUN_ID = re.compile(r"[0-9a-f]{7}")


def new_run_id(used: set[str] | None = None) -> str:
    """Short random hex ID, like an abbreviated commit hash (e.g. 3f9a1c2)."""
    if used is None:
        migrate_history()
        used = {entry.get("run_id") for entry in history()}
    while True:
        run_id = os.urandom(4).hex()[:7]
        if run_id not in used:
            return run_id


def migrate_history() -> None:
    """Give entries without a hash-style `run_id` one, keeping a backup of the old file."""
    entries = history()
    if all(RUN_ID.fullmatch(str(entry.get("run_id", ""))) for entry in entries):
        return
    runs: list[list[dict]] = []
    for entry in entries:
        previous = runs[-1] if runs else None
        if previous and _same_run(previous, entry):
            previous.append(entry)
        else:
            runs.append([entry])
    used = {entry["run_id"] for entry in entries if RUN_ID.fullmatch(str(entry.get("run_id", "")))}
    lines = []
    for run_entries in runs:
        old = str(run_entries[0].get("run_id", ""))
        run_id = old if RUN_ID.fullmatch(old) else new_run_id(used)
        used.add(run_id)
        for index, entry in enumerate(run_entries, start=1):
            entry.pop("batch_id", None)
            fields = {k: v for k, v in entry.items() if k not in ("id", "run_id")}
            lines.append(json.dumps({"id": f"{run_id}.{index}", "run_id": run_id, **fields}))
    HISTORY.with_suffix(".jsonl.bak").write_text(HISTORY.read_text(encoding="utf-8"), encoding="utf-8")
    HISTORY.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _same_run(previous: list[dict], entry: dict) -> bool:
    """Entries share a run_id or batch_id; untagged ones are consecutive resources of one month, under a minute apart."""
    last = previous[-1]
    run_key = lambda item: item.get("run_id") or item.get("batch_id")
    if run_key(entry) or run_key(last):
        return run_key(entry) == run_key(last)
    if not (entry.get("resource_name") and last.get("resource_name")) or entry.get("month_value") != last.get("month_value"):
        return False
    if any(item.get("resource_name") == entry["resource_name"] for item in previous):
        return False
    gap = datetime.fromisoformat(entry["started_at"]) - datetime.fromisoformat(last["finished_at"])
    return gap.total_seconds() < 60


# --- Persistent configuration (config.json) ---------------------------------

def get_config() -> dict:
    if CONFIG.exists():
        try:
            data = json.loads(CONFIG.read_text(encoding="utf-8"))
            resources = [_clean_resource(item) for item in data.get("resources", [])]
            month = str(data.get("month_value") or DEFAULT_MONTH)
            normalize_month(month)
            return {
                "resources": _unique(resources),
                "month_value": month,
                "signature": _signature_settings(data.get("signature")),
            }
        except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
            pass
    return {
        "resources": [{"name": DEFAULT_RESOURCE, "excluded": False}],
        "month_value": DEFAULT_MONTH,
        "signature": _signature_settings(None),
    }


def active_resources(config: dict | None = None) -> list[str]:
    """Names that will run — resources without the exclude flag."""
    config = config or get_config()
    return [item["name"] for item in config["resources"] if not item["excluded"]]


def save_config(resources: list, month_value: str | None = None, signature: dict | None = None) -> dict:
    current = get_config()
    cleaned = _unique([_clean_resource(item) for item in resources])
    month = normalize_month(month_value) if month_value else current["month_value"]
    config = {
        "resources": cleaned,
        "month_value": month,
        "signature": _signature_settings(signature if signature is not None else current["signature"]),
    }
    CONFIG.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config


def save_signature_options(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Signature settings are required")
    current = get_config()
    merged = {**current["signature"]}
    if "signer_name" in payload:
        name = " ".join(str(payload.get("signer_name") or "").split())
        if len(name) > 120:
            raise ValueError("Name is too long")
        merged["signer_name"] = name
    if "include_name" in payload:
        merged["include_name"] = bool(payload.get("include_name"))
    if "include_datetime" in payload:
        merged["include_datetime"] = bool(payload.get("include_datetime"))
    return save_config(current["resources"], current["month_value"], signature=merged)


def _signature_settings(raw) -> dict:
    data = raw if isinstance(raw, dict) else {}
    name = " ".join(str(data.get("signer_name") or "").split())[:120].rstrip()
    return {
        "signer_name": name,
        "include_name": True if "include_name" not in data else bool(data.get("include_name")),
        "include_datetime": True if "include_datetime" not in data else bool(data.get("include_datetime")),
    }


def add_resource(name: str) -> dict:
    config = get_config()
    resource = normalize_resource_name(name)
    if resource.casefold() in {item["name"].casefold() for item in config["resources"]}:
        raise ValueError(f"'{resource}' is already in the list")
    return save_config([*config["resources"], {"name": resource, "excluded": False}])


def update_resource(old_name: str, new_name: str) -> dict:
    config = get_config()
    resource = normalize_resource_name(new_name)
    items = config["resources"]
    index = next((i for i, item in enumerate(items) if item["name"].casefold() == old_name.casefold()), None)
    if index is None:
        raise ValueError("Resource not found")
    if resource.casefold() in {item["name"].casefold() for i, item in enumerate(items) if i != index}:
        raise ValueError(f"'{resource}' is already in the list")
    items[index] = {**items[index], "name": resource}
    return save_config(items)


def set_resource_excluded(name: str, excluded: bool) -> dict:
    config = get_config()
    items = config["resources"]
    index = next((i for i, item in enumerate(items) if item["name"].casefold() == name.casefold()), None)
    if index is None:
        raise ValueError("Resource not found")
    items[index] = {**items[index], "excluded": bool(excluded)}
    return save_config(items)


def remove_resource(name: str) -> dict:
    config = get_config()
    items = [item for item in config["resources"] if item["name"].casefold() != name.casefold()]
    if len(items) == len(config["resources"]):
        raise ValueError("Resource not found")
    return save_config(items)


def _clean_resource(item) -> dict:
    if isinstance(item, str):  # older config files stored plain names
        return {"name": normalize_resource_name(item), "excluded": False}
    return {"name": normalize_resource_name(item["name"]), "excluded": bool(item.get("excluded", False))}


def _unique(items: list[dict]) -> list[dict]:
    seen, result = set(), []
    for item in items:
        if item["name"].casefold() not in seen:
            seen.add(item["name"].casefold())
            result.append(item)
    return result


def history() -> list[dict]:
    if not HISTORY.exists():
        return []
    entries = []
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def normalize_resource_name(resource_name: str) -> str:
    resource = " ".join((resource_name or "").split())
    if not resource:
        raise ValueError("Resource name is required")
    if len(resource) > 120:
        raise ValueError("Resource name is too long")
    return resource


def normalize_month(month_value: str) -> str:
    month = (month_value or "").strip()
    if not re.fullmatch(r"(0[1-9]|1[0-2])/\d{4}", month):
        raise ValueError("Month must use MM/YYYY")
    return month


def normalize_run_parameters(resource_name: str, month_value: str) -> tuple[str, str]:
    return normalize_resource_name(resource_name), normalize_month(month_value)


def _secrets(overrides: dict | None) -> dict:
    values = {"password": os.getenv("PASSWORD", "")}
    if overrides:
        values.update(overrides)
    return values


def _browser(headless: bool = True) -> webdriver.Chrome:
    options = Options()
    PROFILE.mkdir(exist_ok=True)
    DOWNLOADS.mkdir(exist_ok=True)
    chrome_bin = os.getenv("CHROME_BIN", "").strip()
    if chrome_bin:
        options.binary_location = chrome_bin
    options.add_argument(f"--user-data-dir={PROFILE}")
    options.add_argument("--window-size=1280,900")
    if headless:
        options.add_argument("--headless=new")
    else:
        options.add_argument("--window-position=0,0")
        options.add_argument("--start-maximized")
    if os.getenv("IN_CONTAINER"):
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--password-store=basic")
    options.add_experimental_option(
        "prefs",
        {
            "download.default_directory": str(DOWNLOADS),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
            "plugins.always_open_pdf_externally": True,
        },
    )
    # Ignore a stale chromedriver on PATH and use one that matches installed Chrome.
    driver_path = os.getenv("CHROMEDRIVER", "").strip() or SeleniumManager().binary_paths(
        ["--browser", "chrome", "--skip-driver-in-path"]
    )["driver_path"]
    driver = webdriver.Chrome(service=Service(driver_path), options=options)
    driver.execute_cdp_cmd(
        "Browser.setDownloadBehavior",
        {"behavior": "allow", "downloadPath": str(DOWNLOADS), "eventsEnabled": True},
    )
    if headless:
        # Some sign-in pages treat a "HeadlessChrome" user agent differently from the real browser.
        agent = driver.execute_script("return navigator.userAgent").replace("HeadlessChrome", "Chrome")
        driver.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": agent})
    return driver


# --- Session health, storage and purge (settings page) ------------------------

def session_status() -> dict:
    """Result of the last session check, or 'unknown' if none has run yet, plus whether browser files exist."""
    try:
        result = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result = {"status": "unknown", "checked_at": None, "detail": ""}
    return {**result, "has_profile": has_profile()}


def has_profile() -> bool:
    return PROFILE.is_dir() and any(PROFILE.iterdir())


def check_session(timeout: float = 45) -> dict:
    """Open Power BI with the saved profile in a hidden browser and report whether it is still signed in."""
    status, detail, driver = "unknown", "", None
    try:
        driver = _browser(headless=True)
        driver.get(POWER_BI_HOME)
        status = _wait_for_session(driver, timeout)
        detail = {
            "healthy": "Power BI opens without asking to sign in.",
            "signed_out": "Power BI asks to sign in again.",
        }.get(status, "Power BI did not finish loading in time.")
    except Exception as exc:
        detail = public_message(exc, "Could not open the browser.")
    finally:
        if driver is not None:
            close_browser(driver)
    return save_session(status, detail)


def _wait_for_session(driver: webdriver.Chrome, timeout: float) -> str:
    """'healthy' once the report list shows, 'signed_out' on a sign-in page, else 'unknown' after `timeout`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sign_in_requested(driver):
            return "signed_out"
        body = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
        if SIGNED_IN_TEXT in body:
            return "healthy"
        time.sleep(1)
    return "unknown"


def sign_in(timeout: float = 600) -> dict:
    """Open Power BI in a visible window and wait for the person to sign in; records the session like a check."""
    driver = None
    try:
        say("Opening Power BI")
        driver = _browser(headless=False)
        driver.get(POWER_BI_HOME)
        if _wait_for_session(driver, 20) != "healthy":
            say("Sign in to Power BI on the screen that opened")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and _wait_for_session(driver, 5) != "healthy":
                time.sleep(1)
        if _wait_for_session(driver, 1) == "healthy":
            return save_session("healthy", "Power BI opens without asking to sign in.")
        return save_session("signed_out", f"Sign-in was not completed within {int(timeout // 60)} minutes.")
    finally:
        if driver is not None:
            close_browser(driver)


def save_session(status: str, detail: str) -> dict:
    result = {"status": status, "checked_at": _now(), "detail": detail}
    SESSION_FILE.write_text(json.dumps(result), encoding="utf-8")
    return result


def cut_off_session(method: str) -> dict:
    """End the saved Power BI sign-in, either by signing out ("sign_out") or by deleting the browser profile ("forget")."""
    if method == "sign_out":
        detail = _sign_out()
    elif method == "forget":
        detail = _forget_profile()
    else:
        raise ValueError("Pick how to cut off the session")
    return save_session("signed_out", detail)


def _sign_out(timeout: float = 45) -> str:
    """Sign out through Microsoft in a hidden browser, clear the local sign-in, then confirm Power BI asks to sign in."""
    driver = None
    try:
        say("Starting the log out")
        driver = _browser(headless=True)
        driver.get(POWER_BI_HOME)
        state = _wait_for_session(driver, timeout)
        if state == "signed_out":
            say("Power BI was already logged out")
        else:
            say("Logging out of your Microsoft account")
            driver.get(SIGN_OUT_URL)
            _finish_microsoft_sign_out(driver)
        say("Clearing the saved sign-in from the server")
        driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
        for origin in SIGN_IN_ORIGINS:
            driver.execute_cdp_cmd("Storage.clearDataForOrigin", {"origin": origin, "storageTypes": "all"})
        say("Checking that Power BI now asks you to sign in")
        driver.get(POWER_BI_HOME)
        if _wait_for_session(driver, timeout) == "healthy":
            raise RuntimeError("Power BI still opened without asking to sign in. Try deleting the saved sign-in instead.")
        return "Logged out of Power BI. Sign in again before running exports."
    finally:
        if driver is not None:
            close_browser(driver)


def _finish_microsoft_sign_out(driver: webdriver.Chrome, timeout: float = 30) -> None:
    """Get through Microsoft's sign-out pages: pick the account if asked, then wait for the confirmation."""
    deadline = time.monotonic() + timeout
    picked = False
    while time.monotonic() < deadline:
        body = (driver.execute_script("return document.body ? document.body.innerText : ''") or "").casefold()
        if "pick an account" not in body and ("signed out" in body or "sign in" in body):
            return
        if not picked:
            tiles = driver.find_elements(By.CSS_SELECTOR, "[data-test-id] [role='button'], #tilesHolder [role='button'], .table[role='button']")
            if tiles:
                tiles[0].click()
                picked = True
        time.sleep(1)
    debug("Microsoft sign-out page did not confirm in time; clearing local data anyway")


def _forget_profile() -> str:
    """Delete the browser profile that holds the saved sign-in."""
    if _profile_in_use(PROFILE / "SingletonLock"):
        raise RuntimeError("The export browser is still open. Wait for it to close and try again.")
    say("Deleting the saved sign-in from the server")
    if PROFILE.exists():
        rmtree(PROFILE)
    return "Saved sign-in deleted from the server. Sign in again before running exports."


def storage_summary() -> dict:
    files = [path for path in DOWNLOADS.rglob("*") if path.is_file()] if DOWNLOADS.exists() else []
    pdfs = [path.relative_to(DOWNLOADS).parts for path in files if path.suffix.casefold() == ".pdf"]
    return {
        "pdfs": len(pdfs),
        "bytes": sum(path.stat().st_size for path in files),
        "resources": len({parts[0] for parts in pdfs if len(parts) > 1}),
        "months": len({parts[1] for parts in pdfs if len(parts) > 2}),
    }


def purge_files() -> int:
    """Delete everything under downloads/; returns how many PDFs were removed."""
    removed = storage_summary()["pdfs"]
    if DOWNLOADS.exists():
        for child in DOWNLOADS.iterdir():
            if child.is_dir():
                rmtree(child)
            else:
                child.unlink()
    return removed


def purge_history() -> int:
    """Clear run history (and its migration backup); returns how many runs were removed."""
    removed = len({entry.get("run_id") for entry in history()})
    HISTORY.write_text("", encoding="utf-8")
    HISTORY.with_suffix(".jsonl.bak").unlink(missing_ok=True)
    return removed


def close_browser(driver: webdriver.Chrome, timeout: float = 20) -> None:
    """Quit Chrome and wait until it has really exited.

    Starting Chrome on this profile while a previous one is still shutting down makes it
    reuse that session or fail to start.
    """
    try:
        driver.quit()
    except Exception:
        traceback.print_exc()
    lock = PROFILE / "SingletonLock"
    deadline = time.monotonic() + timeout
    while _profile_in_use(lock) and time.monotonic() < deadline:
        time.sleep(0.5)
    if _profile_in_use(lock):
        debug("Chrome did not exit in time; closing it.")
        subprocess.run(["pkill", "-f", "--", f"--user-data-dir={PROFILE}"], check=False)
        time.sleep(2)
        lock.unlink(missing_ok=True)
    time.sleep(1)


def _profile_in_use(lock: Path) -> bool:
    if lock.is_symlink() or lock.exists():
        return True
    found = subprocess.run(["pgrep", "-f", "--", f"--user-data-dir={PROFILE}"], capture_output=True, check=False)
    return found.returncode == 0


# Anything shown in the app (run results, logs) goes through these: no file paths, local
# endpoints, process details or stack traces. The terminal keeps the full tracebacks.
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://([^/\s:'\"]+)(?::\d+)?[^\s'\"]*", re.IGNORECASE)
_PATH = re.compile(r"(?<![\w.])(?:~|[A-Za-z]:)?(?:[/\\][^\s/\\'\"]+){2,}[/\\]?")
_HEX = re.compile(r"\b0x[0-9a-f]{4,}\b|\b[0-9a-f]{16,}\b", re.IGNORECASE)
_PORT = re.compile(r"\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d+)?\b|\bport[ =:]+\d+\b", re.IGNORECASE)


def redact(text: str) -> str:
    text = _URL.sub(lambda m: "the local browser" if m.group(1) in ("localhost", "127.0.0.1") else m.group(1), text)
    text = _PATH.sub(lambda m: re.split(r"[/\\]", m.group(0).rstrip("/\\"))[-1] or "a local folder", text)
    text = _PORT.sub("the local browser", text)
    return _HEX.sub("…", text)


def public_message(exc: BaseException, fallback: str = "Something unexpected went wrong. Please try again.") -> str:
    """One readable line for the UI, without paths, endpoints or Selenium stack traces."""
    if isinstance(exc, (ElementClickInterceptedException, ElementNotInteractableException, StaleElementReferenceException)):
        return "Could not click a button in the report."
    if isinstance(exc, TimeoutException):
        raw = ((exc.msg or "") if isinstance(exc, WebDriverException) else str(exc)).casefold()
        if "click" in raw or "clickable" in raw:
            return "Could not click a button in the report. It did not appear in time."
        return "The report took too long to respond."
    if isinstance(exc, (NoSuchWindowException, InvalidSessionIdException)):
        return "The browser closed unexpectedly during the export."
    if not isinstance(exc, (WebDriverException, RuntimeError, ValueError, TimeoutError)):
        return fallback
    raw = (exc.msg or "") if isinstance(exc, WebDriverException) else str(exc)
    first = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    first = redact(first.removeprefix("Message:").strip())[:300]
    if not first and isinstance(exc, WebDriverException):
        return "The browser stopped responding."
    return first or fallback


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    result = run()
    raise SystemExit(0 if result["status"] == "ok" else 1)
