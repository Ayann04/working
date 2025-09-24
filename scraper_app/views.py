import os
import re
import time
import logging
from io import BytesIO
from datetime import datetime
import shutil
import uuid
from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
import traceback
from PIL import Image
from openpyxl import Workbook

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from .models import ScrapedRecord,ScrapingRun,ScrapingStatus


driver = None
# Configurable constants
DEFAULT_WAIT = int(getattr(settings, "SELENIUM_DEFAULT_WAIT",500))
CAPTCHA_WAIT_SECONDS = int(getattr(settings, "CAPTCHA_WAIT_SECONDS", 180))
# Cache key template for per-run CAPTCHA values
CAPTCHA_CACHE_KEY = "captcha:run:{run_id}:{captcha_key}"

timestamp_2 = datetime.now().strftime("%Y%m%d_%H%M%S")
ScrapedRecord.objects.all().delete()

def get_status(request):
    global driver
    """
    Renders current scraping status for the latest run.
    Accepts POST with 'captcha_value' + 'captcha_key' to feed the scraping flow.
    """
    latest_run = ScrapingRun.objects.order_by("-started_at").first()
    if latest_run:
        statuses = latest_run.statuses.order_by("created_at")
        # latest status that has a captcha image for this run
        captcha_status = (
            latest_run.statuses.filter(captcha_image__isnull=False)
            .order_by("-created_at")
            .first()
        )
    else:
        statuses = []
        captcha_status = None

    latest_status = ScrapingStatus.objects.order_by("-created_at").first()

    captcha_value = None
    if request.method == "POST":
        captcha_value = (request.POST.get("captcha_value") or "").strip()
        captcha_key = (request.POST.get("captcha_key") or "").strip()
        if captcha_value and latest_run and captcha_key:
            cache.set(CAPTCHA_CACHE_KEY.format(run_id=latest_run.id, captcha_key=captcha_key), captcha_value, timeout=300)
        elif not captcha_value:
            print("Exception occurred:")
            traceback.print_exc()

    timestamp = int(time.time())
    return render(
        request,
        "scraper_app/status.html",
        {
            "latest_run": latest_run,
            "statuses": statuses,
            "status": latest_status,
            "captcha_status": captcha_status,
            "captcha_value": captcha_value,
            "timestamp": timestamp,
        },
    )


def parse_address(addr: str):
    global driver
    parsed = {}
    patterns = {
        "Ward/Colony": r"Ward Colony\s*-\s*([^,\.]+)",
        "District": r"Distirct:?\s*([^,\.]+)",
        "Village": r"Village:?\s*([^,\.]+)",
        "Sub-Area/Road": r"Sub-Area\s*:?\s*([^,\.]+)",
        "Tehsil/Locality": r"Tehsil:?\s*([^,\.]+)",
        "PIN Code": r"pin-?(\d{6})",
        "Landmark": r"(\d+\s*m\s+from\s+[^p]+)",
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, addr, re.IGNORECASE)
        if match:
            parsed[key] = match.group(1) if match.lastindex and match.lastindex >= 1 else ""
        else:
            parsed[key] = ""

    parsed["State"] = "Madhya Pradesh" if "Madhya Pradesh" in addr else ""
    parsed["Country"] = "India" if "India" in addr else ""
    return parsed


def _create_status(run: ScrapingRun, message: str, pil_image: Image.Image | None = None, captcha_key: str | None = None) -> ScrapingStatus:
    global driver
    """
    Create a ScrapingStatus row with optional image stored in captcha_image.
    If captcha_key is provided, it is stored on the status to tie UI input to the right wait.
    """
    status = ScrapingStatus.objects.create(run=run, message=message, captcha_key=captcha_key)
    if pil_image is not None:
        buffer = BytesIO()
        pil_image.save(buffer, format="PNG")
        buffer.seek(0)
        status.captcha_image.save(f"captcha_{int(time.time())}.png", ContentFile(buffer.read()), save=True)
    return status


def _driver_from_config():
    global driver
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.binary_location = os.environ.get("CHROME_BIN")
    service = Service(os.environ.get("CHROMEDRIVER_PATH"))
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver


def _screenshot_element(driver: webdriver.Chrome, element) -> Image.Image:
    """
    Take a full-page screenshot and crop the given element accurately using devicePixelRatio.
    """
    # Ensure visibility
    driver.execute_script("arguments[0].scrollIntoView(true);", element)
    time.sleep(0.5)

    dpr = driver.execute_script("return window.devicePixelRatio") or 1
    png = driver.get_screenshot_as_png()
    image = Image.open(BytesIO(png))
    img_width, img_height = image.size

    location = element.location_once_scrolled_into_view
    size = element.size

    left = max(0, int(location["x"] * dpr))
    top = max(0, int(location["y"] * dpr))
    right = min(img_width, int((location["x"] + size["width"]) * dpr))
    bottom = min(img_height, int((location["y"] + size["height"]) * dpr))

    cropped = image.crop((left, top, right, bottom))
    return cropped


def _wait_for_captcha_value(run_id: int, captcha_key: str, timeout: int = CAPTCHA_WAIT_SECONDS, poll_interval: float = 1.0) -> str | None:
    global driver
    """
    Poll cache for a per-run + per-captcha-key value set via get_status POST.
    """
    key = CAPTCHA_CACHE_KEY.format(run_id=run_id, captcha_key=captcha_key)
    waited = 0
    while waited < timeout:
        value = cache.get(key)
        if value:
            # Clear it so subsequent steps don't reuse stale values
            cache.delete(key)
            return value
        time.sleep(poll_interval)
        waited += poll_interval
    return None


def save_to_db(all_sections):
    global driver
    """
    Persist scraped sections into ScrapedRecord with robust handling.
    """
    try:
        data = {}
        for headings, data_texts in all_sections:
            for heading, value in zip(headings, data_texts):
                data[heading] = value

        ScrapedRecord.objects.create(
            registration_details = dict(zip(all_sections[0][0], all_sections[0][1])),
            seller_details = dict(zip(all_sections[1][0], all_sections[1][1])),
            buyer_details = dict(zip(all_sections[2][0], all_sections[2][1])),
            property_details = dict(zip(all_sections[3][0], all_sections[3][1])),
            khasra_details = dict(zip(all_sections[4][0], all_sections[4][1])),
        )
    except Exception as e:
        print("Exception occurred:")
        traceback.print_exc()


def trigger_scrape(request):
    global driver
    """
    Launch the scraping process. For production, consider moving this to a background worker (Celery/RQ).
    """
    new_run = ScrapingRun.objects.create()
    if request.method != "POST":
        return render(request, "scraper_app/scrape_form.html")

    username = (request.POST.get("username") or "").strip()
    password = (request.POST.get("password") or "").strip()
    district = (request.POST.get("district") or "").strip()
    deed_type = (request.POST.get("deed_type") or "").strip()
    date_too = request.POST.get("date_to")
    date_from = request.POST.get("date_from")

    try:
        date_from_fmt = datetime.strptime(date_from, "%Y-%m-%d").strftime("%d-%m-%Y")
        date_to_fmt = datetime.strptime(date_too, "%Y-%m-%d").strftime("%d-%m-%Y")
        print(date_from_fmt)
    except Exception:
        _create_status(new_run, "Invalid date format. Expected YYYY-MM-DD.")
        return JsonResponse({"message": "Invalid date format. Expected YYYY-MM-DD."}, status=400)

    
    try:
        driver = _driver_from_config()
        driver.get("https://sampada.mpigr.gov.in/#/clogin")
        time.sleep(20)
        english_to = driver.find_elements(By.CSS_SELECTOR,'div.ng-star-inserted>a')
        english_to[2].click()
        _create_status(new_run, "CLICKED ON ENGLISH")
        # Login loop with CAPTCHA #1
        max_attempts = 10
        login_success = False
        _create_status(new_run, "Filling Username And Password To login")
        for attempt in range(max_attempts):
            try:
                driver.refresh()
                WebDriverWait(driver, DEFAULT_WAIT).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input#username"))
                )

                username_input = driver.find_element(By.CSS_SELECTOR, "input#username")
                username_input.send_keys(username)

                password_input = driver.find_element(By.CSS_SELECTOR, "input#password")
                password_input.send_keys(password)

                # CAPTCHA image
                elem = WebDriverWait(driver, DEFAULT_WAIT).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, "div.input-group>img"))
                )

                # Screenshot and ask user to solve
                captcha_img_1 = _screenshot_element(driver, elem)
                captcha_key_1 = f"c1-{uuid.uuid4().hex[:8]}"
                _create_status(new_run, "Please solve CAPTCHA #1 in the UI", pil_image=captcha_img_1, captcha_key=captcha_key_1)

                captcha_value = _wait_for_captcha_value(new_run.id, captcha_key_1, timeout=CAPTCHA_WAIT_SECONDS)
                if not captcha_value:
                    _create_status(new_run, "CAPTCHA #1 timed out waiting for input. Retrying...")
                    continue

                captcha_inputs = driver.find_elements(By.CSS_SELECTOR, "div.input-group>input")
                if len(captcha_inputs) < 3:
                    raise RuntimeError("CAPTCHA input box not found for login form.")

                captcha_inputs[2].click()
                captcha_inputs[2].send_keys(captcha_value)

                # Click login and wait for navigation
                login_button = driver.find_elements(By.CSS_SELECTOR, "button.mat-focus-indicator")
                before_url = driver.current_url
                if len(login_button) >= 2:
                    driver.execute_script("arguments[0].click();", login_button[1])
                else:
                    raise RuntimeError("Login button not found.")

                WebDriverWait(driver, DEFAULT_WAIT).until(EC.url_changes(before_url))
                time.sleep(1.5)  # brief render settle
                after_url = driver.current_url
                if after_url != before_url:
                    login_success = True
                    _create_status(new_run, "Captcha #1 solved successfully; logged in.")
                    break
            except Exception as e:
                print("Exception occurred:")
                traceback.print_exc()
                continue

        if not login_success:
            _create_status(new_run, "Login CAPTCHA solving failed after multiple attempts. Try again.")
            if driver:
                driver.quit()
            return JsonResponse({"message": "Login CAPTCHA solving failed after multiple attempts."}, status=500)
        
        # Navigate to search
        WebDriverWait(driver, DEFAULT_WAIT).until(EC.presence_of_element_located((By.CSS_SELECTOR, "h5.my-0")))
        search_certified = driver.find_elements(By.CSS_SELECTOR, "li.ng-star-inserted>a")
        if len(search_certified) > 2:
            driver.execute_script("arguments[0].click();", search_certified[2])
        else:
            if driver:
                driver.quit()
            return JsonResponse({"message": "Scraping failed: Initial elements not found."}, status=500)

        time.sleep(20)
      
        try:
            driver.refresh()
            time.sleep(10) 
            other_details = driver.find_elements(By.CSS_SELECTOR, 'div.apex-item-option')
            if len(other_details) > 2:
                    other_details[2].click()
            else:
                driver.quit()
                return JsonResponse({"message": "Scraping failed: Other details elements not found."}, status=500)
                
            WebDriverWait(driver, 600).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'input#P2000_FROM_DATE')))
            period_from= driver.find_element(By.CSS_SELECTOR,"input#P2000_FROM_DATE")
            period_from.click()
            period_from.send_keys(date_from_fmt)
            period_to= driver.find_element(By.CSS_SELECTOR,"input#P2000_TO_DATE")
            period_to.send_keys(date_to_fmt)
            time.sleep(5)
            WebDriverWait(driver, 600).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, 'select#P2000_DISTRICT')))
            element = driver.find_element(By.CSS_SELECTOR, 'select#P2000_DISTRICT')
            select_districts = Select(element)
            # Wait until options are actually populated (more than 1 option means loaded)
            WebDriverWait(driver, 200).until(lambda d: len(select_districts.options) > 1)
            # Debug: print options so you know what’s available
            print([opt.text for opt in select_districts.options])
            # Now safely select by visible text
            select_districts.select_by_visible_text(district)
                
            time.sleep(5)
            input_box = driver.find_element(By.XPATH, "//input[@aria-autocomplete='list']")
            time.sleep(5)
            input_box.send_keys(deed_type)
            time.sleep(5)
            input_box.send_keys(Keys.ENTER)
            time.sleep(5)
            captcha_imgs = driver.find_elements(By.CSS_SELECTOR, "div.input-group>img")
            if len(captcha_imgs) < 2:
                raise RuntimeError("CAPTCHA #2 image not found.")
            captcha_img_el = captcha_imgs[1]
            captcha_img_2 = _screenshot_element(driver, captcha_img_el)
            captcha_key_2 = f"c2-{uuid.uuid4().hex[:8]}"
            _create_status(new_run, "Please solve CAPTCHA #2 in the UI", pil_image=captcha_img_2, captcha_key=captcha_key_2)
            time.sleep(10)
            captcha_value_2 = _wait_for_captcha_value(new_run.id, captcha_key_2, timeout=CAPTCHA_WAIT_SECONDS)
            if not captcha_value_2:
                _create_status(new_run, "CAPTCHA #2 timed out waiting for input. Retrying...")
            time.sleep(10)
            captcha_inputs = driver.find_elements(By.CSS_SELECTOR, "div.input-group>input")
            if len(captcha_inputs) < 2:
                raise RuntimeError("CAPTCHA #2 input not found.")
            captcha_inputs[1].click()
            print(captcha_value_2)
            time.sleep(5)
            captcha_inputs[1].send_keys(captcha_value_2)
            _create_status(new_run, "captcha has been filld",)
            time.sleep(5)
            search_button = driver.find_elements(By.CSS_SELECTOR,'div>button.btn')
            search_button[4].click() 
            _create_status(new_run, "search button clicked")   
            time.sleep(100)
            _create_status(new_run, "CAPTCHA #2 solved successfully.")
            
        except Exception as e:
                print("Exception occurred:")
                traceback.print_exc()
                
        while True:  # Keep looping through all pages until no next button
                    _create_status(new_run, "Fetch all record links on current page")
                    max_retries = 5   # how many times to retry if nothing found
                    retries = 0
                    while retries < max_retries:
                        try:
                            time.sleep(10)  # let the page settle (can replace with WebDriverWait)
                            data_elements_2 = driver.find_elements(By.CSS_SELECTOR, 'td.mat-cell>span.link')

                            if len(data_elements_2) == 0:
                                raise ValueError("No records found")  # trigger except block

                            print(f"Found {len(data_elements_2)} records --------------")
                            break   # ✅ exit loop if elements are found

                        except Exception as e:
                            print(f"Attempt {retries+1}: Failed to fetch records ({e})")
                            retries += 1
                            if retries < max_retries:
                                print("Retrying...")
                                time.sleep(5)  # wait before trying again
                            else:
                                print("Max retries reached. Moving on or exiting...")


                    for i in range(len(data_elements_2)):
                        # Re-fetch elements each time (important after navigation/closing modal)
                        data_elements_2 = driver.find_elements(By.CSS_SELECTOR,'td.mat-cell>span.link')

                        if i >= len(data_elements_2):
                            break
                        span = data_elements_2[i]
                        driver.execute_script("arguments[0].click();", span)  # safer than normal click
                        time.sleep(20)
                        Registration_details_data = driver.find_elements(By.XPATH, "//fieldset[legend[contains(text(), 'Registration Details')]]/div/table/tbody/tr/td")
                        Registration_details_heading = driver.find_elements(By.XPATH, "//fieldset[legend[contains(text(), 'Registration Details')]]/div/table/thead/tr/th")
                        headings = [th.text.strip() for th in Registration_details_heading]
                        data_texts = [td.text.strip() for td in Registration_details_data]

                        # Extract Seller
                        seller_data = driver.find_elements(By.XPATH, '//fieldset[legend[contains(text(), "Party From")]]/div/table/tbody/tr/td')
                        seller_heading = driver.find_elements(By.XPATH, "//fieldset[legend[contains(text(), 'Party From')]]/div/table/thead/tr/th")
                        headings_2 = [th.text.strip() for th in seller_heading]
                        data_texts_2 = [td.text.strip() for td in seller_data]

                        # Extract Buyer
                        buyer_data = driver.find_elements(By.XPATH, "//fieldset[legend[contains(text(), 'Party To')]]/div/table/tbody/tr/td")
                        buyer_heading = driver.find_elements(By.XPATH, "//fieldset[legend[contains(text(), 'Party To')]]/div/table/thead/tr/th")
                        headings_3 = [th.text.strip() for th in buyer_heading]
                        data_texts_3 = [td.text.strip() for td in buyer_data]

                        # Extract Property Details
                        property_details = driver.find_elements(By.XPATH, "//fieldset[legend[contains(text(), 'Property Details')]]/div/table/tbody/tr/td")
                        property_heading = driver.find_elements(By.XPATH, "//fieldset[legend[contains(text(), 'Property Details')]]/div/table/thead/tr/th")
                        headings_4 = [th.text.strip() for th in property_heading]
                        data_texts_4 = [td.text.strip() for td in property_details]

                        # Extract Khasra/Building/Plot Details
                        khasra_building_plot_details = driver.find_elements(By.XPATH, "//fieldset[legend[contains(text(), 'Khasra/Building/Plot Details')]]/div/table/tbody/tr/td")
                        khasra_heading = driver.find_elements(By.XPATH, "//fieldset[legend[contains(text(), 'Khasra/Building/Plot Details')]]/div/table/thead/tr/th")
                        headings_5 = [th.text.strip() for th in khasra_heading]
                        data_texts_5 = [td.text.strip() for td in khasra_building_plot_details]

                        # Parse address inside property details
                        final_data_texts_4 = []
                        for heading_100, data in zip(headings_4, data_texts_4):
                            if "address" in heading_100.lower():
                                parsed_addr = parse_address(data)
                                for k, v in parsed_addr.items():
                                    final_data_texts_4.append((k, v))
                            else:
                                final_data_texts_4.append((heading_100, data))

                        headings_4_parsed = [h for h, v in final_data_texts_4]
                        data_texts_4_parsed = [v for h, v in final_data_texts_4]
                        
                        print(headings, data_texts,headings_2, data_texts_2,headings_3, data_texts_3,headings_4_parsed, data_texts_4_parsed,headings_5, data_texts_5)
                        
                        all_sections = [
                            (headings, data_texts),
                            (headings_2, data_texts_2),
                            (headings_3, data_texts_3),
                            (headings_4_parsed, data_texts_4_parsed),
                            (headings_5, data_texts_5),
                        ]

                        # Save to Excel
                        save_to_db(all_sections)

                        # Close popup
                        try:
                            data_elements_200 = driver.find_elements(By.CSS_SELECTOR, 'button.colsebtn')
                            print(data_elements_200)
                            if len(data_elements_200) > 1:
                                data_elements_200[1].click()
                            else:
                                data_elements_200[0].click()
                            time.sleep(3)
                        except:
                            print("Close button not found")
                    
                    time.sleep(10)
                    # --- Pagination Part ---
                    try:
                        next_button = driver.find_element(By.CSS_SELECTOR, "button.mat-paginator-navigation-next")
                        time.sleep(20)
                        if "disabled" in next_button.get_attribute("class"):
                            break
                        else:
                            driver.execute_script("arguments[0].click();", next_button)
                            time.sleep(5)
                    except:
                        break
                    

        _create_status(new_run,"Scraping completed successfully! Go to /get-status/ to review and download from /download-excel/",)
        return JsonResponse({"message": f"Scraping completed successfully! {timestamp_2}"})

    except Exception as e:
        print("Exception occurred:")
        traceback.print_exc()
        _create_status(new_run, "Scraping failed due to an error. Please check logs and try again.")
        return JsonResponse({"message": f"Scraping failed: {e}"}, status=500)
    finally:
        if driver:
            driver.quit()


def clear_logs(request):
    ScrapingStatus.objects.all().delete()
    return JsonResponse({"message": "Logs cleared"})


def download_excel(request):
    global driver
    """
    Export ScrapedRecord to Excel. Handles None JSON fields gracefully.
    """
    records = ScrapedRecord.objects.all()
    wb = Workbook()
    ws = wb.active

    if records.exists():
        first = records.first()
        registration = first.registration_details or {}
        seller = first.seller_details or {}
        buyer = first.buyer_details or {}
        prop = first.property_details or {}
        khasra = first.khasra_details or {}

        headers = list(registration.keys()) + list(seller.keys()) + list(buyer.keys()) + list(prop.keys()) + list(khasra.keys())
        ws.append(headers)

        for r in records:
            row = list((r.registration_details or {}).values()) + \
                  list((r.seller_details or {}).values()) + \
                  list((r.buyer_details or {}).values()) + \
                  list((r.property_details or {}).values()) + \
                  list((r.khasra_details or {}).values())
            ws.append(row)
    else:
        ws.append(["No data"])

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="scraped_data.xlsx"'
    wb.save(response)
    return response
