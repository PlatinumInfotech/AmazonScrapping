from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright, TimeoutError
import re

app = Flask(__name__)
API_TOKEN = "your_secret_token_here"

@app.before_request
def check_auth():
    if request.endpoint != "index":
        token = request.headers.get("X-API-TOKEN")
        if token != API_TOKEN:
            return jsonify({"error": "Unauthorized"}), 401

@app.route("/")
def index():
    return "✅ Amazon Playwright Scraper is running!"

@app.route("/scrape", methods=["POST"])
def scrape_single():
    data = request.get_json()
    url = data.get("url")
    expected_asin = data.get("asin")

    if not url or not expected_asin:
        return jsonify({"error": "Missing 'url' or 'asin' in request body"}), 400

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
            ))
            page = context.new_page()
            page.goto(url, timeout=60000)

            # --- Extract ASIN from page ---
            page_content = page.content()
            asin_match = re.search(r'"asin"\s*:\s*"([A-Z0-9]{10})"', page_content, re.IGNORECASE)
            if not asin_match:
                asin_match = re.search(r'/dp/([A-Z0-9]{10})', page.url, re.IGNORECASE)

            if not asin_match:
                return jsonify({"error": "Could not detect ASIN on page"}), 400

            detected_asin = asin_match.group(1)

            if detected_asin.upper() != expected_asin.upper():
                return jsonify({
                    "error": "ASIN mismatch",
                    "expected": expected_asin,
                    "found": detected_asin
                }), 409

            # Bot bypass
            try:
                button = page.locator("button:visible").first
                if button.is_visible():
                    button.click()
                    page.wait_for_timeout(3000)
            except:
                pass

            for y in range(0, 2000, 400):
                page.mouse.wheel(0, y)
                page.wait_for_timeout(300)

            try:
                title = page.locator("#productTitle").first.text_content().strip()
            except:
                title = "Not found"

            price = "Not found"
            for sel in [".a-price .a-offscreen", "#priceblock_ourprice", "#priceblock_dealprice"]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible():
                        price = el.inner_text().strip()
                        break
                except:
                    continue

            try:
                badge = page.locator("span.dealBadgeTextColor, span.dealBadgeText").first
                limited_deal = badge.inner_text().strip() if badge.is_visible() else "Not found"
            except:
                limited_deal = "Not found"

            # try:
            #     rating = page.locator("span.a-icon-alt").first.inner_text().strip()
            # except:
            #     rating = "Not found"

            # --- RATING (SAFE & AMAZON-PROOF) ---
            rating = None

            try:
                # wait briefly for lazy-loaded rating
                page.wait_for_selector("span.a-icon-alt", timeout=5000)
            except:
                pass

            try:
                rating_elements = page.locator("span.a-icon-alt")
                total = rating_elements.count()

                for i in range(total):
                    text = rating_elements.nth(i).inner_text().strip().lower()

                    # valid rating format: "4.3 out of 5 stars"
                    if "out of" in text and "star" in text:
                        rating = text.split(" ")[0]
                        break
            except:
                rating = "Not found"

            try:
                detail_section = page.locator("#productDetails_detailBullets_sections1, #prodDetails, #detailBulletsWrapper_feature_div").first
                text = detail_section.inner_text()
                ranks = re.findall(r"#\d[\d,]*\s+in\s+[^\n()]+", text)
                best_seller_rank = ranks if ranks else ["Not found"]
            except:
                best_seller_rank = ["Not found"]

            try:
                aplus_content = "Yes" if page.locator("div.aplus-v2.desktop, div.aplus, div#aplus").first.is_visible() else "No"
            except:
                aplus_content = "No"

            try:
                bullet_count = page.locator("#feature-bullets ul li").count()
            except:
                bullet_count = 0

            try:
                deal_element = page.locator("span.a-size-mini.a-color-base").filter(has_text="Deal")
                deal_tag = deal_element.first.inner_text().strip() if deal_element.count() > 0 else "Not found"
            except:
                deal_tag = "Not found"

            try:
                seller_name = page.locator("#sellerProfileTriggerId").first.inner_text().strip()
            except:
                seller_name = "Not found"

            try:
                review_text = page.locator('#acrCustomerReviewText, span[data-ux="review-count"]').first.text_content().strip()
            except:
                review_text = "Not found"

            browser.close()

            return jsonify({
                "ASIN": detected_asin,
                "Title": title,
                "Price": price,
                "Limited Time Deal": limited_deal,
                "Rating": rating,
                "Best Sellers Rank": best_seller_rank,
                "A Plus Content": aplus_content,
                "Bullet Points": bullet_count,
                "Deal Tag": deal_tag,
                "Seller Name": seller_name,
                "Review Count": review_text
            })

    except TimeoutError:
        return jsonify({"error": "Timeout while loading page"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
