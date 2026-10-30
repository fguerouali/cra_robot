from __future__ import annotations

import sys
import time
import unicodedata
from calendar import month_name
from difflib import get_close_matches
from shutil import rmtree
from pathlib import Path
from re import sub

from pypdf import PdfWriter
from paths import DOWNLOADS
from signature import has_signature, sign_pdf
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchWindowException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

SIGNED_IN_TEXT = "CRA AttijariView"
SIGN_IN_URLS = ("login.microsoftonline.com", "login.live.com", "login.windows.net", "app.powerbi.com/singleSignOn")

RESOURCE_FILTER = "Ressource"
MONTH_FILTER = "Mois_Année"
WEEK_FILTER = "Num_Semaine"
FILTER_LABELS = {
    RESOURCE_FILTER: "the resource list",
    MONTH_FILTER: "the month list",
    WEEK_FILTER: "the week list",
}
PAGE_MENSUEL = "Mensuel"
PAGE_HEBDOMADAIRE = "Hebdomadaire"
PAGE_LABELS = {PAGE_MENSUEL: "monthly", PAGE_HEBDOMADAIRE: "weekly"}
PDF_BUTTON = (By.ID, "pdf")
CURRENT_PAGE = (By.CSS_SELECTOR, 'input[aria-label="Only export current page"]')
CLEAR_BUTTON_XPATH = (
    ".//button[@aria-label='Clear filter' and not(@disabled) and not(@aria-disabled='true')]"
)


def say(message: str) -> None:
    """A progress line for the person running the export (shown in the app's logs)."""
    print(message, flush=True)


def debug(message: str) -> None:
    """Technical detail for the terminal only."""
    print(message, file=sys.stderr, flush=True)


def month_label(month_value: str) -> str:
    """'07/2026' -> 'July 2026'."""
    month, _, year = month_value.partition("/")
    return f"{month_name[int(month)]} {year}" if month.isdigit() and 1 <= int(month) <= 12 else month_value


def workflow(
    driver: WebDriver,
    secrets: dict,
    resource_name: str,
    month_value: str,
    stamp: dict | None = None,
) -> list[Path]:
    output_folder = _output_folder(resource_name, month_value)
    file_stem = _file_stem(resource_name, month_value)
    started = time.time()
    try:
        month = month_label(month_value)
        say("Opening Power BI")
        driver.get("https://app.powerbi.com/home")
        _wait_for_sign_in(driver)
        _open_cra_report(driver)
        _open_report_page(driver, PAGE_MENSUEL)
        _set_resource_month(driver, resource_name, month_value)
        say(f"Showing {resource_name} for {month}")
        say("Downloading the monthly PDF")
        saved = [_export_current_page(driver, output_folder, file_stem)]
        _open_report_page(driver, PAGE_HEBDOMADAIRE)
        _set_resource_month(driver, resource_name, month_value)
        week_values = _week_values(driver)
        say(f"Found {len(week_values)} week{'s' if len(week_values) != 1 else ''} in {month}")
        weeks = []
        for number, week in enumerate(week_values, start=1):
            _select_only_week(driver, week)
            say(f"Downloading week {week} ({number} of {len(week_values)})")
            weeks.append(_export_current_page(driver, output_folder, file_stem, week))
        if len(weeks) > 1:
            say(f"Combining the {len(weeks)} weekly PDFs into one")
        saved.append(_merge_weeks(weeks, output_folder, file_stem))
        if has_signature():
            say("Adding your signature to the monthly PDF")
            options = stamp or {}
            sign_pdf(
                saved[0],
                name=str(options.get("signer_name") or resource_name),
                include_name=options.get("include_name", True),
                include_datetime=options.get("include_datetime", True),
            )
        for path in saved:
            debug(f"Saved {path.name}")
        return saved
    finally:
        _cleanup_temporary_files(output_folder, started)


class SignInRequired(RuntimeError):
    """Power BI showed a sign-in page, which an export cannot get past on its own."""


def sign_in_requested(driver: WebDriver) -> bool:
    url = driver.current_url
    if any(part in url for part in SIGN_IN_URLS):
        return True
    return bool(driver.find_elements(By.CSS_SELECTOR, "input[name='loginfmt'], input[type='email']"))


def _wait_for_sign_in(driver: WebDriver, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sign_in_requested(driver):
            raise SignInRequired("Power BI is logged out. Sign in from the app, then run again.")
        body = driver.execute_script("return document.body ? document.body.innerText : ''")
        if body is None:
            # Chromedriver answers with nothing once the window is gone.
            raise NoSuchWindowException("The browser window was closed")
        if SIGNED_IN_TEXT in body:
            say("Connected to Power BI")
            return
        time.sleep(1)
    raise RuntimeError("Power BI took too long to open. Try again.")


def _cleanup_temporary_files(output_folder: Path, started: float) -> None:
    week_folder = output_folder / ".weeks"
    if week_folder.exists():
        rmtree(week_folder)
    remove_empty_folders(output_folder)
    for pattern in ("*.crdownload", "*.tmp"):
        for path in DOWNLOADS.glob(pattern):
            path.unlink(missing_ok=True)
    for path in DOWNLOADS.glob("*.pdf"):
        if path.stat().st_mtime >= started - 1:
            path.unlink(missing_ok=True)


def remove_empty_folders(folder: Path) -> None:
    """Remove `folder` and its parents up to DOWNLOADS while they hold nothing but Finder metadata."""
    root = DOWNLOADS.resolve()
    folder = folder.resolve()
    while folder != root and root in folder.parents and folder.is_dir():
        if any(child.name != ".DS_Store" for child in folder.iterdir()):
            return
        (folder / ".DS_Store").unlink(missing_ok=True)
        folder.rmdir()
        folder = folder.parent


def _open_cra_report(driver: WebDriver, timeout: float = 60) -> None:
    """Open the report from the home page body: Recommended card first, then the Name table row."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sign_in_requested(driver):
            raise SignInRequired("Power BI is logged out. Sign in from the app, then run again.")
        target = _cra_home_link(driver)
        if target is not None:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target)
            try:
                _click(target, "the CRA AttijariView report")
            except RuntimeError:
                try:
                    _js_click(driver, target, "the CRA AttijariView report")
                except RuntimeError:
                    time.sleep(0.4)
                    continue
            try:
                WebDriverWait(driver, 8).until(_cra_report_opened)
                return
            except TimeoutException:
                pass
        _scroll_home(driver)
        time.sleep(0.4)
    raise RuntimeError("Could not click the CRA AttijariView report. It did not appear in time.")


def _cra_report_opened(driver: WebDriver) -> bool:
    if driver.find_elements(By.XPATH, f"//mat-action-list/button[normalize-space()='{PAGE_MENSUEL}']"):
        return True
    path = (driver.current_url or "").split("?", 1)[0].casefold().rstrip("/")
    return not path.endswith("/home")


def _scroll_home(driver: WebDriver) -> None:
    driver.execute_script(
        "const main = document.querySelector('main, [role=\"main\"]') || document.scrollingElement;"
        "if (main) main.scrollBy(0, Math.max(main.clientHeight || 0, 360));"
    )


def _cra_home_link(driver: WebDriver) -> WebElement | None:
    try:
        return driver.execute_script(
            """
            const name = arguments[0];
            const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
            const firstLine = (el) => norm((el.innerText || el.textContent || '').split('\\n')[0]);
            const isNav = (el) => !!(el.closest('nav, [id*="navbar"], [id*="Navbar"], [id*="leftNav"], [id*="LeftNav"]'));
            const visible = (el) => {
              if (!el) return false;
              const r = el.getBoundingClientRect();
              const st = getComputedStyle(el);
              return r.width > 4 && r.height > 4 && st.visibility !== 'hidden' && st.display !== 'none';
            };
            const labeled = (el) => {
              const title = norm(el.getAttribute('title'));
              const label = norm(el.getAttribute('aria-label'));
              const line = firstLine(el);
              return title === name || label === name || line === name;
            };
            const headingAbove = (el) => {
              const top = el.getBoundingClientRect().top;
              let best = '', dist = Infinity;
              document.querySelectorAll('h1, h2, h3, h4, [role="heading"]').forEach((h) => {
                const ht = h.getBoundingClientRect().top;
                if (ht <= top + 8) {
                  const d = top - ht;
                  if (d < dist) { dist = d; best = norm(h.textContent); }
                }
              });
              return best;
            };

            const clickables = [...document.querySelectorAll('a, button, [role="link"]')]
              .filter((el) => visible(el) && !isNav(el) && labeled(el));
            const recommended = clickables.find((el) => /recommended/i.test(headingAbove(el)));
            if (recommended) return recommended;
            const inTable = clickables.find((el) => el.closest('[role="grid"], [role="table"], table'));
            if (inTable) return inTable;

            const rows = [...document.querySelectorAll('[role="row"], tr')].filter((row) => {
              if (isNav(row) || !visible(row)) return false;
              return [...row.querySelectorAll('[role="gridcell"], td, a, span')].some((cell) => firstLine(cell) === name);
            });
            if (rows.length) {
              const link = rows[0].querySelector('a, [role="link"], button');
              if (link && visible(link)) return link;
              return rows[0];
            }
            return clickables[0] || null;
            """,
            SIGNED_IN_TEXT,
        )
    except WebDriverException:
        return None


def _click(element: WebElement, name: str) -> None:
    try:
        element.click()
    except (ElementClickInterceptedException, ElementNotInteractableException, StaleElementReferenceException) as exc:
        raise RuntimeError(f"Could not click {name}.") from exc


def _js_click(driver: WebDriver, element: WebElement, name: str) -> None:
    try:
        driver.execute_script("arguments[0].click()", element)
    except WebDriverException as exc:
        raise RuntimeError(f"Could not click {name}.") from exc


def _wait_click(driver: WebDriver, locator: tuple, name: str, timeout: float = 30) -> WebElement:
    try:
        element = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable(locator))
    except TimeoutException:
        raise RuntimeError(f"Could not click {name}. It did not appear in time.") from None
    _click(element, name)
    return element


def _open_report_page(driver: WebDriver, name: str) -> None:
    locator = (By.XPATH, f"//mat-action-list/button[normalize-space()='{name}']")
    _wait_click(driver, locator, f"the {PAGE_LABELS.get(name, name)} report", 30)
    WebDriverWait(driver, 30).until(lambda d: (d.find_element(*locator).get_attribute("aria-selected") or "").lower() == "true")
    WebDriverWait(driver, 60).until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.filterPaneModern")))
    say(f"Opened the {PAGE_LABELS.get(name, name)} report")


def _export_current_page(
    driver: WebDriver,
    output_folder: Path,
    file_stem: str,
    week: str | None = None,
) -> Path:
    DOWNLOADS.mkdir(exist_ok=True)
    try:
        pdf_button = WebDriverWait(driver, 60).until(EC.presence_of_element_located(PDF_BUTTON))
    except TimeoutException:
        raise RuntimeError("Could not click the Export to PDF button. It did not appear in time.") from None
    _js_click(driver, pdf_button, "the Export to PDF button")
    try:
        checkbox = WebDriverWait(driver, 20).until(EC.presence_of_element_located(CURRENT_PAGE))
    except TimeoutException:
        raise RuntimeError("Could not click the current page option. It did not appear in time.") from None
    if (checkbox.get_attribute("aria-checked") or "").lower() != "true":
        _js_click(driver, checkbox, "the current page option")
        WebDriverWait(driver, 10).until(
            lambda d: (d.find_element(*CURRENT_PAGE).get_attribute("aria-checked") or "").lower() == "true"
        )
    started = time.time()
    _click(_export_button(driver), "the Export button")
    debug(f"Export started for {f'week {week}' if week else 'the month'}")
    saved = _place_pdf(_wait_for_pdf(started), output_folder, file_stem, week)
    driver.switch_to.active_element.send_keys(Keys.ESCAPE)
    return saved


def _export_button(driver: WebDriver) -> WebElement:
    checkbox = driver.find_element(*CURRENT_PAGE)
    container = checkbox.find_element(By.XPATH, "ancestor::*[.//button[normalize-space()='Export']][1]")
    for button in container.find_elements(By.XPATH, ".//button[normalize-space()='Export']"):
        if button.is_displayed():
            return button
    raise RuntimeError("Power BI's export window did not show an Export button. Try again in a moment.")


def _safe_segment(value: str) -> str:
    cleaned = sub(r"[^\w .-]+", "_", value, flags=0).strip(" .")
    return cleaned or "unknown"


def _output_folder(resource_name: str, month_value: str) -> Path:
    """One folder per resource and month; re-running the same month refreshes its files."""
    month = month_value.replace("/", "-").replace("\\", "-")
    return DOWNLOADS / _safe_segment(resource_name) / month


def _file_stem(resource_name: str, month_value: str) -> str:
    month = month_value.replace("/", "-").replace("\\", "-")
    return f"CRA_{_safe_segment(resource_name)}_{month}"


def _place_pdf(source: Path, output_folder: Path, file_stem: str, week: str | None = None) -> Path:
    if week:
        folder = output_folder / ".weeks"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{week}.pdf"
    else:
        output_folder.mkdir(parents=True, exist_ok=True)
        target = output_folder / f"{file_stem}.pdf"
    if target.exists():
        target.unlink()
    source.rename(target)
    return target


def _merge_weeks(pages: list[Path], output_folder: Path, file_stem: str) -> Path:
    target = output_folder / f"{file_stem}_hebdo.pdf"
    _merge_pdfs(pages, target)
    for page in pages:
        page.unlink()
    week_folder = pages[0].parent if pages else None
    if week_folder is not None and week_folder.name == ".weeks":
        week_folder.rmdir()
    return target


def _merge_pdfs(sources: list[Path], target: Path) -> None:
    writer = PdfWriter()
    for source in sources:
        writer.append(str(source))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    with target.open("wb") as handle:
        writer.write(handle)
    writer.close()


def _set_resource_month(driver: WebDriver, resource_name: str, month_value: str) -> None:
    """Clear each filter with its own eraser, then pick resource and month."""
    for name in (WEEK_FILTER, MONTH_FILTER, RESOURCE_FILTER):
        _clear_filter(driver, name)
    _select_only_filter_value(driver, RESOURCE_FILTER, resource_name, search=True)
    _clear_filter(driver, MONTH_FILTER)
    _clear_filter(driver, WEEK_FILTER)
    _select_only_filter_value(driver, MONTH_FILTER, month_value, search=False)
    _clear_filter(driver, WEEK_FILTER)


def _clear_filter_button(card: WebElement | None) -> WebElement | None:
    """That filter card's own eraser, only when Power BI has it turned on."""
    if card is None:
        return None
    try:
        for button in card.find_elements(By.XPATH, CLEAR_BUTTON_XPATH):
            if button.is_displayed() and button.is_enabled():
                return button
    except StaleElementReferenceException:
        return None
    return None


def _clear_filter(driver: WebDriver, filter_name: str) -> None:
    """Click this filter's Clear button. If it is greyed out, the card is already (All)."""
    label = FILTER_LABELS.get(filter_name, filter_name)
    WebDriverWait(driver, 20).until(lambda d: _filter_card(d, filter_name))
    card = _expand_filter(driver, filter_name)
    button = _clear_filter_button(card)
    if button is None:
        debug(f"{filter_name}: already all")
        return
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", button)
    try:
        _click(button, f"Clear on {label}")
    except RuntimeError:
        _js_click(driver, button, f"Clear on {label}")
    WebDriverWait(driver, 20).until(lambda d: _clear_filter_button(_filter_card(d, filter_name)) is None)
    debug(f"{filter_name}: cleared")


def _week_values(driver: WebDriver) -> list[str]:
    seen: list[str] = []
    stable = 0
    region = None
    for _ in range(12):
        card = _expand_filter(driver, WEEK_FILTER)
        if region is None:
            regions = card.find_elements(By.CSS_SELECTOR, ".scrollRegion")
            region = regions[0] if regions else None
            if region is not None:
                driver.execute_script("arguments[0].scrollTop = 0", region)
        before = len(seen)
        for text, _, _ in _week_rows(driver):
            if text and text.casefold() != "select all" and text not in seen:
                seen.append(text)
        if region is not None:
            driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollTop + Math.max(arguments[0].clientHeight, 40)",
                region,
            )
        stable = stable + 1 if seen and len(seen) == before else 0
        if stable >= 2:
            break
        time.sleep(0.4)
    if not seen:
        raise RuntimeError("The weekly report lists no weeks for this month.")
    return sorted(seen, key=lambda value: int(value) if value.isdigit() else value)


def _select_only_week(driver: WebDriver, week: str) -> None:
    try:
        _select_only_filter_value(driver, WEEK_FILTER, week, search=False)
    except RuntimeError as exc:
        if "not listed" in str(exc).casefold() or "not available" in str(exc).casefold():
            raise RuntimeError(f"Week {week} disappeared from the weekly report during the export. Try again.") from None
        raise


def _week_rows(driver: WebDriver) -> list[tuple[str, str, WebElement]]:
    return _filter_item_rows(driver, WEEK_FILTER)


def _wait_for_pdf(started: float) -> Path:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        pending = list(DOWNLOADS.glob("*.crdownload")) + list(DOWNLOADS.glob("*.tmp"))
        fresh = [path for path in DOWNLOADS.glob("*.pdf") if path.stat().st_mtime >= started - 1]
        if fresh and not pending:
            return max(fresh, key=lambda path: path.stat().st_mtime)
        time.sleep(0.5)
    raise TimeoutError("The PDF did not arrive within 3 minutes of starting the export.")


def _select_only_filter_value(driver: WebDriver, filter_name: str, value: str, search: bool) -> None:
    """Clear this filter with its own eraser, then click `value` so it is the only check."""
    label = FILTER_LABELS.get(filter_name, filter_name)
    _clear_filter(driver, filter_name)
    card = _expand_filter(driver, filter_name)
    if search:
        _search_filter(card, max(value.split(), key=len))
    try:
        row = WebDriverWait(driver, 20).until(lambda d: _value_row(_filter_card(d, filter_name), value))
    except TimeoutException:
        if filter_name == MONTH_FILTER:
            raise RuntimeError(f"{month_label(value)} is not available in the report.") from None
        hint = _similar_values(driver, filter_name, value) if search else []
        detail = f" Similar names: {', '.join(hint)}." if hint else ""
        raise RuntimeError(f"This name is not listed in the report. Check the spelling in Resources.{detail}") from None
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", row)
    try:
        _click(row, f"{value} in {label}")
    except RuntimeError:
        row = _value_row(_filter_card(driver, filter_name), value)
        if row is None:
            raise
        _js_click(driver, row, f"{value} in {label}")
    if search:
        _clear_search(_filter_card(driver, filter_name))
    try:
        WebDriverWait(driver, 20).until(lambda d: _clear_filter_button(_filter_card(d, filter_name)) is not None)
    except TimeoutException:
        raise RuntimeError(f"Could not select only {value} in {label}.") from None
    debug(f"{filter_name}: selected '{value}'")


def _filter_item_rows(driver: WebDriver, filter_name: str) -> list[tuple[str, str, WebElement]]:
    try:
        card = _expand_filter(driver, filter_name)
        rows = []
        for row in card.find_elements(By.CSS_SELECTOR, "div.slicerItemContainer"):
            labels = row.find_elements(By.CSS_SELECTOR, "span.slicerText")
            if not labels:
                continue
            rows.append((labels[0].text.strip(), (row.get_attribute("aria-checked") or "").lower(), row))
        return rows
    except StaleElementReferenceException:
        return []


def _expand_filter(driver: WebDriver, filter_name: str) -> WebElement:
    card = _filter_card(driver, filter_name)
    if card.find_elements(By.CSS_SELECTOR, ".cardExpanded"):
        return card
    button = card.find_element(By.CSS_SELECTOR, 'button[aria-label$="Expand or collapse filter card"]')
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", button)
    _click(button, FILTER_LABELS.get(filter_name, filter_name))
    WebDriverWait(driver, 20).until(lambda d: _filter_card(d, filter_name).find_elements(By.CSS_SELECTOR, ".cardExpanded"))
    return _filter_card(driver, filter_name)


def _filter_card(driver: WebDriver, filter_name: str) -> WebElement:
    target = filter_name.casefold()
    try:
        for card in driver.find_elements(By.CSS_SELECTOR, "div.filterPaneModern div.card"):
            for label in card.find_elements(By.CSS_SELECTOR, ".cardHeader .textLabel"):
                if label.text.strip().casefold() == target:
                    return card
    except StaleElementReferenceException:
        return None
    return None


def _value_row(card: WebElement, value: str) -> WebElement | None:
    if card is None:
        return None
    try:
        target = _normalized(value)
        for row in card.find_elements(By.CSS_SELECTOR, "div.slicerItemContainer"):
            labels = row.find_elements(By.CSS_SELECTOR, "span.slicerText")
            if labels and _normalized(labels[0].text) == target:
                return row
    except StaleElementReferenceException:
        return None
    return None


def _normalized(text: str) -> str:
    """Case-, accent- and spacing-insensitive form, so 'Faycal  GUEROUALI' matches 'Fayçal Guerouali'."""
    decomposed = unicodedata.normalize("NFKD", text)
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).split()).casefold()


def _search_filter(card: WebElement, text: str) -> None:
    """Type `text` into the card's search box, which must be empty first or the search is wrong."""
    driver = card.parent
    box = _clear_search(card)
    if box is None:
        raise RuntimeError("The report's resource list has no search box.")
    box.send_keys(text)
    WebDriverWait(driver, 5).until(lambda d: box.get_attribute("value") == text)


def _clear_search(card: WebElement | None) -> WebElement | None:
    """Empty the card's search box and confirm it is empty; returns the box (None if the card has none)."""
    boxes = card.find_elements(By.CSS_SELECTOR, "input.searchInput") if card is not None else []
    if not boxes:
        return None
    driver, box = card.parent, boxes[0]
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", box)
    _click(box, "the search field")
    for _ in range(3):
        current = box.get_attribute("value") or ""
        if not current:
            break
        box.send_keys(Keys.END, Keys.BACKSPACE * len(current))
    if box.get_attribute("value"):
        # Typing did not clear it; set the value the way the page's own input handler expects.
        driver.execute_script(
            "const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;"
            "set.call(arguments[0], ''); arguments[0].dispatchEvent(new Event('input', {bubbles: true}));",
            box,
        )
    WebDriverWait(driver, 5).until(lambda d: not box.get_attribute("value"))
    return box


def _similar_values(driver: WebDriver, filter_name: str, value: str, limit: int = 3) -> list[str]:
    """Names the filter does list for each word of `value`, closest first."""
    found: dict[str, str] = {}
    for word in sorted({w for w in value.split() if len(w) >= 3}, key=len, reverse=True):
        _search_filter(_expand_filter(driver, filter_name), word)
        time.sleep(1.5)
        for label in _filter_card(driver, filter_name).find_elements(By.CSS_SELECTOR, "div.slicerItemContainer span.slicerText"):
            text = label.text.strip()
            if text and text.casefold() != "select all":
                found.setdefault(_normalized(text), text)
    _clear_search(_filter_card(driver, filter_name))
    close = get_close_matches(_normalized(value), list(found), n=limit, cutoff=0.4)
    return [found[key] for key in close] or list(found.values())[:limit]
