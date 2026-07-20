from submitters import selectors


def test_selector_lists_nonempty_and_joinable():
    for name in ["EASY_APPLY_BUTTON", "SUBMIT_BUTTON", "NEXT_BUTTON",
                 "DISCARD_BUTTON", "SUCCESS_DIALOG"]:
        val = getattr(selectors, name)
        assert isinstance(val, list) and val
    joined = selectors.join(selectors.SUBMIT_BUTTON)
    assert "," in joined or joined == selectors.SUBMIT_BUTTON[0]
