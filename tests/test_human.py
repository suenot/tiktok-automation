import random

from tiktok_automation import human


def test_keystroke_pace_varies_the_way_a_hand_does():
    rng = random.Random(7)
    values = human.intervals("Word origins, island stories & Caribbean culture.", rng)
    assert len(values) == 49
    assert min(values) >= human.KEY_MINIMUM_MS
    # A metronome is the tell: locator.type(delay=55) measured 0-3 ms of spread.
    spread = max(values) - min(values)
    assert spread > 60, f"Keystroke pace is too regular to pass for a person: {spread} ms"
    assert len(set(values)) > 20


def test_a_space_is_not_faster_than_a_letter():
    rng = random.Random(11)
    spaces = [value for character, value in
              zip("a a a a a a a a", human.intervals("a a a a a a a a", rng))
              if character == " "]
    assert spaces and all(value >= human.KEY_MINIMUM_MS for value in spaces)


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
