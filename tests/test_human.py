import random
import statistics

from tiktok_automation import human


SENTENCE = ("Which word did not come from the Caribbean? Six words, one odd one "
            "out. Watch for the reveal. #WordOrigins")


def test_keystroke_pace_is_shaped_like_a_hand_not_a_metronome():
    rng = random.Random(3)
    values = human.intervals(SENTENCE, rng)
    assert len(values) == len(SENTENCE)
    assert min(values) >= human.KEY_MINIMUM_MS
    # locator.type(delay=55) measured 0-3 ms of spread; fill none at all.
    assert max(values) - min(values) > 200
    # Real intervals pile up near the median and trail off to the right, so
    # the mean sits well above the median. A symmetric draw does not do that.
    assert statistics.mean(values) > statistics.median(values) * 1.15


def test_the_sentence_interrupts_the_typing():
    rng = random.Random(3)
    values = human.intervals(SENTENCE, rng)
    assert sum(1 for value in values if value > 400) >= 3, \
        "A person stops now and then; this trace never does"


def test_a_line_break_costs_more_than_a_letter():
    rng = random.Random(5)
    letters = human.intervals("aaaaaaaaaa", random.Random(5))
    breaks = [value for character, value
              in zip("a\na\na\na\na\n", human.intervals("a\na\na\na\na\n", rng))
              if character == "\n"]
    assert min(breaks) > max(letters)


def test_the_plan_types_the_exact_text_even_when_it_slips():
    for seed in range(40):
        typed = ""
        for key, _ in human.plan(SENTENCE, random.Random(seed)):
            typed = typed[:-1] if key == "\b" else typed + key
        assert typed == SENTENCE, f"seed {seed} did not reconstruct the text"


def test_slips_happen_and_are_taken_back_immediately():
    corrections = 0
    for seed in range(40):
        steps = human.plan(SENTENCE, random.Random(seed))
        for index, (key, _) in enumerate(steps):
            if key == "\b":
                corrections += 1
                assert index > 0 and steps[index - 1][0] != "\b"
    assert corrections > 0, "No slip in 40 passes; a long caption is never flawless"


def test_a_hashtag_is_never_mistyped():
    """A half-typed tag opens the platform's suggestion list.

    A correction inside one would then be fighting that UI, so slips stop at
    the # or @ and resume at the next space.
    """
    text = "#WordOrigins #Caribbean @someone done"
    for seed in range(60):
        steps = human.plan(text, random.Random(seed))
        typed = ""
        for key, _ in steps:
            if key == "\b":
                assert "#" not in typed.split(" ")[-1] and "@" not in typed.split(" ")[-1]
                typed = typed[:-1]
            else:
                typed += key


def test_a_person_types_what_they_know_and_pastes_what_they_composed():
    assert human.would_type("Word origins, island stories & Caribbean culture.")
    # A caption is composed elsewhere; typing one flawlessly is not human.
    assert not human.would_type("x" * (human.TYPED_LIMIT + 1))
    assert not human.would_type("first line\nsecond line")
    # A typed emoji arrives with no keystroke at all, so it must be pasted.
    assert not human.would_type("Which word is Caribbean? \U0001F334")


def test_text_splits_into_what_a_keyboard_can_produce_and_what_it_cannot():
    runs = human.segments("Which word? \U0001F334 Six words.")
    assert "".join(run for _, run in runs) == "Which word? \U0001F334 Six words."
    assert [typable for typable, _ in runs] == [True, False, True]
    assert human.segments("plain ascii only") == [(True, "plain ascii only")]


def test_clipboard_round_trip_and_restore():
    if not human.clipboard_tools():
        return  # no clipboard on this platform; enter_text falls back to typing
    before = human.read_clipboard()
    assert human.write_clipboard("caribbean awesome probe")
    assert human.read_clipboard() == "caribbean awesome probe"
    if before is not None:
        human.write_clipboard(before)
        assert human.read_clipboard() == before


def test_no_fabricated_browser_events_in_the_publisher():
    """A page can tell a dispatched event from a real one; never send one.

    The caption field used to receive a hand-built paste event, which carries
    isTrusted: false and a clipboardData that is not a DataTransfer.
    """
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[1] / "src" / "tiktok_automation"
    for path in source.glob("*.py"):
        code = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                         if not line.strip().startswith("#"))
        for marker in ("dispatchEvent", "new Event(", "new MouseEvent(", "new KeyboardEvent("):
            assert marker not in code, f"{path.name} fabricates browser events: {marker}"
