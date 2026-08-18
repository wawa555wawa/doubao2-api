from doubao2_api.session_refresh import SESSION_COOKIE_NAMES, extract_login_cookies


def make_cookie(name: str) -> dict:
    return {"name": name, "value": f"v-{name}", "domain": ".doubao.com"}


def test_extract_returns_none_when_not_logged_in():
    cookies = [make_cookie("ttwid"), make_cookie("other")]
    assert extract_login_cookies(cookies) is None


def test_extract_returns_all_cookies_when_logged_in():
    marker = next(iter(SESSION_COOKIE_NAMES))
    cookies = [make_cookie("ttwid"), make_cookie(marker)]
    result = extract_login_cookies(cookies)
    assert result == {"ttwid": "v-ttwid", marker: f"v-{marker}"}
