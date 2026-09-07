from app.parsers.bestseller import parse_bestseller_page
from app.parsers.common import parse_int, parse_price, parse_rating
from app.parsers.product import parse_product
from app.parsers.search import parse_search_page


def test_common_values():
    assert parse_price("$1,299.99") == 1299.99
    assert parse_int("10,152 ratings") == 10152
    assert parse_rating("4.6 out of 5 stars") == 4.6


def test_product_parser_does_not_treat_unit_price_as_list_price():
    html = """
    <div id="title_feature_div"><span id="productTitle">Sample Cleaner</span><span class="a-color-secondary">Gray subtitle highlight</span></div><a id="bylineInfo">Visit the Sample Brand Store</a>
    <div id="corePrice_feature_div">
      <span class="a-price"><span class="a-offscreen">$8.99</span></span>
      <span class="a-price a-text-price"><span class="a-offscreen">$1.12 per count</span></span>
    </div>
    <span id="acrPopover">4.6 out of 5 stars</span>
    <span id="acrCustomerReviewText">1,384 ratings</span>
    <div id="availability">In Stock</div>
    <div id="feature-bullets"><ul><li><span class="a-list-item">First selling point</span></li></ul></div>
    <table id="productDetails_detailBullets_sections1"><tr><th>Date First Available</th><td>August 12, 2026</td></tr></table>
    <img id="landingImage" src="https://m.media-amazon.com/images/I/ABC123._AC_SX679_.jpg" />
    <div id="altImages"><img src="https://m.media-amazon.com/images/I/DEF456._AC_US100_.jpg" /></div>
    """
    row = parse_product(html, "B000000010", "https://www.amazon.com/dp/B000000010")
    assert row["current_price"] == 8.99
    assert row["list_price"] is None
    assert row["rating_count"] == 1384
    assert row["brand"] == "Sample Brand"
    assert row["highlights_text"] == "Gray subtitle highlight"
    assert row["about_items_json"] == ["First selling point"]
    assert row["product_image_count"] == 2
    assert row["product_images_json"] == [
        "https://m.media-amazon.com/images/I/ABC123.jpg",
        "https://m.media-amazon.com/images/I/DEF456.jpg",
    ]
    assert row["delivery_text"] is None
    assert row["date_first_available"] == "2026-08-12"
    assert row["date_first_available_text"] == "August 12, 2026"
    assert row["high_return_rate"] == 0
    assert row["listing_status"] == "active"


def test_product_parser_detects_high_return_rate_label():
    html = """
    <html><head><title>Sample</title></head><body>
      <span id="productTitle">Sample Cleaner</span>
      <div id="availability">In Stock</div>
      <div id="productAlert_feature_div">Frequently returned item</div>
    </body></html>
    """
    row = parse_product(html, "B000000001", "https://www.amazon.com/dp/B000000001")

    assert row["success"] == 1
    assert row["high_return_rate"] == 1
    assert row["high_return_rate_text"] == "Frequently returned item"


def test_product_parser_collects_promotions_business_price_and_rating_breakdown():
    html = """
    <html><body>
      <span id="productTitle">Sample Cleaner</span>
      <div id="availability">In Stock</div>
      <div id="dealBadge_feature_div">
        <span id="dealBadgeSupportingText">Black Friday Deal</span>
        Ends in 03 hours 14 minutes 59 seconds
      </div>
      <div id="primeSavingsUpsellCaption_feature_div">Prime Big Deal Days</div>
      <div id="businessPrice_feature_div">
        <span>Business Price:</span><span class="a-price"><span class="a-offscreen">$7.49</span></span>
      </div>
      <span id="acrPopover">4.6 out of 5 stars</span>
      <span id="acrCustomerReviewText">1,000 ratings</span>
      <ul id="histogramTable">
        <li><a aria-label="74 percent of reviews have 5 stars"></a></li>
        <li><a aria-label="15 percent of reviews have 4 stars"></a></li>
        <li><a aria-label="7 percent of reviews have 3 stars"></a></li>
        <li><a aria-label="2 percent of reviews have 2 stars"></a></li>
        <li><a aria-label="2 percent of reviews have 1 stars"></a></li>
      </ul>
      <script>window.copy = "Cyber Monday Deal";</script>
    </body></html>
    """

    row = parse_product(html, "B000000001", "https://www.amazon.com/dp/B000000001")

    assert row["deal_text"] == "Black Friday Deal；Prime Big Deal Days"
    assert row["business_price_text"] == "Business Price: $7.49"
    assert row["rating_breakdown_json"] == {"5": 74, "4": 15, "3": 7, "2": 2, "1": 2}


def test_product_parser_does_not_treat_promotion_copy_in_scripts_as_active_deal():
    row = parse_product(
        "<html><body><span id='productTitle'>Active item</span>"
        "<div id='availability'>In Stock</div>"
        "<script>window.copy = 'Prime Day Deal and Black Friday Deal';</script></body></html>",
        "B000000001", "https://www.amazon.com/dp/B000000001",
    )

    assert row["deal_text"] is None


def test_product_parser_recognizes_dog_and_removed_pages():
    dog = parse_product(
        "<html><head><title>Amazon.com Page Not Found</title></head>"
        "<body>Sorry! We couldn't find that page. Dogs of Amazon</body></html>",
        "B000000001", "https://www.amazon.com/dp/B000000001",
    )
    removed = parse_product(
        "<html><body>This item is no longer available.</body></html>",
        "B000000002", "https://www.amazon.com/dp/B000000002",
    )

    assert dog["listing_status"] == "dog" and dog["success"] == 0
    assert removed["listing_status"] == "removed" and removed["success"] == 0


def test_removed_copy_outside_main_availability_does_not_hide_valid_product():
    row = parse_product(
        "<html><body><span id='productTitle'>Active item</span>"
        "<div id='availability'>In Stock</div>"
        "<div class='recommendation'>This item is no longer available</div></body></html>",
        "B000000001", "https://www.amazon.com/dp/B000000001",
    )

    assert row["listing_status"] == "active"
    assert row["success"] == 1


def test_search_ranks_ads_and_organic_separately():
    html = """
    <div data-component-type="s-search-result" data-asin="B000000001"><span>Sponsored</span><h2><span>Ad</span></h2></div>
    <div data-component-type="s-search-result" data-asin="B000000002"><h2><span>Organic</span></h2></div>
    """
    rows = parse_search_page(html, "test", 1, {"absolute": 0, "organic": 0, "ad": 0})
    assert rows[0]["ad_rank"] == 1 and rows[0]["organic_rank"] is None
    assert rows[1]["organic_rank"] == 1 and rows[1]["absolute_position"] == 2


def test_bestseller_parser():
    html = """
    <div id="gridItemRoot"><span class="zg-bdg-text">#1</span>
      <a href="/dp/B000000001"><span><div>Example</div></span></a>
      <span class="p13n-sc-price">$9.98</span><span class="a-icon-alt">4.7 out of 5 stars</span>
    </div>
    """
    rows = parse_bestseller_page(html, "Category", "https://example.com")
    assert rows[0]["rank"] == 1 and rows[0]["asin"] == "B000000001"
