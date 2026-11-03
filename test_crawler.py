"""Unit and integration tests for crawler.py."""

import httpx
import pytest

from crawler import URLProcessor, WebCrawler


def test_url_normalization() -> None:
    assert URLProcessor.normalize("https://zego.com") == "https://zego.com/"
    assert URLProcessor.normalize("https://zego.com/about/") == "https://zego.com/about"
    assert (
        URLProcessor.normalize("/pricing#faq", current_page="https://zego.com/about")
        == "https://zego.com/pricing"
    )
    assert URLProcessor.normalize("mailto:info@zego.com") is None
    assert URLProcessor.normalize("javascript:void(0)") is None


def test_strict_single_domain_filtering() -> None:
    processor = URLProcessor("https://zego.com")
    assert processor.should_crawl("https://zego.com/car-insurance")
    # Subdomains and different hosts should be rejected
    assert not processor.should_crawl("https://blog.zego.com/post")
    assert not processor.should_crawl("https://www.zego.com/")
    assert not processor.should_crawl("https://google.com/")
    # Obvious static/binary files should be rejected
    assert not processor.should_crawl("https://zego.com/terms.pdf")
    assert not processor.should_crawl("https://zego.com/logo.png")


def test_extract_links_deduplicates_and_normalizes() -> None:
    html = """
    <html><body>
        <a href="/about">About</a>
        <a href="https://zego.com/about#team">About Team</a>
        <a href="https://twitter.com/zego">Twitter</a>
        <a href="mailto:test@zego.com">Email</a>
    </body></html>
    """
    links = WebCrawler.extract_links(html, "https://zego.com/")
    assert links == ["https://zego.com/about", "https://twitter.com/zego"]


@pytest.mark.asyncio
async def test_crawler_crawls_only_same_domain_and_avoids_cycles() -> None:
    pages = {
        "https://zego.com/": """
            <a href="/about">About</a>
            <a href="https://blog.zego.com/news">Subdomain</a>
            <a href="https://twitter.com/zego">External</a>
            <a href="/policy.pdf">PDF</a>
        """,
        "https://zego.com/about": """
            <a href="/">Home (Cycle)</a>
            <a href="/contact">Contact</a>
        """,
        "https://zego.com/contact": """
            <a href="/">Home</a>
        """,
        "https://blog.zego.com/news": """
            <a href="https://blog.zego.com/secret">Should not be crawled</a>
        """,
    }

    fetch_counts: dict[str, int] = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        fetch_counts[url] = fetch_counts.get(url, 0) + 1
        if url in pages:
            return httpx.Response(
                200, headers={"content-type": "text/html"}, text=pages[url]
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(mock_handler)
    ) as mock_client:
        crawler = WebCrawler("https://zego.com", concurrency=4, client=mock_client)
        results = await crawler.crawl()

    crawled_urls = {r.url for r in results}
    assert crawled_urls == {
        "https://zego.com/",
        "https://zego.com/about",
        "https://zego.com/contact",
    }
    assert all(fetch_counts.get(url, 0) == 1 for url in crawled_urls)
    assert "https://blog.zego.com/news" not in fetch_counts
    assert "https://twitter.com/zego" not in fetch_counts

    home = next(r for r in results if r.url == "https://zego.com/")
    assert "https://blog.zego.com/news" in home.links
    assert "https://twitter.com/zego" in home.links
