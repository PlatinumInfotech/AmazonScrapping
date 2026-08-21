import asyncio
import re
import json
from fastapi import FastAPI, HTTPException, Request
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
from bs4 import BeautifulSoup
import uvicorn

app = FastAPI()
API_TOKEN = "your_secret_token_here"



def extract_asin(html, url):
    # URL is more reliable – check it first
    url_match = re.search(r'/dp/([A-Z0-9]{10})', url, re.IGNORECASE)
    if url_match:
        return url_match.group(1).upper()
    # Fall back to embedded JSON in page source
    content_match = re.search(r'"asin"\s*:\s*"([A-Z0-9]{10})"', html, re.IGNORECASE)
    if content_match:
        return content_match.group(1).upper()
    return None


@app.post("/crawl")
async def scrape_amazon(request: Request):
    # ── Auth Check ────────────────────────────────────────────────────────────
    token = request.headers.get("X-API-TOKEN")
    if token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    data = await request.json()
    url = data.get("url")
    expected_asin = data.get("asin", "").upper()

    if not url or not expected_asin:
        raise HTTPException(status_code=400, detail="Missing url or asin")

    # ── Browser & Crawler Setup ───────────────────────────────────────────────
    browser_cfg = BrowserConfig(
        headless=True,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    )

    # Bot-bypass click + scroll to trigger lazy-loaded content
    js_interaction = """
    // Attempt to click visible buttons (potential bot checks)
    const btn = document.querySelector("button");
    if (btn && btn.offsetWidth > 0 && btn.offsetHeight > 0) {
        btn.click();
        await new Promise(r => setTimeout(r, 2000));
    }
    // Smooth scroll to trigger lazy-loaded sections
    for (let y = 0; y < 3000; y += 600) {
        window.scrollBy(0, y);
        await new Promise(r => setTimeout(r, 200));
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

        html = result.html
        soup = BeautifulSoup(html, "html.parser")

        # ── ASIN Validation ───────────────────────────────────────────────────
        detected_asin = extract_asin(html, url)
        if not detected_asin:
            return {"status": "failed", "error": "Could not detect ASIN (Likely Captcha)"}

        if detected_asin != expected_asin:
            return {
                "status": "error",
                "message": "ASIN mismatch",
                "found": detected_asin,
                "expected": expected_asin,
            }

        # ── 1. Title ──────────────────────────────────────────────────────────
        title = "Not found"
        title_el = soup.select_one("#productTitle")
        if title_el:
            val = title_el.get_text(strip=True)
            if val:
                title = val

        # ── 2. Availability ───────────────────────────────────────────────────
        availability = "Available"
        avail_el = soup.select_one("#availability")
        if avail_el:
            avail_text = avail_el.get_text().lower()
            if any(kw in avail_text for kw in ("unavailable", "currently unavailable")):
                availability = "Unavailable"
            elif "out of stock" in avail_text:
                availability = "Out of Stock"
        else:
            availability = "Unknown"

        # ── 3. Price (only when available) ────────────────────────────────────
        price = None
        if availability == "Available":
            price = "Not found"

            # Primary selectors (matching Playwright reference)
            for sel in [
                ".a-price .a-offscreen",
                "#priceblock_ourprice",
                "#priceblock_dealprice",
                "#corePrice_feature_div .a-offscreen",
                "span.priceToPay .a-offscreen",
            ]:
                price_el = soup.select_one(sel)
                if price_el:
                    val = price_el.get_text(strip=True)
                    if val:
                        price = val
                        break

            # Fallback: whole + fraction parts → ₹X.YY
            if price == "Not found":
                whole_el = soup.select_one("span.a-price-whole")
                fraction_el = soup.select_one("span.a-price-fraction")
                if whole_el:
                    whole = whole_el.get_text(strip=True).rstrip(".")
                    fraction = fraction_el.get_text(strip=True) if fraction_el else "00"
                    price = f"₹{whole}.{fraction}"

        # ── 4. Coupon ─────────────────────────────────────────────────────────
        coupon_text = "Not found"

        coupon_el = soup.select_one(".couponLabelText, #couponTextFeature_feature_div")
        if coupon_el:
            raw_coupon = coupon_el.get_text(" ", strip=True)
            coupon_cleaned = re.sub(
                r'\s*(Terms|Shop items|\|).*$', '', raw_coupon, flags=re.IGNORECASE
            ).strip()
            if coupon_cleaned:
                coupon_text = coupon_cleaned
        else:
            fallback_coupon = soup.select_one(
                "i.newCouponBadge, .promoPriceBlockMessage, span[id*='couponText']"
            )
            if fallback_coupon:
                parent = fallback_coupon.find_parent("div") or fallback_coupon.parent
                if parent:
                    parent_text = parent.get_text(" ", strip=True)
                    match = re.search(
                        r'(Apply\s+[\d%₹\$\s\w]+coupon|[\d%₹\$]+ off coupon)',
                        parent_text,
                        re.IGNORECASE,
                    )
                    if match:
                        coupon_text = match.group(1).strip()

        # ── 5. Limited Time Deal badge ────────────────────────────────────────
        limited_time_deal = "Not found"
        badge = soup.select_one("span.dealBadgeTextColor, span.dealBadgeText")
        if badge:
            val = badge.get_text(strip=True)
            if val:
                limited_time_deal = val

        # ── 6. Deal tag ───────────────────────────────────────────────────────
        deal_tag = "Not found"
        for dt_el in soup.select("span.a-size-mini.a-color-base"):
            if "Deal" in dt_el.get_text():
                deal_tag = dt_el.get_text(strip=True)
                break

        # ── 7. Consolidated Offer field ───────────────────────────────────────
        if coupon_text != "Not found":
            offer = coupon_text
        elif limited_time_deal != "Not found":
            offer = limited_time_deal
        elif deal_tag != "Not found":
            offer = deal_tag
        else:
            offer = "Not found"

        # ── 8. Rating ─────────────────────────────────────────────────────────
        rating = "Not found"
        for el in soup.select("span.a-icon-alt"):
            text = el.get_text().lower()
            if "out of" in text and "star" in text:
                rating = text.split(" ")[0]
                break

        # ── 9. Best Sellers Rank (3-tier fallback) ────────────────────────────
        best_seller_rank = ["Not found"]
        bsr_found = False

        # Strategy 1: dedicated detail-bullets / prodDetails sections
        for bsr_sel in [
            "#productDetails_detailBullets_sections1",
            "#prodDetails",
            "#detailBulletsWrapper_feature_div",
            "#detailBullets_feature_div",
            "#productDetails_db_sections",
        ]:
            section = soup.select_one(bsr_sel)
            if section:
                text = section.get_text(" ", strip=True)
                # Stop at next rank (#), open paren, or after ~60 chars of category name
                ranks = re.findall(r"#[\d,]+\s+in\s+[A-Za-z][^#\n(]{2,55}", text)
                # Strip trailing junk: multiple spaces, known label words
                ranks = [
                    re.split(r'\s{2,}|\bSee\b|\bBest Sellers\b|\bDate\b|\bManufacturer\b|\bPacker\b|\bCustomer Reviews\b|\bASIN\b', r)[0].strip().rstrip(',')
                    for r in ranks
                ]
                ranks = [r for r in ranks if r]
                if ranks:
                    best_seller_rank = ranks
                    bsr_found = True
                    break

        # Strategy 2: table rows – look for row whose text contains "best seller"
        if not bsr_found:
            for bsr_tbl_sel in [
                "#productDetails_detailBullets_sections1 tr",
                "#prodDetails tr",
                "table.prodDetTable tr",
            ]:
                for row in soup.select(bsr_tbl_sel):
                    row_text = row.get_text(" ", strip=True)
                    if "best seller" in row_text.lower():
                        ranks = re.findall(r"#[\d,]+\s+in\s+[A-Za-z][^#\n(]{2,55}", row_text)
                        ranks = [
                            re.split(r'\s{2,}|\bSee\b|\bBest Sellers\b|\bDate\b|\bManufacturer\b|\bPacker\b|\bCustomer Reviews\b|\bASIN\b', r)[0].strip().rstrip(',')
                            for r in ranks
                        ]
                        ranks = [r for r in ranks if r]
                        if ranks:
                            best_seller_rank = ranks
                            bsr_found = True
                            break
                if bsr_found:
                    break

        # Strategy 3: full body text grep (last resort) with dedup
        if not bsr_found:
            body_text = soup.get_text(" ", strip=True)
            raw_ranks = re.findall(r"#[\d,]+\s+in\s+[A-Za-z][^#\n()]{2,55}", body_text)
            seen = set()
            unique_ranks = []
            for r in raw_ranks:
                cleaned = re.split(r'\s{2,}|\bSee\b|\bBest Sellers\b|\bDate\b|\bManufacturer\b|\bPacker\b|\bCustomer Reviews\b|\bASIN\b', r)[0].strip().rstrip(',')
                if cleaned and cleaned not in seen:
                    seen.add(cleaned)
                    unique_ranks.append(cleaned)
            if unique_ranks:
                best_seller_rank = unique_ranks

        # ── 10. A+ Content ────────────────────────────────────────────────────
        # Only #aplus_feature_div counts as A+.
        # #aplusBrandStory_feature_div is Brand Story – must NOT be counted.
        # We confirm by checking at least one .aplus-module child exists inside
        # the wrapper (same logic as Playwright reference).
        aplus_content = "No"

        aplus_wrapper = soup.select_one("#aplus_feature_div")
        if aplus_wrapper:
            aplus_modules = aplus_wrapper.select(".aplus-module")
            if aplus_modules:
                aplus_content = "Yes"

        # ── 11. Bullet Points ─────────────────────────────────────────────────
        bullets = len(soup.select("#feature-bullets ul li"))

        # ── 12. Seller Name (5-strategy fallback) ────────────────────────────
        seller_name = "Not found"

        # Strategy 1: seller profile trigger link (most common)
        seller_tag = soup.select_one("#sellerProfileTriggerId")
        if seller_tag:
            val = seller_tag.get_text(strip=True)
            if val:
                seller_name = val

        # Strategy 2: #merchant-info block
        if seller_name == "Not found":
            merchant = soup.select_one("#merchant-info")
            if merchant:
                raw = merchant.get_text(" ", strip=True)
                m = re.search(
                    r'(?:Sold by|Ships from and sold by)\s+([^\n.]+)',
                    raw,
                    re.IGNORECASE,
                )
                if m:
                    seller_name = m.group(1).strip()
                elif raw:
                    seller_name = raw

        # Strategy 3: table row containing "Sold by"
        if seller_name == "Not found":
            for tbl_sel in [
                "#productDetails_detailBullets_sections1 tr",
                "#prodDetails tr",
                "table.prodDetTable tr",
            ]:
                for row in soup.select(tbl_sel):
                    row_text = row.get_text(" ", strip=True)
                    if "sold by" in row_text.lower():
                        td = row.find("td")
                        if td:
                            seller_name = td.get_text(strip=True)
                            break
                if seller_name != "Not found":
                    break

        # Strategy 4: buybox / centerCol "Sold by" text pattern
        if seller_name == "Not found":
            for box_sel in ["#buybox", "#desktop_buybox", "#centerCol"]:
                box = soup.select_one(box_sel)
                if box:
                    box_text = box.get_text(" ", strip=True)
                    m = re.search(r'Sold by\s*[:\-]?\s*([^\n]+)', box_text, re.IGNORECASE)
                    if m:
                        seller_name = m.group(1).strip().rstrip(".")
                        break

        # Strategy 5: any string node on the page containing "Sold by"
        if seller_name == "Not found":
            for t in soup.find_all(string=lambda s: s and "Sold by" in s):
                m = re.search(r'Sold by\s+(.*)', t)
                if m:
                    seller_name = m.group(1).strip()
                    break

        # ── 13. Review Count ──────────────────────────────────────────────────
        review_text = "Not found"
        review_el = soup.select_one('#acrCustomerReviewText, span[data-ux="review-count"]')
        if review_el:
            val = review_el.get_text(strip=True)
            if val:
                review_text = val

        # ── Build Response ────────────────────────────────────────────────────
        return {
            "ASIN": detected_asin,
            "Title": title,
            "Availability": availability,
            "Price": price,
            "Offer": offer,
            "Limited Time Deal": limited_time_deal,
            "Rating": rating,
            "Best Sellers Rank": best_seller_rank,
            "A Plus Content": aplus_content,
            "Bullet Points": bullets,
            "Seller Name": seller_name,
            "Review Count": review_text,
            "Status": "success",
        }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
