"""Smart Language review-filter regressions using real user-reported name pairs."""
import pytest

from language_filter import meaningful_name_change


@pytest.mark.parametrize("before,after,useful", [
    # Cosmetic presentation: Unicode casefold, accents and punctuation.
    ("The Toxic Avenger - It Doesn't Matter",
     "The Toxic Avenger - It Doesn’t Matter", False),
    ("LORN - Drawn Out Like an Ache",
     "Lorn - Drawn Out Like an Ache", False),
    ("NTO - Carrousel", "N’to - Carrousel", False),
    ("Virtual Riot - CHROMA", "Virtual Riot - Chroma", False),
    ("Bakermat - Baiana", "Bakermat - Baianá", False),
    ("Jean‐Michel Jarre - Oxygène, Pt. 17",
     "Jean‐Michel Jarre - Oxygène, Part 17", False),
    ("Subtronics feat. Grabbitz - Into Pieces (Wooli x Grabbitz remix)",
     "Subtronics feat. Grabbitz - Into Pieces (Wooli × Grabbitz remix)", False),
    ("Earth, Wind & Fire - Let's Groove (Instrumental)",
     "Earth, Wind & Fire - Let’s Groove (instrumental)", False),
    # Same credited artists with different formatting or order.
    ("TheFatRat & Anjulie - Fly Away",
     "TheFatRat feat. Anjulie - Fly Away", False),
    ("Pizza Hotline x Shirobon - Nightfall",
     "Shirobon & Pizza Hotline - Nightfall", False),
    ("Darren Korb ft. Ashley Barrett - In the Blood",
     "Darren Korb feat. Ashley Barrett - In the Blood", False),
    # Important English-first protection: never suggest a Japanese fallback.
    ("西木康智 - The Sunlands", "西木康智 - サンランド地方", False),
    ("西木康智 - The Highlands", "西木康智 - ハイランド地方", False),
    ("西木康智 - Octopath Traveler - Main Theme",
     "西木康智 - OCTOPATH TRAVELER –メインテーマ–", False),
    # Actual text improvement, typo correction, translation, new credits.
    ("Gareth Coker - Seperated by the Storm",
     "Gareth Coker - Separated by the Storm", True),
    ("Shakkam - Cavers of Noria",
     "Shakkam - Caverns of Noria", True),
    ("Toby Fox - サーカス", "Toby Fox - The Circus", True),
    ("Toby Fox - 夢と希望の平原",
     "Toby Fox - Field of Hopes and Dreams", True),
    ("Skrillex & ناي برغوثي - XENA",
     "Skrillex & Nai Barghouti - Xena", True),
    ("Kevin Penkin ft. 斎藤洸 - VOH",
     "Kevin Penkin ft. Takeshi Saito - VOH", True),
    ("Daft Punk - Within",
     "Daft Punk feat. Chilly Gonzales - Within", True),
    ("KEYGEN CHURCH - Lax ubivarfe afc, quea iuhac befdi ac gori",
     "KEYGEN CHURCH - Lex universa est, quae iubet nasci et mori", True),
])
def test_smart_filter_uses_meaning_not_presentation(before, after, useful):
    assert meaningful_name_change({
        "changed_field": "Title / artist",
        "before_label": before, "after_label": after,
    }) is useful


@pytest.mark.parametrize("before,after,useful", [
    ("LORN", "Lorn", False),
    ("Gregory Reveret", "Grégory Reveret", False),
    ("Virtual Riot & LeKtriQue feat. Leah Culver",
     "Virtual Riot & Lektrique feat. Leah Culver", False),
    ("Lena Raine & 谷岡久美", "Lena Raine & Kumi Tanioka", True),
    ("Amos Roddy", "ToyTree", True),
    ("Various Artists", "NewRetroWave", True),
])
def test_smart_filter_handles_album_artist_changes(before, after, useful):
    assert meaningful_name_change({
        "changed_field": "Album artist",
        "before_label": before, "after_label": after,
    }) is useful


def test_field_level_differences_keep_meaningful_artist_change():
    assert meaningful_name_change({
        "display_diffs": [
            {"field": "TITLE", "before": "Within", "after": "Within"},
            {"field": "ARTIST", "before": "Daft Punk",
             "after": "Daft Punk feat. Chilly Gonzales"},
        ],
    })
    assert not meaningful_name_change({
        "display_diffs": [
            {"field": "TITLE", "before": "It Doesn't Matter",
             "after": "It Doesn’t Matter"},
            {"field": "ARTIST", "before": "LORN", "after": "Lorn"},
        ],
    })
    # Still safe on old persisted reviews with no field-level comparison.
    assert not meaningful_name_change({
        "changed_field": "Album artist",
        "before_label": "LORN", "after_label": "Lorn",
    })
