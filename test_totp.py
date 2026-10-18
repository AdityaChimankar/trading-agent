from playwright.sync_api import sync_playwright

kite = __import__('kiteconnect').KiteConnect(api_key='ucnclpnzvt5vu93q')
login_url = kite.login_url()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    page.goto(login_url, wait_until='networkidle')
    page.wait_for_timeout(3000)
    
    # Check buttons
    buttons = page.query_selector_all('button')
    for btn in buttons:
        print('Button:', btn.get_attribute('type'), btn.inner_text())
    
    # Fill user_id and password
    page.fill('input#userid', 'test')
    page.fill('input#password', 'test')
    
    # Try clicking login button
    page.click('button[type="submit"]')
    page.wait_for_timeout(5000)
    
    # Check what's on page now
    print('URL after click:', page.url)
    inputs = page.query_selector_all('input')
    for inp in inputs:
        print('Input:', inp.get_attribute('type'), inp.get_attribute('name'), inp.get_attribute('id'))
    
    browser.close()