from paperlens.parsing.page_labels import printed_page_label


def word(text, x0, top, x1, bottom):
    return dict(text=text, x0=x0, top=top, x1=x1, bottom=bottom)


def test_isolated_footer_above_last_six_percent_beats_citation_year():
    words = [
        word("4182", 290, 737, 312, 748),
        word("July", 50, 765, 70, 775),
        word("2020.", 80, 765, 110, 775),
        word("Association", 120, 765, 180, 775),
    ]
    assert printed_page_label(words, 600, 800) == "4182"


def test_a_citation_year_with_trailing_period_is_not_a_decorated_page_number():
    assert printed_page_label([
        word("July", 50, 765, 70, 775),
        word("2020.", 80, 765, 110, 775),
        word("Association", 120, 765, 180, 775),
    ], 600, 800) == ""
