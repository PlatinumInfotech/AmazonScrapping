import asyncio
import re
import json
from fastapi import FastAPI, HTTPException, Request
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
from bs4 import BeautifulSoup
import uvicorn

app = FastAPI()
API_TOKEN = "your_secret_token_here"

def clean_text(text):
    """Deep cleans Amazon's hidden formatting and artifacts."""
    if not text:
        return "Not found"
    # Remove hidden Unicode markers and extra whitespace
    text = re.sub(r'[\u200e\u200f\u202a\u202c\u202d\u202e]', '', text)
    # Replace multiple spaces/newlines with a single space
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def extract_asin(html, url):
    asin_match = re.search(r'"asin"\s*:\s*"([A-Z0-9]{10})"', html, re.IGNORECASE)
    if not asin_match:
        asin_match = re.search(r'/dp/([A-Z0-9]{10})', url, re.IGNORECASE)
    return asin_match.group(1).upper() if asin_match else None

@app.post("/crawl")
async def scrape_amazon(request: Request):
    # --- Auth Check ---
    token = request.headers.get("X-API-TOKEN")
    if token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    data = await request.json()
    url = data.get("url")
    expected_asin = data.get("asin", "").upper()

    if not url or not expected_asin:
        raise HTTPException(status_code=400, detail="Missing url or asin")

    # --- Browser & Crawler Setup ---
    browser_cfg = BrowserConfig(
        headless=True,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    )

    # Ported bot-bypass and scroll logic
    js_interaction = """
    // Attempt to click visible buttons (potential bot checks)
    const btn = document.querySelector("button");
    if (btn && btn.offsetWidth > 0 && btn.offsetHeight > 0) {
        btn.click();
        await new Promise(r => setTimeout(r, 2000));
    }
    // Smooth scroll logic
    for (let y = 0; y < 2000; y += 400) {
        window.scrollBy(0, y);
        await new Promise(r => setTimeout(r, 300));
    }
    """

    run_cfg = CrawlerRunConfig(
        js_code=js_interaction,
        wait_for="body",
        magic=True,
        cache_mode="BYPASS",
        page_timeout=60000
    )

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        result = await crawler.arun(url=url, config=run_cfg)

        if not result.success:
            return {"status": "failed", "error": result.error_message}

        soup = BeautifulSoup(result.html, "html.parser")
        
        # --- ASIN Validation ---
        detected_asin = extract_asin(result.html, url)
        if not detected_asin:
             return {"status": "failed", "error": "Could not detect ASIN (Likely Captcha)"}
        
        if detected_asin != expected_asin:
            return {"status": "error", "message": "ASIN mismatch", "found": detected_asin, "expected": expected_asin}

        # --- Data Extraction (Matching Playwright logic) ---
        
        # 1. Title
        title_el = soup.select_one("#productTitle")
        title = title_el.get_text(strip=True) if title_el else "Not found"

        # 2. Availability
        availability = "Available"
        avail_el = soup.select_one("#availability")
        if avail_el:
            avail_text = avail_el.get_text().lower()
            if "unavailable" in avail_text: availability = "Unavailable"
            elif "out of stock" in avail_text: availability = "Out of Stock"
        else:
            availability = "Unknown"

        # 3. Price Logic
        price = "Not found"
        if availability == "Available":
            # Primary selectors
            for sel in [".a-price .a-offscreen", "#priceblock_ourprice", "#priceblock_dealprice", "span.priceToPay", ".a-price.apexPriceToPay"]:
                price_el = soup.select_one(sel)
                if price_el:
                    price = price_el.get_text(strip=True)
                    break
            
            # Fallback for ₹ format
            if price == "Not found":
                whole = soup.select_one("span.a-price-whole")
                fraction = soup.select_one("span.a-price-fraction")
                if whole:
                    p_val = whole.get_text(strip=True).replace(".", "")
                    f_val = fraction.get_text(strip=True) if fraction else "00"
                    price = whole
        else:
            price = None

        # 4. Ratings
        rating = "Not found"
        rating_elements = soup.select("span.a-icon-alt")
        for el in rating_elements:
            text = el.get_text().lower()
            if "out of" in text and "star" in text:
                rating = text.split(" ")[0]
                break

        # # 5. Best Sellers Rank (Regex)
        # details_text = ""
        # detail_section = soup.select_one("#productDetails_detailBullets_sections1, #prodDetails, #detailBulletsWrapper_feature_div")
        # if detail_section:
        #     details_text = detail_section.get_text()
        # ranks = re.findall(r"#\d[\d,]*\s+in\s+[^\n()]+", details_text)
        # best_seller_rank = ranks if ranks else ["Not found"]

        # --- DETAIL SECTION PROCESSING (Rank & Contact Info) ---
        detail_section = soup.select_one("#productDetails_detailBullets_sections1, #prodDetails, #detailBulletsWrapper_feature_div, #productDetails_techSpec_section_1")
        details_text = detail_section.get_text() if detail_section else ""
        
        # Best Sellers Rank
        rank_pattern = r"#[\d,]+\s+in\s+[^|]+"
        ranks_found = re.findall(rank_pattern, details_text)
        # Final cleanup to remove any trailing words that aren't part of the category
        ranks = [re.split(r'\s{2,}|Date|Manufacturer|Packer', r)[0].strip() for r in ranks_found]

        # 6. Additional Fields
        badge = soup.select_one("span.dealBadgeTextColor, span.dealBadgeText")
        seller = soup.select_one("#sellerProfileTriggerId")
        reviews = soup.select_one('#acrCustomerReviewText, span[data-ux="review-count"]')
        aplus = "Yes" if soup.select_one("div.aplus-v2.desktop, div.aplus, div#aplus") else "No"
        bullets = len(soup.select("#feature-bullets ul li"))
        deal_tag = soup.select_one("span.a-size-mini.a-color-base") # Simplified deal check
        
        manufacturer = None
        manufacturer_contact = None
        packer_contact = None
        packer_contact_info = None
        importer_contact = None
        importer_contact_info = None

        if detail_section:
            rows = detail_section.select("tr")

            for row in rows:
                header = row.find("th")
                value = row.find("td")

                if not header or not value:
                    continue

                key = clean_text(header.get_text(strip=True)).lower()
                val = clean_text(value.get_text(strip=True))

                if "manufacturer contact" in key:
                    manufacturer_contact = val

                elif key == "manufacturer":
                    manufacturer = val

                elif "packer contact" in key:
                    packer_contact_info = val

                elif key == "packer":
                    packer_contact = val

                elif "importer contact" in key:
                    importer_contact_info = val

                elif key == "importer":
                    importer_contact = val        

        return {
            "ASIN": detected_asin,
            "Title": title,
            "Availability": availability,
            "Price": price,
            "Limited Time Deal": badge.get_text(strip=True) if badge else "Not found",
            "Rating": rating,
            "Best Sellers Rank": ranks if ranks else ["Not found"],
            "A Plus Content": aplus,
            "Bullet Points": bullets,
            "Deal Tag": deal_tag.get_text(strip=True) if deal_tag and "Deal" in deal_tag.text else "Not found",
            "Manufacturer": manufacturer or "Not found",
            "Manufacturer Contact": manufacturer_contact or "Not found",
            "Packer Contact": packer_contact or "Not found",
            "Packer Contact Info": packer_contact_info,
            "Importer Contact": importer_contact or "Not found",  
            "Importer Contact Info": importer_contact_info,     
            "Seller Name": seller.get_text(strip=True) if seller else "Not found",
            "Review Count": reviews.get_text(strip=True) if reviews else "Not found",
            "Status": "success"
        }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
