from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import re
import logging
import time

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
API_TOKEN = "your_secret_token_here"

# How many times to retry the full scrape on transient failures
MAX_RETRIES = 2


# ── Auth middleware ────────────────────────────────────────────────────────────
@app.before_request
def check_auth():
    if request.endpoint != "index":
        token = request.headers.get("X-API-TOKEN")
        if token != API_TOKEN:
            return jsonify({"error": "Unauthorized"}), 401


@app.route("/")
def index():
    return "✅ Amazon Playwright Scraper is running!"


# ── Core scraper ──────────────────────────────────────────────────────────────
def _do_scrape(url: str, expected_asin: str) -> dict:
    """
    Launch a browser, scrape the Amazon product page, and return a data dict.
    Raises exceptions on hard failures (caller handles retries / HTTP responses).
    """
    browser = None
    context = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
            )
            context.set_extra_http_headers({
                "Accept-Language": "en-IN,en;q=0.9",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,*/*;q=0.8"
                ),
            })

            page = context.new_page()

            # ── Navigate & wait for the page to settle ─────────────────────
            logger.info("Navigating to: %s", url)
            try:
                page.goto(url, timeout=60000, wait_until="domcontentloaded")
            except PlaywrightTimeoutError:
                logger.warning("domcontentloaded timed out – continuing anyway")

            # Give dynamic JS a chance to finish rendering key elements
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                logger.warning("networkidle not reached – proceeding with current DOM")

            # ── ASIN detection (URL first – faster & more reliable) ────────
            detected_asin = None

            url_match = re.search(r"/dp/([A-Z0-9]{10})", page.url, re.IGNORECASE)
            if url_match:
                detected_asin = url_match.group(1).upper()
                logger.info("ASIN from URL: %s", detected_asin)

            if not detected_asin:
                try:
                    page_content = page.content()
                    content_match = re.search(
                        r'"asin"\s*:\s*"([A-Z0-9]{10})"', page_content, re.IGNORECASE
                    )
                    if content_match:
                        detected_asin = content_match.group(1).upper()
                        logger.info("ASIN from page content: %s", detected_asin)
                except Exception as exc:
                    logger.warning("ASIN content extraction error: %s", exc)

            if not detected_asin:
                raise ValueError("Could not detect ASIN on page")

            if detected_asin != expected_asin.upper():
                raise ValueError(
                    f"ASIN mismatch – expected={expected_asin}, found={detected_asin}"
                )

            # ── Bot-check bypass (CAPTCHA button) ─────────────────────────
            try:
                btn = page.locator("button:visible").first
                if btn.count() > 0 and btn.is_visible(timeout=2000):
                    btn.click()
                    page.wait_for_timeout(2000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except PlaywrightTimeoutError:
                        pass
            except Exception as exc:
                logger.debug("Bot-bypass step skipped: %s", exc)

            # ── Scroll to trigger lazy-loaded content ──────────────────────
            for y in range(0, 3000, 600):
                try:
                    page.mouse.wheel(0, y)
                    page.wait_for_timeout(200)
                except Exception:
                    break

            # Allow lazy content to finish loading after scroll
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                pass

            # ── Title ──────────────────────────────────────────────────────
            title = "Not found"
            try:
                page.wait_for_selector("#productTitle", timeout=10000)
                val = page.locator("#productTitle").first.text_content(timeout=5000).strip()
                if val:
                    title = val
            except Exception as exc:
                logger.warning("Title extraction failed: %s", exc)

            # ── Availability ───────────────────────────────────────────────
            availability = "Available"
            try:
                avail_text = page.locator("#availability").first.inner_text(timeout=5000).strip().lower()
                if any(kw in avail_text for kw in ("unavailable", "currently unavailable")):
                    availability = "Unavailable"
                elif "out of stock" in avail_text:
                    availability = "Out of Stock"
                logger.info("Availability: %s", availability)
            except Exception as exc:
                logger.warning("Availability check failed: %s", exc)
                availability = "Unknown"

            # ── Price (only when available) ────────────────────────────────
            price = None
            if availability == "Available":
                price = "Not found"

                # Primary selectors (including newer Amazon price selectors)
                for sel in [
                    ".a-price .a-offscreen",
                    "#priceblock_ourprice",
                    "#priceblock_dealprice",
                    "#corePrice_feature_div .a-offscreen",
                    "span.priceToPay .a-offscreen",
                ]:
                    try:
                        el = page.locator(sel).first
                        if el.count() > 0:
                            val = el.text_content(timeout=3000).strip()
                            if val:
                                price = val
                                logger.info("Price from '%s': %s", sel, price)
                                break
                    except Exception:
                        continue

                # Fallback: whole + fraction parts
                if price == "Not found":
                    try:
                        whole = page.locator("span.a-price-whole").first.text_content(timeout=3000).strip().rstrip(".")
                        fraction_el = page.locator("span.a-price-fraction").first
                        if fraction_el.count() > 0:
                            fraction = fraction_el.text_content(timeout=3000).strip()
                            price = f"₹{whole}.{fraction}"
                        else:
                            price = f"₹{whole}"
                        logger.info("Price from whole/fraction: %s", price)
                    except Exception as exc:
                        logger.warning("Fallback price extraction failed: %s", exc)
                        price = "Not found"

            # ── Coupon ─────────────────────────────────────────────────────
            coupon_text = "Not found"
            try:
                coupon_el = page.locator(".couponLabelText, #couponTextFeature_feature_div").first
                if coupon_el.count() > 0 and coupon_el.is_visible(timeout=2000):
                    raw_coupon = coupon_el.inner_text(timeout=3000).strip()
                    coupon_cleaned = re.sub(
                        r"\s*(Terms|Shop items|\|).*$", "", raw_coupon, flags=re.IGNORECASE
                    ).strip()
                    if coupon_cleaned:
                        coupon_text = coupon_cleaned
                else:
                    fallback = page.locator(
                        "i.newCouponBadge, .promoPriceBlockMessage, span[id*='couponText']"
                    ).first
                    if fallback.count() > 0:
                        parent_text = fallback.locator("xpath=..").inner_text(timeout=3000)
                        match = re.search(
                            r"(Apply\s+[\d%₹\$\s\w]+coupon|[\d%₹\$]+ off coupon)",
                            parent_text,
                            re.IGNORECASE,
                        )
                        if match:
                            coupon_text = match.group(1).strip()
            except Exception as exc:
                logger.warning("Coupon extraction failed: %s", exc)
                coupon_text = "Not found"

            # ── Limited Time Deal badge ────────────────────────────────────
            limited_deal = "Not found"
            try:
                badge = page.locator("span.dealBadgeTextColor, span.dealBadgeText").first
                if badge.count() > 0 and badge.is_visible(timeout=2000):
                    limited_deal = badge.inner_text(timeout=3000).strip()
            except Exception as exc:
                logger.warning("Limited deal extraction failed: %s", exc)

            # ── Deal tag ───────────────────────────────────────────────────
            deal_tag = "Not found"
            try:
                deal_el = page.locator("span.a-size-mini.a-color-base").filter(has_text="Deal")
                if deal_el.count() > 0:
                    deal_tag = deal_el.first.inner_text(timeout=3000).strip()
            except Exception as exc:
                logger.warning("Deal tag extraction failed: %s", exc)

            # ── Consolidated offer field ───────────────────────────────────
            if coupon_text != "Not found":
                offer = coupon_text
            elif limited_deal != "Not found":
                offer = limited_deal
            elif deal_tag != "Not found":
                offer = deal_tag
            else:
                offer = "Not found"

            # ── Rating ─────────────────────────────────────────────────────
            rating = None
            try:
                page.wait_for_selector("span.a-icon-alt", timeout=7000)
                rating_elements = page.locator("span.a-icon-alt")
                total = rating_elements.count()
                for i in range(total):
                    try:
                        text = rating_elements.nth(i).inner_text(timeout=2000).strip().lower()
                        if "out of" in text and "star" in text:
                            rating = text.split(" ")[0]
                            logger.info("Rating: %s", rating)
                            break
                    except Exception:
                        continue
            except Exception as exc:
                logger.warning("Rating extraction failed: %s", exc)
                rating = "Not found"

            # ── Best Sellers Rank ──────────────────────────────────────────
            best_seller_rank = ["Not found"]
            try:
                bsr_found = False

                # Strategy 1: dedicated detail-bullets / prodDetails sections
                for bsr_sel in [
                    "#productDetails_detailBullets_sections1",
                    "#prodDetails",
                    "#detailBulletsWrapper_feature_div",
                    "#detailBullets_feature_div",
                    "#productDetails_db_sections",
                ]:
                    try:
                        el = page.locator(bsr_sel).first
                        if el.count() > 0:
                            text = el.inner_text(timeout=5000)
                            ranks = re.findall(r"#[\d,]+\s+in\s+[^\n(]+", text)
                            if ranks:
                                best_seller_rank = [r.strip() for r in ranks]
                                bsr_found = True
                                logger.info("BSR from '%s': %s", bsr_sel, best_seller_rank)
                                break
                    except Exception:
                        continue

                # Strategy 2: product-details table rows (th contains "Best Sellers Rank")
                if not bsr_found:
                    try:
                        rows = page.locator(
                            "#productDetails_detailBullets_sections1 tr, "
                            "#prodDetails tr, "
                            "table.prodDetTable tr"
                        )
                        for i in range(rows.count()):
                            try:
                                row_text = rows.nth(i).inner_text(timeout=2000)
                                if "best seller" in row_text.lower():
                                    ranks = re.findall(r"#[\d,]+\s+in\s+[^\n(]+", row_text)
                                    if ranks:
                                        best_seller_rank = [r.strip() for r in ranks]
                                        bsr_found = True
                                        logger.info("BSR from table row: %s", best_seller_rank)
                                        break
                            except Exception:
                                continue
                    except Exception:
                        pass

                # Strategy 3: full page content grep (last resort)
                if not bsr_found:
                    try:
                        full_text = page.locator("body").inner_text(timeout=8000)
                        ranks = re.findall(r"#[\d,]+\s+in\s+[A-Za-z][^\n()]{3,60}", full_text)
                        # De-duplicate while preserving order
                        seen = set()
                        unique_ranks = []
                        for r in ranks:
                            cleaned = r.strip()
                            if cleaned not in seen:
                                seen.add(cleaned)
                                unique_ranks.append(cleaned)
                        if unique_ranks:
                            best_seller_rank = unique_ranks
                            logger.info("BSR from full page text: %s", best_seller_rank)
                    except Exception as exc2:
                        logger.warning("BSR full-page fallback failed: %s", exc2)

            except Exception as exc:
                logger.warning("BSR extraction failed: %s", exc)

            # ── A+ Content ─────────────────────────────────────────────────
            # Strategy:
            #   1. Look for #aplus_feature_div – the dedicated wrapper Amazon
            #      injects only when real A+ (Enhanced Product Description) exists.
            #   2. Confirm it is NOT the Brand Story section alone by checking that
            #      it contains at least one actual A+ module (.aplus-module) inside.
            #   3. Explicitly skip #aplusBrandStory_feature_div – that is "Product
            #      Story" / Brand Story, a separate feature that must NOT count as A+.
            aplus_content = "No"
            try:
                # Primary: dedicated A+ feature div (absent when no A+ is published)
                aplus_wrapper = page.locator("#aplus_feature_div").first

                if aplus_wrapper.count() > 0 and aplus_wrapper.is_visible(timeout=3000):
                    # Count actual A+ content modules inside the wrapper.
                    # Brand Story lives in #aplusBrandStory_feature_div (a sibling,
                    # not a child of #aplus_feature_div), so modules here belong
                    # exclusively to the A+ Enhanced Product Description.
                    module_count = aplus_wrapper.locator(".aplus-module").count()

                    if module_count > 0:
                        aplus_content = "Yes"
                        logger.info("A+ content confirmed: %d module(s) found", module_count)
                    else:
                        logger.info("A+ wrapper present but no .aplus-module children – marking No")
                else:
                    logger.info("No #aplus_feature_div found – marking No")

            except Exception as exc:
                logger.warning("A+ content check failed: %s", exc)

            # ── Bullet Points ──────────────────────────────────────────────
            bullet_count = 0
            try:
                bullet_count = page.locator("#feature-bullets ul li").count()
            except Exception as exc:
                logger.warning("Bullet count failed: %s", exc)

            # ── Seller Name ────────────────────────────────────────────────
            seller_name = "Not found"
            try:
                # Strategy 1: seller profile link (most common)
                seller_el = page.locator("#sellerProfileTriggerId").first
                if seller_el.count() > 0:
                    val = seller_el.inner_text(timeout=4000).strip()
                    if val:
                        seller_name = val
                        logger.info("Seller from #sellerProfileTriggerId: %s", seller_name)

                # Strategy 2: #merchant-info block
                if seller_name == "Not found":
                    merchant_el = page.locator("#merchant-info").first
                    if merchant_el.count() > 0:
                        raw = merchant_el.inner_text(timeout=4000).strip()
                        # Extract the linked seller name if present
                        m = re.search(r"(?:Sold by|Ships from and sold by)\s+([^\n.]+)", raw, re.IGNORECASE)
                        if m:
                            seller_name = m.group(1).strip()
                            logger.info("Seller from #merchant-info: %s", seller_name)
                        elif raw:
                            seller_name = raw

                # Strategy 3: tabular product details ("Sold by" row)
                if seller_name == "Not found":
                    try:
                        sold_by_rows = page.locator(
                            "#productDetails_detailBullets_sections1 tr, "
                            "#prodDetails tr, "
                            "table.prodDetTable tr"
                        )
                        for i in range(sold_by_rows.count()):
                            try:
                                row_text = sold_by_rows.nth(i).inner_text(timeout=2000)
                                if "sold by" in row_text.lower():
                                    # The seller name is in the <td> beside the <th>
                                    td = sold_by_rows.nth(i).locator("td").first
                                    if td.count() > 0:
                                        seller_name = td.inner_text(timeout=2000).strip()
                                        logger.info("Seller from table row: %s", seller_name)
                                        break
                            except Exception:
                                continue
                    except Exception:
                        pass

                # Strategy 4: any visible "Sold by" text block on the page
                if seller_name == "Not found":
                    try:
                        body_text = page.locator("#buybox, #desktop_buybox, #centerCol").first.inner_text(timeout=5000)
                        m = re.search(r"Sold by\s*[:\-]?\s*([^\n]+)", body_text, re.IGNORECASE)
                        if m:
                            seller_name = m.group(1).strip().rstrip(".")
                            logger.info("Seller from buybox text: %s", seller_name)
                    except Exception:
                        pass

            except Exception as exc:
                logger.warning("Seller name extraction failed: %s", exc)

            # ── Review Count ───────────────────────────────────────────────
            review_text = "Not found"
            try:
                review_el = page.locator(
                    '#acrCustomerReviewText, span[data-ux="review-count"]'
                ).first
                if review_el.count() > 0:
                    review_text = review_el.text_content(timeout=4000).strip()
            except Exception as exc:
                logger.warning("Review count extraction failed: %s", exc)

            # ── Build response ─────────────────────────────────────────────
            result = {
                "ASIN": detected_asin,
                "Title": title,
                "Availability": availability,
                "Price": price,
                "Offer": offer,
                "Limited Time Deal": limited_deal,
                "Rating": rating,
                "Best Sellers Rank": best_seller_rank,
                "A Plus Content": aplus_content,
                "Bullet Points": bullet_count,
                "Seller Name": seller_name,
                "Review Count": review_text,
            }
            logger.info("Scrape complete for ASIN %s", detected_asin)
            return result

    finally:
        # Always close browser – even if an exception was raised mid-scrape
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass


# ── Flask route ────────────────────────────────────────────────────────────────
@app.route("/scrape", methods=["POST"])
def scrape_single():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid or missing JSON body"}), 400

    url = data.get("url", "").strip()
    expected_asin = data.get("asin", "").strip()

    if not url or not expected_asin:
        return jsonify({"error": "Missing 'url' or 'asin' in request body"}), 400

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info("Scrape attempt %d/%d for ASIN=%s", attempt, MAX_RETRIES, expected_asin)
            start = time.monotonic()
            result = _do_scrape(url, expected_asin)
            elapsed = time.monotonic() - start
            logger.info("Finished in %.1fs on attempt %d", elapsed, attempt)
            return jsonify(result), 200

        except ValueError as ve:
            # ASIN mismatch or detection failure – don't retry, return 4xx
            msg = str(ve)
            logger.error("Validation error: %s", msg)
            if "mismatch" in msg.lower():
                parts = msg.split("expected=")
                exp = parts[1].split(",")[0] if len(parts) > 1 else expected_asin
                found = msg.split("found=")[1] if "found=" in msg else "unknown"
                return jsonify({"error": "ASIN mismatch", "expected": exp, "found": found}), 409
            return jsonify({"error": msg}), 400

        except PlaywrightTimeoutError as te:
            last_error = f"Timeout on attempt {attempt}: {te}"
            logger.warning(last_error)
            if attempt < MAX_RETRIES:
                time.sleep(3)

        except Exception as exc:
            last_error = f"Unexpected error on attempt {attempt}: {exc}"
            logger.exception(last_error)
            if attempt < MAX_RETRIES:
                time.sleep(3)

    logger.error("All %d attempts failed. Last error: %s", MAX_RETRIES, last_error)
    if "timeout" in str(last_error).lower():
        return jsonify({"error": "Timeout while loading page", "detail": last_error}), 504
    return jsonify({"error": "Scrape failed after retries", "detail": last_error}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
