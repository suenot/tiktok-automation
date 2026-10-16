"""Input that leaves the trace a person leaves.

A page sees more than the value of a field. It sees which events arrived, in
what order and how far apart. Measured on a probe page that logs every event
the TikTok profile editor could log:

    locator.fill(text)     49 characters -> 1 beforeinput, 1 input, 0 keydown
    locator.type(delay=55) 49 keystrokes with 0-3 ms between keyup and keydown
    type_text(page, text)  49 keystrokes with 42-186 ms between them, and
                           258 mousemove samples on the way to the field
                           against 27 for a plain locator.click()

The first is not typing at all. The second is typing by a machine with a
metronome. Only the third is indistinguishable from a person at the level a
page can observe, which is why every control a human would touch goes through
this module.

Events produced here are real browser events: isTrusted is true for all of
them. Nothing in this module fabricates an event object.
"""
import random

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
