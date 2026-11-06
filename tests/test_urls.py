from neurovex.modules.news.urls import canonicalize_url, is_http_url, title_fingerprint


def test_canonicalize_strips_tracking_and_normalizes():
    a = canonicalize_url("http://WWW.Example.com:443/news/story/?utm_source=x&b=2&a=1#comments")
    assert a == "https://example.com/news/story?a=1&b=2"


def test_canonicalize_same_story_different_links():
    links = [
        "https://example.com/post",
        "https://example.com/post/",
        "http://www.example.com/post?utm_campaign=rss",
        "https://example.com//post#top",
        "https://example.com/post?fbclid=abc",
    ]
    assert len({canonicalize_url(u) for u in links}) == 1


def test_canonicalize_keeps_meaningful_params_and_ports():
    assert canonicalize_url("https://example.com/item?id=42") == "https://example.com/item?id=42"
    assert canonicalize_url("https://example.com:8443/a") == "https://example.com:8443/a"


def test_title_fingerprint_ignores_case_and_punctuation():
    assert title_fingerprint("OpenAI ships GPT-6!") == title_fingerprint("openai ships gpt 6")
    assert title_fingerprint("A") != title_fingerprint("B")


def test_is_http_url():
    assert is_http_url("https://example.com/feed")
    assert not is_http_url("file:///etc/passwd")
    assert not is_http_url("javascript:alert(1)")
    assert not is_http_url("https://")
