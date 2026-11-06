"""A catalog of feeds you can add with one click ("Discover feeds").

Every URL was checked to return a valid feed on 2026-10-09.
"""

from __future__ import annotations

CATALOG: dict[str, list[dict[str, str]]] = {
    "AI": [
        {"name": "OpenAI News", "url": "https://openai.com/news/rss.xml"},
        {"name": "Google AI Blog", "url": "https://blog.google/technology/ai/rss/"},
        {"name": "Hugging Face Blog", "url": "https://huggingface.co/blog/feed.xml"},
        {
            "name": "MIT Technology Review: AI",
            "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed",
        },
        {"name": "Simon Willison", "url": "https://simonwillison.net/atom/everything/"},
        {"name": "VentureBeat AI", "url": "https://feeds.feedburner.com/venturebeat/SZYF"},
        {
            "name": "TechCrunch AI",
            "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
        },
    ],
    "Cybersecurity": [
        {"name": "CISA Advisories", "url": "https://www.cisa.gov/cybersecurity-advisories/all.xml"},
        {"name": "Krebs on Security", "url": "https://krebsonsecurity.com/feed/"},
        {"name": "BleepingComputer", "url": "https://www.bleepingcomputer.com/feed/"},
        {"name": "The Hacker News", "url": "https://feeds.feedburner.com/TheHackersNews"},
        {"name": "Schneier on Security", "url": "https://www.schneier.com/feed/atom/"},
        {"name": "SecurityWeek", "url": "https://www.securityweek.com/feed/"},
        {"name": "Dark Reading", "url": "https://www.darkreading.com/rss.xml"},
    ],
    "Programming": [
        {"name": "GitHub Blog", "url": "https://github.blog/feed/"},
        {"name": "Microsoft DevBlogs", "url": "https://devblogs.microsoft.com/feed/"},
        {"name": "Rust Blog", "url": "https://blog.rust-lang.org/feed.xml"},
        {"name": "Python Insider", "url": "https://blog.python.org/feeds/posts/default"},
        {"name": "InfoQ", "url": "https://feed.infoq.com/"},
        {"name": "Lobsters", "url": "https://lobste.rs/rss"},
        {"name": "Hacker News", "url": "https://hnrss.org/frontpage"},
    ],
    "Cloud & DevOps": [
        {"name": "AWS News Blog", "url": "https://aws.amazon.com/blogs/aws/feed/"},
        {"name": "Cloudflare Blog", "url": "https://blog.cloudflare.com/rss/"},
        {"name": "The New Stack", "url": "https://thenewstack.io/feed/"},
        {"name": "Phoronix (Linux)", "url": "https://www.phoronix.com/rss.php"},
    ],
    "Hardware & Gadgets": [
        {"name": "Tom's Hardware", "url": "https://www.tomshardware.com/feeds/all"},
        {"name": "Windows Central", "url": "https://www.windowscentral.com/feeds/all"},
        {"name": "Engadget", "url": "https://www.engadget.com/rss.xml"},
        {"name": "9to5Google", "url": "https://9to5google.com/feed/"},
    ],
    "General tech": [
        {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index"},
        {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml"},
        {"name": "Wired", "url": "https://www.wired.com/feed/rss"},
        {"name": "The Register", "url": "https://www.theregister.com/headlines.atom"},
        {"name": "ZDNET", "url": "https://www.zdnet.com/news/rss.xml"},
    ],
    "Science": [
        {"name": "Quanta Magazine", "url": "https://api.quantamagazine.org/feed/"},
    ],
}
