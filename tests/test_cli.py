import pytest

from reelbot.cli import parse_picks

NAMES = ["Juno", "Mochi"]


@pytest.mark.parametrize(
    "tokens",
    [["Juno", "3", "Mochi", "1"], ["juno=3", "mochi=1"], ["Juno 3, Mochi 1"], ["JUNO", "3", "mochi", "1"]],
)
def test_pick_accepts_the_ways_people_type_it(tokens):
    assert parse_picks(tokens, NAMES) == [("Juno", 2), ("Mochi", 0)]


@pytest.mark.parametrize(
    "tokens, msg",
    [
        (["Juno"], "usage"),
        (["Batman", "1"], "unknown character 'Batman'"),
        (["Juno", "0"], "must be a number"),
        (["Juno", "three"], "must be a number"),
    ],
)
def test_pick_rejects_bad_input_with_a_clear_message(tokens, msg):
    with pytest.raises(SystemExit, match=msg):
        parse_picks(tokens, NAMES)
