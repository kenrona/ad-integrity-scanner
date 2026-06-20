from app.normalize import normalize_url


def test_scheme_and_host_lowercased_default_https():
    n = normalize_url("Example.COM/Path")
    assert n.url == "https://example.com/Path"  # host lowercased, path case kept
    assert n.host == "example.com"
    assert n.domain == "example.com"


def test_default_port_stripped_nondefault_kept():
    assert normalize_url("http://example.com:80/a").url == "http://example.com/a"
    assert normalize_url("http://example.com:8080/a").url == "http://example.com:8080/a"


def test_fragment_dropped_and_trailing_slash_normalized():
    a = normalize_url("https://example.com/a/b/#section")
    b = normalize_url("https://example.com/a/b")
    assert a.url == b.url == "https://example.com/a/b"
    assert a.url_hash == b.url_hash


def test_root_path_keeps_single_slash():
    assert normalize_url("https://example.com").url == "https://example.com/"
    assert normalize_url("https://example.com/").url == "https://example.com/"


def test_tracking_params_stripped_and_query_sorted():
    n = normalize_url("https://example.com/p?b=2&utm_source=x&a=1&gclid=zzz")
    assert n.url == "https://example.com/p?a=1&b=2"


def test_tracking_strip_can_be_disabled():
    n = normalize_url("https://example.com/p?utm_source=x", strip_tracking=False)
    assert "utm_source=x" in n.url


def test_registrable_domain_extraction():
    assert normalize_url("https://news.example.co.uk/story").domain == "example.co.uk"


def test_same_logical_page_hashes_equal():
    a = normalize_url("HTTP://Example.com:80/Path/?utm_medium=cpc&x=1#frag")
    b = normalize_url("http://example.com/Path?x=1")
    assert a.url_hash == b.url_hash


def test_invalid_urls_raise():
    for bad in ["", "   ", "ftp://example.com", "https://", "not a url with spaces"]:
        try:
            normalize_url(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")
