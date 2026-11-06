"""Default feeds seeded on first start. Add or remove your own via the UI, API or CLI."""

DEFAULT_SOURCES: list[dict[str, str]] = [
    {"name": "Hacker News", "url": "https://hnrss.org/frontpage", "category": "Technology"},
    {
        "name": "Ars Technica",
        "url": "https://feeds.arstechnica.com/arstechnica/index",
        "category": "Technology",
    },
    {
        "name": "The Verge",
        "url": "https://www.theverge.com/rss/index.xml",
        "category": "Technology",
    },
    {
        "name": "Krebs on Security",
        "url": "https://krebsonsecurity.com/feed/",
        "category": "Cybersecurity",
    },
    {
        "name": "BleepingComputer",
        "url": "https://www.bleepingcomputer.com/feed/",
        "category": "Cybersecurity",
    },
    {
        "name": "The Hacker News",
        "url": "https://feeds.feedburner.com/TheHackersNews",
        "category": "Cybersecurity",
    },
    {
        "name": "TechCrunch AI",
        "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
        "category": "AI",
    },
]
