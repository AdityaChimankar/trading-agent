import asyncio
from playwright.async_api import async_playwright

async def test():
    kite = __import__('kiteconnect').KiteConnect(api_key='ucnclpnzvt5vu93q')
    login_url = kite.login_url()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        
        print('Navigating...')
        await page.goto(login_url, wait_until='networkidle')
        await page.wait_for_timeout(3000)
        
        # Try different selectors
        selectors = [
            'input[name="userid"]',
            'input#userid',
            'input[type="text"]',
            'input[name*="user"]',
            'form input',
        ]
        
        for sel in selectors:
            locator = page.locator(sel)
            count = await locator.count()
            print(f'Selector "{sel}": count={count}')
        
        # Check the actual HTML
        content = await page.content()
        # Find the input elements in HTML
        import re
        inputs_html = re.findall(r'<input[^>]*>', content)
        for inp in inputs_html:
            print('HTML input:', inp[:200])
        
        await browser.close()

asyncio.run(test())