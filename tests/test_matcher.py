from app.matcher import match_title, parse_terms, title_hash


def m(title, text):
    return match_title(title, parse_terms(text)) is not None


def test_keyword_examples_from_spec():
    for title in ["The Beatles - Abbey Road FLAC", "Beatles Discography", "Rare Beatles Collection"]:
        assert m(title, "Beatles")


def test_keyword_is_whole_word_and_case_insensitive():
    assert m("the.beatles-abbey_road.2019", "BEATLES")
    assert not m("Beatlesque Pop Compilation", "Beatles")


def test_phrase_examples_from_spec():
    assert m("Michael Jackson - Thriller", '"Michael Jackson"')
    assert m("Michael Jackson Collection FLAC", '"Michael Jackson"')
    assert not m("Jackson Family Collection", '"Michael Jackson"')
    assert not m("Jackson Michael Remix", '"Michael Jackson"')


def test_multiword_keyword_requires_all_words_any_order():
    assert m("Floyd, Pink - The Wall", "Pink Floyd")
    assert not m("Pink - Greatest Hits", "Pink Floyd")


def test_any_term_matches_and_exclusions():
    text = 'Beatles\nMetallica, "The Rolling Stones"\n-karaoke\n-"Greatest Hits"'
    terms = parse_terms(text)
    assert [t.display for t in terms] == ["Beatles", "Metallica", '"The Rolling Stones"', "-karaoke", '-"Greatest Hits"']
    assert m("Metallica - Master of Puppets", text)
    assert m("The Rolling Stones - Sticky Fingers", text)
    assert not m("Beatles Karaoke Party", text)
    assert not m("The Rolling Stones Greatest Hits", text)


def test_title_hash_ignores_punctuation_and_case():
    assert title_hash("The.Beatles-Abbey.Road") == title_hash("the beatles abbey road")


def test_search_variants_for_multiword_terms():
    t = parse_terms('"The Rolling Stones"\nPink Floyd\nBeatles')
    assert t[0].queries == ["the rolling stones", "therollingstones", "the.rolling.stones"]
    assert t[1].queries == ["pink floyd", "pinkfloyd", "pink.floyd"]
    assert t[2].queries == ["beatles"]


def test_run_together_and_dotted_titles_match():
    for title in ["The.Rolling.Stones-Sticky.Fingers", "TheRollingStones - Live 1972",
                  "TheRollingStones2023.FLAC", "the_rolling_stones_hackney_diamonds"]:
        assert m(title, '"The Rolling Stones"'), title
    assert m("PinkFloyd-The.Wall", "Pink Floyd")
    assert not m("Rolling Thunder Stones", '"The Rolling Stones"')
