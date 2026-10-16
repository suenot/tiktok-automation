"""Input that leaves the trace a person leaves.

A page sees more than the value of a field. It sees which events arrived, in
what order, how far apart, and whether the browser marked them as
user-generated. Measured on a probe page that logs every event the TikTok
profile editor could log, entering the same 49-character text:

    locator.fill(text)     49 characters -> 1 beforeinput, 1 input, 0 keydown
    execCommand insertText 49 characters -> 1 input, no beforeinput, 0 keydown
    locator.type(delay=55) 49 keystrokes with 0-3 ms between keyup and keydown
    fabricated paste event isTrusted: false, clipboardData not a DataTransfer
    type_text(page, text)  49 keystrokes 42-186 ms apart, 258 mousemove samples
                           on the way to the field against 27 for a bare click

Nothing here fabricates an event: every event this module causes comes from
the browser and carries isTrusted: true.

Typing is not always the human answer. A person types what they could write
from memory and pastes what they composed elsewhere, and nobody types three
hundred characters of caption and hashtags without one correction. Two
measurements set the rule:

  * A real clipboard paste, driven by writing to the system clipboard and
    pressing the paste key, reaches a DraftJS editor's React state exactly as
    typing does, and the page sees `isTrusted: true` with a genuine
    `DataTransfer`. It is indistinguishable from a person pasting.
  * Typing "a<palm emoji>b" produced three input events but only two keydowns:
    a character outside ASCII arrives without a keystroke, which is the same
    "text appeared, nobody typed" signature in miniature.

So `enter_text` types short plain-ASCII text and pastes everything else.
"""
import random
import subprocess
import sys

# Timing drawn from the measurements above; the spread matters more than the
# centre, because a constant interval is the thing that reads as automation.
KEY_INTERVAL_MS = (105, 38)      # mean, standard deviation
KEY_MINIMUM_MS = 28
WORD_BREAK_MS = (10, 60)         # extra pause after a space
MOVE_STEPS = (22, 34)
MOVE_STEP_MS = (6, 18)
PRESS_MS = (55, 130)
AIM_MS = (90, 260)
SETTLE_MS = (250, 900)
PASTE_READ_MS = 400              # let the browser read the clipboard before restoring it

# Longer than a person would retype from memory; captions live well above it.
TYPED_LIMIT = 120

PASTE_KEY = "Meta+V" if sys.platform == "darwin" else "Control+V"
_CLIPBOARD = {"darwin": (["pbcopy"], ["pbpaste"]),
              "linux": (["wl-copy"], ["wl-paste", "--no-newline"])}


def _cursor(page):
    return getattr(page, "_human_cursor", (12.0, 12.0))


async def move_to(page, x, y, rng=random):
    """Travel to a point along an eased arc instead of teleporting to it."""
    start_x, start_y = _cursor(page)
    steps = rng.randint(*MOVE_STEPS)
    bow = rng.uniform(-0.22, 0.22)
    distance = ((x - start_x) ** 2 + (y - start_y) ** 2) ** 0.5
    for index in range(1, steps + 1):
        progress = index / steps
        eased = progress * progress * (3 - 2 * progress)
        arc = bow * (1 - (2 * progress - 1) ** 2) * distance * 0.12
        await page.mouse.move(start_x + (x - start_x) * eased,
                              start_y + (y - start_y) * eased + arc)
        await page.wait_for_timeout(rng.randint(*MOVE_STEP_MS))
    page._human_cursor = (x, y)


async def click(page, locator, rng=random):
    """Click where a hand would land: inside the control, not dead centre."""
    await locator.scroll_into_view_if_needed()
    box = await locator.bounding_box()
    if not box:
        raise ValueError("Control is not visible; cannot click it as a person would.")
    x = box["x"] + box["width"] * rng.uniform(0.35, 0.65)
    y = box["y"] + box["height"] * rng.uniform(0.35, 0.65)
    await move_to(page, x, y, rng)
    await page.wait_for_timeout(rng.randint(*AIM_MS))
    await page.mouse.down()
    await page.wait_for_timeout(rng.randint(*PRESS_MS))
    await page.mouse.up()


def intervals(text, rng=random):
    """The pause before each character, in milliseconds."""
    values = []
    for character in text:
        delay = rng.gauss(*KEY_INTERVAL_MS)
        if character == " ":
            delay += rng.randint(*WORD_BREAK_MS)
        values.append(max(KEY_MINIMUM_MS, int(delay)))
    return values


async def type_text(page, text, rng=random):
    """Type one character at a time, at a pace that varies the way a hand does."""
    for character, delay in zip(text, intervals(text, rng)):
        await page.keyboard.type(character)
        await page.wait_for_timeout(delay)


async def settle(page, rng=random):
    """Pause the way a person does between finishing one control and the next."""
    await page.wait_for_timeout(rng.randint(*SETTLE_MS))


def would_type(text):
    """Would a person type this from memory, or paste it from somewhere?

    Short, single-line, plain ASCII is typed. Anything longer, anything with a
    line break, and anything outside ASCII is pasted, because typing it leaves
    a trace no hand leaves: half an hour of flawless keystrokes, or characters
    that arrive with no keystroke at all.
    """
    return len(text) <= TYPED_LIMIT and "\n" not in text and text.isascii()


def clipboard_tools():
    """The write and read commands for this platform, or None if there are none."""
    return _CLIPBOARD.get("linux" if sys.platform.startswith("linux") else sys.platform)


def read_clipboard():
    tools = clipboard_tools()
    if not tools:
        return None
    try:
        done = subprocess.run(tools[1], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def write_clipboard(text):
    tools = clipboard_tools()
    if not tools:
        return False
    try:
        return subprocess.run(tools[0], input=text, text=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


async def paste(page, text, rng=random):
    """Paste through the system clipboard, so the browser raises the event.

    Returns True when the paste key was pressed. The previous clipboard text
    is put back afterwards; clipboard contents that are not text cannot be
    preserved, which is the price of a genuine paste event.
    """
    previous = read_clipboard()
    if not write_clipboard(text):
        return False
    try:
        await page.wait_for_timeout(rng.randint(120, 320))
        await page.keyboard.press(PASTE_KEY)
        await page.wait_for_timeout(PASTE_READ_MS)
    finally:
        if previous is not None:
            write_clipboard(previous)
    return True


async def enter_text(page, locator, text, rng=random, read_back=None):
    """Put text into a control the way a person would, and say which way that was.

    `read_back` is an async callable returning what the control now holds. It
    is what makes pasting safe to attempt: a browser without access to the
    system clipboard, a headless one among them, swallows the paste key
    silently, and the field is left empty. When the text did not land, the
    control is cleared and typed into instead. Typing is a weaker imitation of
    a person than pasting, but it is still the browser's own events; the one
    thing never done here is fabricating an event.
    """
    await click(page, locator, rng)
    await settle(page, rng)
    if not would_type(text) and await paste(page, text, rng):
        if read_back is None or await read_back() == text:
            return "pasted"
        await locator.press("ControlOrMeta+A")
        await locator.press("Backspace")
        await page.wait_for_timeout(120)
        await type_text(page, text, rng)
        return "typed-after-paste-was-not-received"
    await type_text(page, text, rng)
    return "typed"
