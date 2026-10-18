from playwright.sync_api import sync_playwright
import os
from dotenv import load_dotenv

load_dotenv()

kite = __import__('kiteconnect').KiteConnect(api_key=os.getenv('KITE_API_KEY'))
login_url = kite.login_url()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    page.goto(login_url, wait_until='networkidle')
    page.wait_for_timeout(3000)
    
    # Fill with real credentials
    page.fill('input#userid', os.getenv('KITE_USER_ID'))
    page.fill('input#password', os.getenv('KITE_PASSWORD'))
    page.click('button[type="submit"]')
    page.wait_for_timeout(5000)
    
    # Check what's on page now
    print('URL after click:', page.url)
    inputs = page.query_selector_all('input')
    for inp in inputs:
        print('Input:', inp.get_attribute('type'), inp.get_attribute('name'), inp.get_attribute('id'))
    
    browser.close()