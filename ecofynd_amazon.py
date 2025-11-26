from flask import Flask, request, jsonify
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time
import re

app = Flask(__name__)

@app.route("/")
def index():
    return "✅ Ecofynd Amazon Scraper API is running!"

@app.route("/scrape", methods=["POST"])
def scrape():
    try:
        data = request.get_json()
        url = data.get("url")

        if not url:
            return jsonify({"error": "Missing 'url' in request body"}), 400

        # Set up headless Chrome
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920x1080')

        driver = webdriver.Chrome(options=chrome_options)

        # Load the page
        driver.get(url)
        time.sleep(3)  # Wait for page to load (Amazon is JS-heavy)

        # Extract the price
        # try:
        #     price = driver.find_element(By.XPATH, '(//span[@class="a-price-whole"])[1]').text
        # except:
        #     price = "Not found"
    # First, check availability
        try:
            availability = driver.find_element(By.ID, "availability").text.strip()
        except:
            availability = ""

        if "Currently unavailable" in availability or "Out of stock" in availability:
            price = "Currently unavailable"

        # If available, extract price from common price locations
        else:
            try:
                price = driver.find_element(By.XPATH, '(//span[@class="a-price-whole"])[1]').text.strip()
            except:
                price =  "Price not found"

        # Extract the title
        try:
            title = driver.find_element(By.ID, 'productTitle').text.strip()
        except:
            title = "Not found"
               
        # Extract ratings  
        try:
            rating_element = driver.find_element(By.XPATH, "(//span[@id='acrPopover'])[1]")
            rating = rating_element.get_attribute("title").strip()  # Example: "4.5 out of 5 stars"
        except:
            rating = "Not found"  

        # ⭐ Extract Review Count (NEW)
        try:
            review_count_el = driver.find_element(By.ID, "acrCustomerReviewText")
            review_text = review_count_el.text.strip()            # e.g. "1,234 ratings"
            review_count = re.sub(r"[^\d]", "", review_text)
        except:
            review_count = "Not found"

        # Check for Limited Time Deal
        try:
            ltd_element = driver.find_element(By.XPATH, '//span[contains(@class,"dealBadgeTextColor")]').text
            limited_deal = "Yes" if ltd_element and "Limited time deal" in ltd_element.text else "No"
        except:
            limited_deal = "Not found"
        
        # Bullet points 
        try:
            bullet_elements = driver.find_elements(By.XPATH, "//div[@id='feature-bullets']//span[@class='a-list-item']")
            bullet_points = len([b.text.strip() for b in bullet_elements if b.text.strip()])
        except:
            bullet_points = []     
            
        # Best Sellers Rank
        try:
            bsr_elements = driver.find_elements(By.XPATH, '//ul[contains(@class, "a-unordered-list") and contains(@class, "a-nostyle")]/li')
            bsr_ranks = [el.text.strip() for el in bsr_elements if "#" in el.text]
        except:
            bsr_ranks = []
           
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(3)                  

        #  A Plus Content   
        try:
            aplus_section = driver.find_element(By.XPATH, '//div[@id="aplus"]')
            aplus_content = aplus_section.text.strip()
            aplus_present = "Yes" if aplus_content else "No"
        except:
            aplus_present = "No"

        # Seller name 
        try:
            seller_element = driver.find_element(By.XPATH, '//a[@id="sellerProfileTriggerId"]')
            seller_name = seller_element.text.strip()
        except:
            try:
                # Fallback for 'Sold by' section in offer info
                seller_element = driver.find_element(By.XPATH, '//div[@id="merchant-info"]')
                seller_name = seller_element.text.strip()
            except:
                seller_name = ""

        driver.quit()

        return jsonify({
            "Title": title,
            "Seller Name": seller_name,
            "Price": price,
            "Rating": rating,
            "Deal Tag": limited_deal,
            "Bullet Points": bullet_points,
            "Best Sellers Rank": bsr_ranks,
            "A Plus Content": aplus_present,
            "Review Count": review_count                         
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
