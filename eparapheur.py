"""Send one successful export's monthly and weekly PDFs to Ersal.

The site is only reachable from the Windows machine that runs this app.
Flow: sign in, new request with the monthly PDF as Compte Rendu d'Activité,
wait for that file in the inbox, fill the request, attach the weekly PDF,
then Actions → Envoyer pour validation.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader
from selenium import webdriver
from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.selenium_manager import SeleniumManager

from paths import EPARAPHEUR, EPARAPHEUR_PROFILE, SHOTS

ERSAL_URL = "https://ersal-prd.attijariwafa.net/navigator/"
DOCUMENT_TYPE = "Compte Rendu d'Activité"
PROJECT_CODE = "0"
_MONTHS = (
    "janvier", "fevrier", "mars", "avril", "mai", "juin",
    "juillet", "aout", "septembre", "octobre", "novembre", "decembre",
)

_lock = threading.Lock()
_job = {"running": False, "entry_id": "", "run_id": "", "step": "", "error": ""}
_listener = None

_FIND_JS = """
const wanted = arguments[0];
const exact = arguments[1];
const norm = (value) => (value || "")
  .normalize("NFD").replace(/[\\u0300-\\u036f]/g, "")
  .replace(/\\s+/g, " ").trim().toLowerCase();
const target = norm(wanted);
const own = (node) => norm([...node.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join(" "));
const disabled = (node) => {
  let current = node;
  for (let i = 0; i < 4 && current; i += 1) {
    if (/disabled|dijitDisabled/i.test(String(current.className || ""))) return true;
    if (current.getAttribute && (current.hasAttribute("disabled") || current.getAttribute("aria-disabled") === "true")) return true;
    current = current.parentElement;
  }
  return false;
};
let best = null;
let bestScore = Infinity;
for (const node of document.querySelectorAll("button, a, [role='button'], .dijitButton, .dijitMenuItem, input[type='button'], input[type='submit'], span, div, td, label")) {
  const style = getComputedStyle(node);
  if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0" || disabled(node)) continue;
  if (!node.getClientRects().length || node.offsetWidth < 2 || node.offsetHeight < 2) continue;
  let text = own(node);
  if (!text) text = norm(node.innerText || node.value || node.getAttribute("aria-label") || "");
  if (!text || text.length > 80) continue;
  const hit = exact ? text === target : text === target || text.includes(target);
  if (!hit) continue;
  const score = text.length * 1000 + node.offsetWidth * node.offsetHeight;
  if (score < bestScore) { best = node; bestScore = score; }
}
return best;
"""

_FIELD_JS = """
const wanted = arguments[0];
const norm = (value) => (value || "")
  .normalize("NFD").replace(/[\\u0300-\\u036f]/g, "")
  .replace(/[*＊✱∗﹡:：]/g, " ").replace(/\\s+/g, " ").trim().toLowerCase();
const target = norm(wanted);
const own = (node) => norm([...node.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join(" "));
const shown = (node) => {
  if (!node) return false;
  const style = getComputedStyle(node);
  if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
  return !!node.getClientRects().length && node.offsetWidth > 2 && node.offsetHeight > 2;
};
const textOf = (node) => own(node) || (node.children.length ? "" : norm(node.textContent));
const matches = (text) => text === target || text.startsWith(target) || text.endsWith(target) || text.includes(" " + target);
const controlIn = (root) => {
  if (!root || !root.querySelector) return null;
  const box = root.matches(".dijitComboBox, .dijitSelect, [role='combobox']")
    ? root
    : root.querySelector(".dijitComboBox, .dijitSelect, [role='combobox']");
  if (box && shown(box)) {
    return box.querySelector("input.dijitInputInner")
      || [...box.querySelectorAll("input, textarea")].find((el) => !el.classList.contains("dijitArrowButtonInner") && (el.getAttribute("type") || "text").toLowerCase() !== "hidden")
      || box;
  }
  const field = [...root.querySelectorAll("input, textarea, select")].find((el) => {
    const type = (el.getAttribute("type") || "").toLowerCase();
    return !["hidden", "file", "checkbox", "radio", "submit", "button"].includes(type) && shown(el);
  });
  return field || null;
};
const labels = [];
for (const node of document.querySelectorAll("label, span, td, div, legend")) {
  const text = textOf(node);
  if (!text || text.length > 80 || !matches(text) || !shown(node)) continue;
  labels.push(node);
}
labels.sort((a, b) => textOf(a).length - textOf(b).length);
for (const label of labels) {
  const owner = label.closest("label");
  if (owner && owner.htmlFor) {
    const linked = document.getElementById(owner.htmlFor);
    const box = linked && linked.closest ? linked.closest(".dijitComboBox, .dijitSelect, [role='combobox']") : null;
    const resolved = (box && controlIn(box)) || (linked && linked.matches && linked.matches("input, textarea, select") && shown(linked) ? linked : null);
    if (resolved) return resolved;
  }
  let node = label;
  for (let depth = 0; depth < 8 && node; depth += 1) {
    let sib = node.nextElementSibling;
    while (sib) {
      const found = controlIn(sib) || (sib.matches && sib.matches("input, textarea, select") && shown(sib) ? sib : null);
      if (found) return found;
      sib = sib.nextElementSibling;
    }
    node = node.parentElement;
  }
}
return null;
"""

_OPTIONS_JS = """
const seen = new Set();
const out = [];
for (const node of document.querySelectorAll(".dijitMenuItem, [role='option']")) {
  const style = getComputedStyle(node);
  if (style.display === "none" || style.visibility === "hidden" || !node.getClientRects().length) continue;
  const text = (node.innerText || "").replace(/\\s+/g, " ").trim();
  if (!text || seen.has(text)) continue;
  seen.add(text);
  out.push(text);
}
return out;
"""


def form_settings() -> dict:
    data = _load()
    return {
        "username": data.get("username") or "",
        "password_set": bool(data.get("password")),
        "company": data.get("company") or "",
        "provider": data.get("provider") or "",
        "approver": data.get("approver") or "",
    }


def save_form(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise ValueError("Eparapheur settings are required")
    data = _load()
    for key, limit in (("username", 120), ("company", 160), ("provider", 160), ("approver", 160)):
        if key not in payload:
            continue
        value = " ".join(str(payload.get(key) or "").split())
        if len(value) > limit:
            raise ValueError("That value is too long")
        data[key] = value
    if str(payload.get("password") or ""):
        password = str(payload.get("password"))
        if len(password) > 200:
            raise ValueError("Password is too long")
        data["password"] = password
    _save(data)


def status() -> dict:
    with _lock:
        job = dict(_job)
    job["sent"] = list(_load().get("sent") or {})
    return job


def start(entry_id: str, monthly: Path, weekly: Path, month_value: str, days: str, run_id: str = "", resource_name: str = "", on_log=None) -> None:
    """Start the send on a background thread. Raises ValueError when it cannot start."""
    if not re.fullmatch(r"(0[1-9]|1[0-2])/\d{4}", month_value or ""):
        raise ValueError("This export has no month")
    if not days:
        raise ValueError("The monthly PDF has no Nombre de JH. Open it and check the total.")
    settings = _load()
    missing = []
    if not settings.get("username") or not settings.get("password"):
        missing.append("the Ersal username and password")
    if not (settings.get("company") or settings.get("provider")):
        missing.append("the company name or the provider name")
    if not settings.get("approver"):
        missing.append("the next approver")
    if missing:
        raise ValueError("Add " + " and ".join(missing) + " in Settings first")
    if entry_id in (settings.get("sent") or {}):
        raise ValueError("This export was already sent to Eparapheur")
    with _lock:
        if _job["running"]:
            raise ValueError("An Eparapheur send is already running")
        _job.update(running=True, entry_id=entry_id, run_id=run_id, step="Starting", error="")
        global _listener
        _listener = on_log
    threading.Thread(
        target=_run,
        name="eparapheur",
        args=(entry_id, monthly, weekly, month_value, days, settings, resource_name),
        daemon=True,
    ).start()


def billable_days(path: Path) -> str:
    """The Nombre de JH printed on the first page of a monthly PDF."""
    try:
        text = PdfReader(str(path)).pages[0].extract_text() or ""
    except Exception:
        return ""
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if line.replace(":", "").strip().casefold() == "nombre de jh" and index + 1 < len(lines) and lines[index + 1]:
            return lines[index + 1].strip()
    match = re.search(r"Nombre de JH\s*[:\s]\s*([0-9]+(?:[.,][0-9]+)?)", text, re.IGNORECASE)
    return match.group(1) if match else ""


def pick_choice(options: list[str], wanted: str) -> str | None:
    target = _norm(wanted)
    exact = [option for option in options if _norm(option) == target]
    if exact:
        return exact[0]
    partial = [option for option in options if target and _has(_norm(option), target)]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise RuntimeError(
            f"Several choices match “{wanted}”: {', '.join(partial[:6])}. Type the full name in Settings."
        )
    return None


def pick_period(options: list[str], month_value: str) -> str | None:
    month, year = month_value.split("/")
    name = _MONTHS[int(month) - 1]
    bare = str(int(month))
    best = None
    best_score = 0
    for option in options:
        text = _norm(option)
        score = 0
        if _has(text, month) and _has(text, year):
            score = 5
        elif _has(text, name) and _has(text, year):
            score = 4
        elif _has(text, bare) and _has(text, year):
            score = 3
        if score > best_score:
            best, best_score = option, score
    return best


def _run(entry_id: str, monthly: Path, weekly: Path, month_value: str, days: str, settings: dict, resource_name: str) -> None:
    driver = None
    error = ""
    who = resource_name or "this export"
    _emit(f"Eparapheur · {who} · Starting")
    try:
        driver = _chrome()
        transfer(
            driver,
            username=settings.get("username") or "",
            password=settings.get("password") or "",
            monthly=monthly,
            weekly=weekly,
            month_value=month_value,
            days=days,
            company=(settings.get("company") or settings.get("provider") or ""),
            provider=(settings.get("provider") or settings.get("company") or ""),
            approver=settings.get("approver") or "",
        )
        data = _load()
        sent = dict(data.get("sent") or {})
        sent[entry_id] = datetime.now(timezone.utc).isoformat()
        data["sent"] = sent
        _save(data)
        _emit(f"Eparapheur · {who} · Sent to Ersal", "ok")
    except Exception as exc:
        traceback.print_exc()
        error = _readable(exc)
        _emit(f"Eparapheur · {who} · {error}", "error")
        if driver is not None:
            _snap(driver)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        with _lock:
            global _listener
            _listener = None
            _job.update(running=False, run_id="", step="", error=error)


def transfer(
    driver: webdriver.Chrome,
    *,
    username: str,
    password: str,
    monthly: Path,
    weekly: Path,
    month_value: str,
    days: str,
    company: str,
    provider: str,
    approver: str,
) -> None:
    description = f"CRA_{month_value}"
    _step("Signing in")
    _login(driver, username, password)
    _step("New request")
    _open_new_request(driver)
    _step("Monthly PDF")
    _upload(driver, monthly)
    _step("Document type")
    _choose(driver, "Type du document", lambda options: pick_choice(options, DOCUMENT_TYPE), "Compte Rendu")
    _fill_if_present(driver, "Titre du document", description)
    _fill_if_present(driver, "Descriptif de la demande", description)
    _step("Saving")
    _click(driver, "Ajouter", exact=True)
    _wait_dialog_closed(driver)
    _step("Waiting 30s")
    time.sleep(30)
    _step("Refreshing the list")
    _wait_for_row(driver, monthly.name)
    _step("Opening")
    _open_row(driver, monthly.name)
    _step("Filling the form")
    _type_into(driver, ("Raison Sociale Fournisseur", "Raison sociale"), company)
    _type_into(driver, ("Nom du prestataire", "Nom du prestataire"), provider)
    _type_into(driver, ("Nombre de jours travaillés", "Nombre de jours travailles"), days)
    _type_into(driver, ("Descriptif de la demande",), description)
    _set_project_code(driver)
    _choose(driver, "Période de prestation", lambda options: pick_period(options, month_value), month_value)
    _choose(driver, "Validateur suivant", lambda options: pick_choice(options, approver), approver)
    _step("Weekly PDF")
    _click(driver, "Ajout complément")
    _upload(driver, weekly)
    _fill_if_present(driver, "Titre du document", weekly.stem)
    _click(driver, "Ajouter", exact=True)
    _wait_back(driver, weekly.name)
    _step("Sending")
    _click(driver, "Actions", exact=True)
    time.sleep(0.4)
    _click(driver, "Envoyer pour validation")
    _confirm(driver)
    _wait_sent(driver)


def _open(driver: webdriver.Chrome, url: str) -> None:
    """Open the page. Ersal keeps loading scripts after the form is already usable."""
    try:
        driver.get(url)
    except TimeoutException:
        print("eparapheur: the page is still loading; continuing", flush=True)


def _emit(text: str, level: str = "step") -> None:
    """One run-log line. A step also renames the button, without logging twice."""
    if level == "step":
        _remember(text.rsplit(" · ", 1)[-1])
    else:
        print(f"eparapheur: {text}", flush=True)
    _notify(text, level)


def _step(text: str) -> None:
    print(f"eparapheur: {text}", flush=True)
    _remember(text)
    _notify(text, "step")


def _remember(text: str) -> None:
    with _lock:
        _job["step"] = text


def _notify(text: str, level: str) -> None:
    with _lock:
        callback = _listener
    if callback is None:
        return
    try:
        callback(text, level)
    except Exception:
        traceback.print_exc()


def _login(driver: webdriver.Chrome, username: str, password: str) -> None:
    _open(driver, ERSAL_URL)
    deadline = time.time() + 90
    while time.time() < deadline:
        if _page_has(driver, "nouvelle demande") or _page_has(driver, "mes demandes"):
            return
        if _password_field(driver) is not None or _page_has(driver, "mot de passe") or _page_has(driver, "se connecter"):
            break
        time.sleep(0.4)
    else:
        raise RuntimeError(f"The Ersal sign-in page did not open. Chrome showed {_page_clue(driver)}")
    field = _find_field(driver, "Nom d'utilisateur") or _first_text_input(driver)
    secret = _find_field(driver, "Mot de passe") or _password_field(driver)
    if field is None or secret is None:
        raise RuntimeError("Could not find the Ersal username or password field")
    # The sign-in box takes the short name, as in f.guerouali, not the mail address.
    login_name = username.split("@", 1)[0].strip() if "@" in username else username.strip()
    _step(f"Signing in as {login_name}")
    _write(driver, field, login_name, require=True)
    _write(driver, secret, password, require=True)
    _submit_login(driver, secret)
    deadline = time.time() + 40
    while time.time() < deadline:
        if _page_has(driver, "nouvelle demande") or _page_has(driver, "mes demandes"):
            return
        if _rejected(driver):
            raise RuntimeError("Ersal did not accept the username or password. Check them in Settings.")
        time.sleep(0.4)
    try:
        still_empty = not (secret.get_attribute("value") or "").strip()
    except WebDriverException:
        still_empty = False
    if still_empty:
        raise RuntimeError("The Ersal password field stayed empty. Check the password in Settings.")
    raise RuntimeError(f"Ersal stayed on the sign-in page. Chrome showed {_page_clue(driver)}")


def _rejected(driver: webdriver.Chrome) -> bool:
    text = _norm(_visible_text(driver))
    return any(part in text for part in ("incorrect", "echec", "refus"))


def _submit_login(driver: webdriver.Chrome, secret) -> None:
    """Se connecter stays grey until the password is really in the box."""
    driver.execute_script(
        """
        const secret = arguments[0];
        const form = secret.form;
        const norm = (value) => (value || "")
          .normalize("NFD").replace(/[\\u0300-\\u036f]/g, "")
          .replace(/\\s+/g, " ").trim().toLowerCase();
        for (const node of document.querySelectorAll("button, input[type='submit'], a, span")) {
          const text = norm(node.value || node.innerText || "");
          if (text !== "se connecter") continue;
          node.disabled = false;
          node.removeAttribute("disabled");
          node.setAttribute("aria-disabled", "false");
        }
        try {
          if (form && typeof form.requestSubmit === "function") form.requestSubmit();
          else if (form) form.submit();
        } catch (error) {}
        """,
        secret,
    )
    try:
        _click(driver, "Se connecter", exact=True, timeout=3)
    except RuntimeError:
        try:
            secret.send_keys(Keys.ENTER)
        except WebDriverException:
            pass


def _open_new_request(driver: webdriver.Chrome) -> None:
    """The file field exists only inside the form opened by Ajouter Nouvelle Demande."""
    deadline = time.time() + 25
    while time.time() < deadline:
        if _add_form_open(driver):
            return
        try:
            _press(driver, "Ajouter Nouvelle Demande", exact=True, timeout=4)
        except RuntimeError:
            try:
                _press(driver, "nouvelle demande", timeout=4)
            except RuntimeError:
                pass
        time.sleep(0.6)
    raise RuntimeError(f"The new request form did not open. Chrome showed {_page_clue(driver)}")


def _add_form_open(driver: webdriver.Chrome) -> bool:
    # "Type du document" is also a column on the inbox, so it does not mean the form is open.
    return (
        _page_has(driver, "nom de fichier")
        or _page_has(driver, "choisir des fichiers")
        or _page_has(driver, "titre du document")
    )


def _press(driver: webdriver.Chrome, text: str, exact: bool = False, timeout: float = 20) -> None:
    """Dojo toolbar buttons ignore a plain click on the label."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        node = _find(driver, text, exact)
        if node is not None and not _disabled(driver, node):
            try:
                button = driver.execute_script(
                    """
                    const node = arguments[0];
                    const button = node.closest(".dijitButton, .dijitDropDownButton, button, a, [role='button']") || node;
                    button.scrollIntoView({block: "center", inline: "nearest"});
                    return button;
                    """,
                    node,
                )
                try:
                    button.click()
                except WebDriverException:
                    driver.execute_script("arguments[0].click()", button)
                return
            except StaleElementReferenceException:
                pass
            except WebDriverException:
                pass
        time.sleep(0.3)
    raise RuntimeError(f"Could not find “{text}” on Ersal")


def _upload(driver: webdriver.Chrome, path: Path) -> None:
    target = _file_input(driver)
    if target is None:
        raise RuntimeError(f"The Ersal file field did not open. Chrome showed {_page_clue(driver)}")
    driver.execute_script(
        """
        arguments[0].style.setProperty('display', 'block', 'important');
        arguments[0].style.setProperty('visibility', 'visible', 'important');
        arguments[0].style.setProperty('opacity', '1', 'important');
        arguments[0].style.setProperty('height', '40px', 'important');
        arguments[0].style.setProperty('width', '240px', 'important');
        arguments[0].style.setProperty('position', 'fixed', 'important');
        arguments[0].style.setProperty('z-index', '2147483647', 'important');
        """,
        target,
    )
    file_path = str(path.resolve())
    try:
        target.send_keys(file_path)
    except WebDriverException:
        target.send_keys(file_path)


def _choose(driver: webdriver.Chrome, label: str, pick, hint: str) -> None:
    deadline = time.time() + 20
    field = None
    while time.time() < deadline:
        field = _find_field(driver, label)
        if field is not None:
            break
        time.sleep(0.4)
    if field is None:
        raise RuntimeError(f"Could not find {label} on the Ersal form. Chrome showed {_page_clue(driver)}")
    _open_arrow(driver, field)
    time.sleep(0.5)
    options = _options(driver)
    chosen = pick(options) if options else None
    if chosen is None and hint:
        field = _find_field(driver, label)
        if field is None:
            raise RuntimeError(f"Could not find {label} on the Ersal form")
        _write(driver, field, hint)
        time.sleep(0.6)
        options = _options(driver)
        chosen = pick(options) if options else None
    if not chosen:
        sample = ", ".join(options[:8]) or "the list was empty"
        raise RuntimeError(f"Could not choose {label}. Seen: {sample}")
    _click_option(driver, chosen)


def _set_project_code(driver: webdriver.Chrome) -> None:
    try:
        _choose(driver, "Code projet", lambda options: pick_choice(options, PROJECT_CODE), PROJECT_CODE)
    except RuntimeError:
        _type_into(driver, ("Code projet",), PROJECT_CODE)


def _type_into(driver: webdriver.Chrome, labels: tuple[str, ...], value: str) -> None:
    for label in labels:
        field = _find_field(driver, label)
        if field is not None:
            _write(driver, field, value)
            return
    raise RuntimeError(f"Could not find {labels[0]} on the Ersal form")


def _fill_if_present(driver: webdriver.Chrome, label: str, value: str) -> None:
    field = _find_field(driver, label)
    if field is None:
        return
    if (field.get_attribute("value") or "").strip():
        return
    _write(driver, field, value)


def _write(driver: webdriver.Chrome, field, value: str, require: bool = False) -> None:
    """Type into a field, then check the value stayed. A greyed-out button means it did not."""
    driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'nearest'}); arguments[0].focus();", field)
    try:
        field.click()
    except WebDriverException:
        pass
    try:
        field.clear()
    except WebDriverException:
        _set_input_value(driver, field, "")
    try:
        field.send_keys(value)
    except WebDriverException:
        pass
    if (field.get_attribute("value") or "") != value:
        _set_input_value(driver, field, value)
    if (field.get_attribute("value") or "") != value:
        for character in value:
            try:
                field.send_keys(character)
            except WebDriverException:
                break
    if (field.get_attribute("value") or "") != value:
        _set_input_value(driver, field, value)
    _fire_input(driver, field)
    if require and (field.get_attribute("value") or "") != value:
        kind = "password" if (field.get_attribute("type") or "").lower() == "password" else "sign-in"
        raise RuntimeError(f"Could not type into the Ersal {kind} field")


def _set_input_value(driver: webdriver.Chrome, field, value: str) -> None:
    driver.execute_script(
        """
        const el = arguments[0];
        const value = arguments[1];
        const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const desc = Object.getOwnPropertyDescriptor(proto, "value");
        if (desc && desc.set) desc.set.call(el, value);
        else el.value = value;
        """,
        field,
        value,
    )


def _fire_input(driver: webdriver.Chrome, field) -> None:
    driver.execute_script(
        """
        const el = arguments[0];
        el.dispatchEvent(new Event("input", {bubbles: true}));
        el.dispatchEvent(new Event("change", {bubbles: true}));
        el.dispatchEvent(new KeyboardEvent("keyup", {bubbles: true}));
        """,
        field,
    )


def _open_arrow(driver: webdriver.Chrome, field) -> None:
    arrow = driver.execute_script(
        """
        const field = arguments[0];
        const box = field.closest(".dijitComboBox, .dijitSelect, .dijitValidationTextBox, .dijitTextBox") || field.parentElement;
        return box ? box.querySelector(".dijitArrowButton, .dijitDownArrowButton") : null;
        """,
        field,
    )
    if arrow is not None:
        try:
            arrow.click()
            return
        except WebDriverException:
            driver.execute_script("arguments[0].click()", arrow)
            return
    try:
        field.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click()", field)


def _options(driver: webdriver.Chrome) -> list[str]:
    found: list[str] = []
    driver.switch_to.default_content()
    found.extend(driver.execute_script(_OPTIONS_JS) or [])
    frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    for frame in frames:
        driver.switch_to.default_content()
        try:
            driver.switch_to.frame(frame)
        except WebDriverException:
            continue
        found.extend(driver.execute_script(_OPTIONS_JS) or [])
    driver.switch_to.default_content()
    unique = []
    for option in found:
        if option not in unique:
            unique.append(option)
    return unique


def _click_option(driver: webdriver.Chrome, text: str) -> None:
    script = """
    const wanted = arguments[0];
    const norm = (value) => (value || "").replace(/\\s+/g, " ").trim().toLowerCase();
    const target = norm(wanted);
    for (const node of document.querySelectorAll(".dijitMenuItem, [role='option']")) {
      if (!node.getClientRects().length) continue;
      if (norm(node.innerText) === target) return node;
    }
    return null;
    """
    driver.switch_to.default_content()
    node = driver.execute_script(script, text)
    if node is None:
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
        for frame in frames:
            driver.switch_to.default_content()
            try:
                driver.switch_to.frame(frame)
            except WebDriverException:
                continue
            node = driver.execute_script(script, text)
            if node is not None:
                break
    if node is None:
        raise RuntimeError(f"The choice “{text}” disappeared from the list")
    try:
        node.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click()", node)


def _wait_for_row(driver: webdriver.Chrome, filename: str) -> None:
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            _click(driver, "Réinitialiser", exact=True, timeout=2)
        except RuntimeError:
            try:
                _click(driver, "Réinitialiser", timeout=2)
            except RuntimeError:
                pass
        time.sleep(2)
        if _row_visible(driver, filename):
            return
        time.sleep(3)
    raise RuntimeError("The monthly PDF did not appear in the Ersal inbox")


def _open_row(driver: webdriver.Chrome, filename: str) -> None:
    needle = Path(filename).stem
    script = """
    const wanted = arguments[0];
    const norm = (value) => (value || "")
      .normalize("NFD").replace(/[\\u0300-\\u036f]/g, "")
      .replace(/\\s+/g, " ").trim().toLowerCase();
    const target = norm(wanted);
    let best = null;
    let bestScore = Infinity;
    for (const node of document.querySelectorAll("a, td, span")) {
      if (!node.getClientRects().length) continue;
      const text = norm(node.innerText || node.getAttribute("title") || "");
      if (!text.includes(target) || text.length > 180) continue;
      const score = text.length;
      if (score < bestScore) { best = node; bestScore = score; }
    }
    return best;
    """
    deadline = time.time() + 20
    while time.time() < deadline:
        driver.switch_to.default_content()
        node = driver.execute_script(script, needle)
        if node is None:
            frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
            for frame in frames:
                driver.switch_to.default_content()
                try:
                    driver.switch_to.frame(frame)
                except WebDriverException:
                    continue
                node = driver.execute_script(script, needle)
                if node is not None:
                    break
        if node is not None:
            try:
                node.click()
            except WebDriverException:
                driver.execute_script("arguments[0].click()", node)
            break
        time.sleep(0.4)
    else:
        raise RuntimeError("Could not open the monthly PDF in the Ersal inbox")
    deadline = time.time() + 30
    while time.time() < deadline:
        if _page_has(driver, "Ajout complément") or _page_has(driver, "Nombre de jours") or _page_has(driver, "Validation Initiateur"):
            return
        time.sleep(0.4)
    raise RuntimeError("The Ersal request did not open")


def _wait_dialog_closed(driver: webdriver.Chrome) -> None:
    deadline = time.time() + 90
    while time.time() < deadline:
        error = _error_text(driver)
        if error:
            raise RuntimeError(error)
        if not _page_has(driver, "ajouter un document"):
            return
        time.sleep(0.5)
    raise RuntimeError("The Ersal form stayed open after Add")


def _wait_back(driver: webdriver.Chrome, filename: str) -> None:
    deadline = time.time() + 90
    while time.time() < deadline:
        error = _error_text(driver)
        if error:
            raise RuntimeError(error)
        closed = not _page_has(driver, "ajouter un document")
        if closed and (_page_has(driver, "Ajout complément") or _page_has(driver, filename)):
            return
        time.sleep(0.5)
    raise RuntimeError("Ersal did not return to the request after the weekly PDF")


def _confirm(driver: webdriver.Chrome) -> None:
    deadline = time.time() + 6
    while time.time() < deadline:
        text = _norm(_visible_text(driver))
        if any(word in text for word in ("confirmer", "etes vous sur", "voulez vous")):
            for label in ("Oui", "OK", "Valider"):
                try:
                    _click(driver, label, exact=True, timeout=1)
                    return
                except RuntimeError:
                    continue
        time.sleep(0.3)


def _wait_sent(driver: webdriver.Chrome) -> None:
    deadline = time.time() + 25
    while time.time() < deadline:
        error = _error_text(driver)
        if error:
            raise RuntimeError(error)
        text = _norm(_visible_text(driver))
        if "envoy" in text and "envoyer pour validation" not in text:
            return
        if "mes demandes" in text and "validation initiateur" not in text:
            return
        time.sleep(0.5)
    raise RuntimeError("Ersal did not confirm that the request was sent")


def _click(driver: webdriver.Chrome, text: str, exact: bool = False, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        node = _find(driver, text, exact)
        if node is not None and not _disabled(driver, node):
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'nearest'})", node)
                node.click()
                return
            except StaleElementReferenceException:
                pass
            except WebDriverException:
                try:
                    driver.execute_script("arguments[0].click()", node)
                    return
                except WebDriverException:
                    pass
        time.sleep(0.3)
    raise RuntimeError(f"Could not find “{text}” on Ersal")


def _disabled(driver: webdriver.Chrome, node) -> bool:
    try:
        return bool(driver.execute_script(
            """
            let node = arguments[0];
            for (let i = 0; i < 4 && node; i += 1) {
              const cls = String(node.className || "");
              if (/disabled|dijitDisabled/i.test(cls)) return true;
              if (node.getAttribute && (node.hasAttribute("disabled") || node.getAttribute("aria-disabled") === "true")) return true;
              node = node.parentElement;
            }
            return false;
            """,
            node,
        ))
    except WebDriverException:
        return False


def _find(driver: webdriver.Chrome, text: str, exact: bool):
    driver.switch_to.default_content()
    found = driver.execute_script(_FIND_JS, text, exact)
    if found is not None:
        return found
    frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    for frame in frames:
        driver.switch_to.default_content()
        try:
            driver.switch_to.frame(frame)
        except WebDriverException:
            continue
        found = driver.execute_script(_FIND_JS, text, exact)
        if found is not None:
            return found
    driver.switch_to.default_content()
    return None


def _find_field(driver: webdriver.Chrome, label: str):
    driver.switch_to.default_content()
    return _find_field_here(driver, label, 0)


def _find_field_here(driver: webdriver.Chrome, label: str, depth: int):
    try:
        found = driver.execute_script(_FIELD_JS, label)
    except WebDriverException:
        found = None
    if found is not None:
        return found
    if depth >= 3:
        return None
    frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    for frame in frames:
        try:
            driver.switch_to.frame(frame)
        except WebDriverException:
            continue
        found = _find_field_here(driver, label, depth + 1)
        if found is not None:
            return found
        try:
            driver.switch_to.parent_frame()
        except WebDriverException:
            driver.switch_to.default_content()
    return None


def _file_input(driver: webdriver.Chrome):
    deadline = time.time() + 20
    while time.time() < deadline:
        driver.switch_to.default_content()
        found = _file_input_here(driver, 0)
        if found is not None:
            return found
        time.sleep(0.3)
    driver.switch_to.default_content()
    return None


def _file_input_here(driver: webdriver.Chrome, depth: int):
    inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
    if inputs:
        return inputs[-1]
    if depth >= 3:
        return None
    frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    for frame in frames:
        try:
            driver.switch_to.frame(frame)
        except WebDriverException:
            continue
        found = _file_input_here(driver, depth + 1)
        if found is not None:
            return found
        try:
            driver.switch_to.parent_frame()
        except WebDriverException:
            driver.switch_to.default_content()
            return None
    return None


def _shown(element) -> bool:
    try:
        return element.is_displayed()
    except WebDriverException:
        return False


def _password_field(driver: webdriver.Chrome):
    def pick(elements):
        visible = [item for item in elements if _shown(item) and item.size.get("width", 0) > 2]
        return visible[0] if visible else None

    driver.switch_to.default_content()
    found = pick(driver.find_elements(By.CSS_SELECTOR, "input[type='password']"))
    if found is not None:
        return found
    frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    for frame in frames:
        driver.switch_to.default_content()
        try:
            driver.switch_to.frame(frame)
        except WebDriverException:
            continue
        found = pick(driver.find_elements(By.CSS_SELECTOR, "input[type='password']"))
        if found is not None:
            return found
    driver.switch_to.default_content()
    return None


def _first_text_input(driver: webdriver.Chrome):
    script = """
    for (const el of document.querySelectorAll("input")) {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (type !== "text" && type !== "email") continue;
      if (!el.getClientRects().length) continue;
      return el;
    }
    return null;
    """
    driver.switch_to.default_content()
    found = driver.execute_script(script)
    if found is not None:
        return found
    frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    for frame in frames:
        driver.switch_to.default_content()
        try:
            driver.switch_to.frame(frame)
        except WebDriverException:
            continue
        found = driver.execute_script(script)
        if found is not None:
            return found
    driver.switch_to.default_content()
    return None


def _page_has(driver: webdriver.Chrome, text: str) -> bool:
    return _norm(text) in _norm(_visible_text(driver))


def _row_visible(driver: webdriver.Chrome, filename: str) -> bool:
    text = _norm(_visible_text(driver))
    return _norm(filename) in text or _norm(Path(filename).stem) in text


def _visible_text(driver: webdriver.Chrome) -> str:
    driver.switch_to.default_content()
    parts: list[str] = []
    _collect_text(driver, parts, 0)
    driver.switch_to.default_content()
    return "\n".join(parts)


def _collect_text(driver: webdriver.Chrome, parts: list[str], depth: int) -> None:
    parts.append(_body_text(driver))
    if depth >= 3:
        return
    frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    for frame in frames:
        try:
            driver.switch_to.frame(frame)
        except WebDriverException:
            continue
        _collect_text(driver, parts, depth + 1)
        try:
            driver.switch_to.parent_frame()
        except WebDriverException:
            driver.switch_to.default_content()
            return


def _body_text(driver: webdriver.Chrome) -> str:
    try:
        return driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    except WebDriverException:
        return ""


def _error_text(driver: webdriver.Chrome) -> str:
    script = """
    const out = [];
    for (const node of document.querySelectorAll(".dijitDialog, [role='alert'], .dijitToaster")) {
      const style = getComputedStyle(node);
      if (style.display === "none" || style.visibility === "hidden" || !node.getClientRects().length) continue;
      const text = (node.innerText || "").replace(/\\s+/g, " ").trim();
      if (/erreur|obligatoire|invalide|required|echec|échec/i.test(text)) out.push(text.slice(0, 240));
    }
    return out;
    """
    found = []
    driver.switch_to.default_content()
    found.extend(driver.execute_script(script) or [])
    frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    for frame in frames:
        driver.switch_to.default_content()
        try:
            driver.switch_to.frame(frame)
        except WebDriverException:
            continue
        found.extend(driver.execute_script(script) or [])
    driver.switch_to.default_content()
    return found[0] if found else ""


def _chrome() -> webdriver.Chrome:
    """Headless on the server: a disconnected remote desktop makes a visible window stall."""
    EPARAPHEUR_PROFILE.mkdir(parents=True, exist_ok=True)
    options = Options()
    options.page_load_strategy = "eager"
    chrome_bin = os.getenv("CHROME_BIN", "").strip()
    if chrome_bin:
        options.binary_location = chrome_bin
    options.add_argument(f"--user-data-dir={EPARAPHEUR_PROFILE}")
    options.add_argument("--window-size=1440,900")
    headless = os.getenv("SIGNIN_IN_BROWSER", "").strip().lower() in ("1", "true", "yes") or bool(os.getenv("IN_CONTAINER"))
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-dev-shm-usage")
    else:
        options.add_argument("--window-position=40,40")
    if os.getenv("IN_CONTAINER"):
        options.add_argument("--no-sandbox")
    # Ersal is on the internal network. Chrome keeps the Windows proxy settings, including
    # the bypass list. Forcing HTTP_PROXY would send this host through the GitHub proxy.
    options.accept_insecure_certs = True
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-search-engine-choice-screen")
    options.add_experimental_option(
        "prefs",
        {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
            "download.prompt_for_download": False,
            "plugins.always_open_pdf_externally": True,
        },
    )
    driver_path = os.getenv("CHROMEDRIVER", "").strip() or SeleniumManager().binary_paths(
        ["--browser", "chrome", "--skip-driver-in-path"]
    )["driver_path"]
    driver = webdriver.Chrome(service=Service(driver_path), options=options)
    driver.set_page_load_timeout(120)
    if headless:
        agent = driver.execute_script("return navigator.userAgent").replace("HeadlessChrome", "Chrome")
        driver.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": agent})
    return driver


def _page_clue(driver: webdriver.Chrome) -> str:
    """Where Chrome actually is, so a failed open says more than a blank error."""
    try:
        url = driver.current_url or ""
        title = driver.title or ""
    except WebDriverException:
        url, title = "", ""
    text = " ".join(_visible_text(driver).split())[:120]
    parts = [part for part in (url, title, text) if part and part != "about:blank"]
    return " · ".join(parts)[:220] or "a blank page"


def _snap(driver: webdriver.Chrome) -> None:
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
        driver.save_screenshot(str(SHOTS / "eparapheur.png"))
    except Exception:
        pass


def _readable(exc: Exception) -> str:
    if isinstance(exc, RuntimeError) and exc.args:
        return str(exc)[:300]
    text = ""
    if isinstance(exc, WebDriverException):
        text = ((exc.msg or str(exc)).splitlines() or [""])[0].removeprefix("Message:").strip()
    low = text.casefold()
    if any(part in low for part in ("session not created", "failed to start", "chrome not reachable", "cannot find chrome")):
        return "Chrome could not open on this computer. Keep the Windows session signed in, then try again."
    if isinstance(exc, TimeoutException) or "timed out" in low or "timeout" in low:
        return "Ersal took too long to open. Try again."
    if text:
        return text[:240]
    return "The send to Eparapheur stopped unexpectedly."


def _load() -> dict:
    empty = {"username": "", "password": "", "company": "", "provider": "", "approver": "", "sent": {}}
    try:
        data = json.loads(EPARAPHEUR.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(data, dict):
        return empty
    data.setdefault("sent", {})
    return data


def _save(data: dict) -> None:
    temporary = EPARAPHEUR.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(EPARAPHEUR)


def _norm(value: str) -> str:
    text = unicodedata.normalize("NFD", value or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"[^a-z0-9]+", " ", text.casefold())
    return " ".join(text.split())


def _has(text: str, word: str) -> bool:
    return f" {word} " in f" {text} "
