from __future__ import annotations

import argparse
import asyncio
import posixpath
import sys
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

# ignore some subfix that is not a link worth fetching
IGNORED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".bmp",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".rar",
    ".7z",
    ".css",
    ".js",
    ".mjs",
    ".map",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
    ".webm",
}


@dataclass
class PageResult:
    # one page fetched, put the url and link into this structure
    url: str
    links: list[str] = field(default_factory=list)


class URLProcessor:
    # normalize URLs and only select urls from same host

    def __init__(self, base_url: str) -> None:
        normalized = self.normalize(base_url)
        if not normalized:
            raise ValueError(f"Invalid base URL: {base_url}")
        self.base_url = normalized
        self.allowed_host = urlsplit(normalized).hostname

    @staticmethod
    def normalize(raw_url: str, current_page: str | None = None) -> str | None:
        # normalize a raw url, clean up path, remove fragment, and return None if not http(s) or no host
        href = raw_url.strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            return None

        resolved = urljoin(current_page, href) if current_page else href
        parsed = urlsplit(resolved)

        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https") or not parsed.hostname:
            return None

        host = parsed.hostname.lower()
        netloc = f"{host}:{parsed.port}" if parsed.port not in (None, 80, 443) else host

        path = posixpath.normpath(parsed.path or "/")
        if not path.startswith("/"):
            path = "/" + path
        if path != "/":
            # remove trailing slash for non-root paths
            path = path.rstrip("/")

        return urlunsplit((scheme, netloc, path, parsed.query, ""))

    def is_same_domain(self, url: str) -> bool:
        # check if the url is on the same host as the base_url
        return urlsplit(url).hostname == self.allowed_host

    def should_crawl(self, url: str) -> bool:
        # check if the url is on the same host and not an ignored extension
        if not self.is_same_domain(url):
            return False
        _, ext = posixpath.splitext(urlsplit(url).path.lower())
        return ext not in IGNORED_EXTENSIONS


class WebCrawler:
    # crawl a single domain, fetch pages concurrently, and print discovered links

    def __init__(
        self,
        base_url: str,
        concurrency: int = 15,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url_processor = URLProcessor(base_url)
        self.concurrency = concurrency
        self.timeout = timeout
        self._external_client = client  # if provided, use this client instead of creating a new one

        self.seen_urls: set[str] = set()
        self.results: dict[str, PageResult] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._print_lock = asyncio.Lock()

    @staticmethod
    def extract_links(html: str, page_url: str) -> list[str]:
        # extract all links from the html, normalize them, and return a list of unique links
        soup = BeautifulSoup(html, "html.parser")
        links: dict[str, None] = {}
        for tag in soup.find_all("a", href=True):
            normalized = URLProcessor.normalize(str(tag["href"]), current_page=page_url)
            if normalized:
                links[normalized] = None
        return list(links.keys())

    async def crawl(self) -> list[PageResult]:
        self.seen_urls.add(self.url_processor.base_url)
        self.queue.put_nowait(self.url_processor.base_url)

        if self._external_client is not None:
            await self._run_workers(self._external_client)
        else:
            limits = httpx.Limits(
                max_connections=self.concurrency * 2,
                max_keepalive_connections=self.concurrency,
            )
            async with httpx.AsyncClient(
                limits=limits,
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": "ZegoCrawler/1.0"},
            ) as client:
                await self._run_workers(client)

        return list(self.results.values())

    async def _run_workers(self, client: httpx.AsyncClient) -> None:
        workers = [
            asyncio.create_task(self._worker(client)) for _ in range(self.concurrency)
        ]
        await self.queue.join()
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    async def _worker(self, client: httpx.AsyncClient) -> None:
        while True:
            url = await self.queue.get()
            try:
                await self._process_page(client, url)
            except Exception as exc:
                print(f"[WARN] Failed to process {url}: {exc}", file=sys.stderr)
            finally:
                self.queue.task_done()

    async def _process_page(self, client: httpx.AsyncClient, url: str) -> None:
        try:
            response = await client.get(url)
        except httpx.RequestError:
            return

        if response.status_code != 200:
            return

        content_type = response.headers.get("content-type", "").lower()
        if "text/html" not in content_type:
            return

        final_url = URLProcessor.normalize(str(response.url)) or url
        # only process the page if it's on the same domain and hasn't been seen before
        if not self.url_processor.is_same_domain(final_url) or final_url in self.results:
            return

        links = self.extract_links(response.text, final_url)
        result = PageResult(url=final_url, links=links)
        self.results[final_url] = result

        await self._print_result(result)

        for link in links:
            if link not in self.seen_urls and self.url_processor.should_crawl(link):
                self.seen_urls.add(link)
                self.queue.put_nowait(link)

    async def _print_result(self, result: PageResult) -> None:
        lines = [f"Page: {result.url}"]
        lines.extend(f"  -> {link}" for link in result.links)
        async with self._print_lock:
            print("\n".join(lines) + "\n", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crawl a single domain and print discovered links."
    )
    parser.add_argument("base_url", help="Base URL to crawl (e.g. https://www.zego.com)")
    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=15,
        help="Concurrent workers (default: 15)",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=10.0,
        help="Request timeout in seconds",
    )
    args = parser.parse_args()

    try:
        crawler = WebCrawler(
            args.base_url, concurrency=args.concurrency, timeout=args.timeout
        )
        asyncio.run(crawler.crawl())
    except ValueError as exc:
        sys.exit(f"Error: {exc}")
    except KeyboardInterrupt:
        sys.exit("\nCrawl interrupted.")


if __name__ == "__main__":
    main()
