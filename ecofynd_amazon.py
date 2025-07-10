from flask import Flask, request, jsonify
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
import time

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
        try:
            price = driver.find_element(By.XPATH, '(//span[@class="a-price-whole"])[1]').text
        except:
            price = "Not found"

        # Extract the title (optional)
        try:
            title = driver.find_element(By.ID, 'productTitle').text.strip()
        except:
            title = "Not found"
            
        # Extract Limited Time Deal
        try:
            LimitedDeal = driver.find_element(By.XPATH, '//span[@class="a-size-small dealBadgeTextColor a-text-bold"]').text.strip()
        except:
            LimitedDeal = "Not found"

        driver.quit()

        return jsonify({
            "title": title,
            "price": price,
            "Limited Time Deal":LimitedDeal
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    
if __name__ == "__main__":
    # Allow public access to the API (for EC2/n8n to call it)
    app.run(host="0.0.0.0", port=5000)
