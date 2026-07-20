from discovery.login import _is_logged_in


def test_logged_in_detection():
    assert _is_logged_in("https://www.linkedin.com/feed/") is True
    assert _is_logged_in("https://www.linkedin.com/login") is False
    assert _is_logged_in("https://www.linkedin.com/checkpoint/challenge") is False
