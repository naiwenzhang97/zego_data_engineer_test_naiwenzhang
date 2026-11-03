## 1. Requirements

```bash
pip install -r requirements.txt
pytest
python crawler.py https://www.zego.com
```

Optional flags: `-c/--concurrency` (default 15), `-t/--timeout` (default 10.0 seconds).


## 2. Process

## What each part does

### PageResult
Holds one crawled page: its URL and the links found on it.

### URLProcessor
- `normalize` — turn relative links into absolute URLs, drop fragments / mailto / javascript, tidy the path.
- `is_same_domain` — true only if the hostname matches the start site exactly (no subdomains).
- `should_crawl` — same host and not a static file (pdf, image, css, js, etc.).

### WebCrawler
- `extract_links` — parse `<a href>` tags, normalize, dedupe.
- `crawl` — seed the queue and run workers; returns all `PageResult`s.
- `_run_workers` — start N async workers, wait until the queue is empty.
- `_worker` — loop: take a URL from the queue and process it.
- `_process_page` — GET the page; if HTML, save links, print them, enqueue same-domain pages not seen yet.
- `_print_result` — print one page and its links (lock so workers don’t interleave).

### main
CLI entry: parse `base_url`, `-c` concurrency, `-t` timeout, then run `crawl()`.

