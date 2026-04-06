"""
Shared avatar-rendering utilities for Discord plugin actions.

Both ChangeVoiceChannel and UserVolume import from here so that colour
choices, rendering logic, and constants stay in one place.
"""

from PIL import Image, ImageDraw, ImageFont

# Canvas size used for all Stream Deck key renders in this plugin.
BUTTON_SIZE = 72

# Speaking-indicator ring colour (Discord green) and ring thickness.
SPEAKING_COLOR = (88, 201, 96, 255)
RING_WIDTH = 3

# Ordered placeholder colours assigned to users who have no profile picture.
# Colour is chosen deterministically from the Discord user ID so the same
# user always gets the same colour.  The list repeats when there are more
# users than colours.
PLACEHOLDER_COLORS = [
    (88,  101, 242, 255),  # Discord blurple
    (87,  242, 135, 255),  # Discord green
    (254, 231, 92,  255),  # Discord yellow
    (235, 69,  158, 255),  # Discord fuchsia
    (237, 66,  69,  255),  # Discord red
    (52,  152, 219, 255),  # Steel blue
    (155, 89,  182, 255),  # Purple
    (230, 126, 34,  255),  # Orange
]

try:
    _placeholder_font = ImageFont.load_default(size=28)
except Exception:
    _placeholder_font = ImageFont.load_default()


def placeholder_color(user_id: str) -> tuple:
    """Return a deterministic placeholder colour for *user_id*."""
    try:
        idx = int(user_id) % len(PLACEHOLDER_COLORS)
    except (ValueError, TypeError):
        idx = abs(hash(user_id)) % len(PLACEHOLDER_COLORS)
    return PLACEHOLDER_COLORS[idx]


def make_placeholder_avatar(display_name: str, user_id: str, size: int) -> Image.Image:
    """Return a circular avatar with a solid colour background and the user's initial."""
    color = placeholder_color(user_id)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((0, 0, size - 1, size - 1), fill=color)
    initial = display_name[0].upper() if display_name else "?"
    bbox = draw.textbbox((0, 0), initial, font=_placeholder_font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((size - tw) // 2 - bbox[0], (size - th) // 2 - bbox[1]),
        initial,
        fill=(255, 255, 255, 255),
        font=_placeholder_font,
    )
    return img


def make_circle_avatar(img: Image.Image, size: int) -> Image.Image:
    """Resize *img* to *size*×*size* and clip it to a circle."""
    img = img.convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    result.paste(img, mask=mask)
    return result


def draw_speaking_ring(img: Image.Image, size: int) -> Image.Image:
    """Overlay a green speaking-indicator ring onto *img*."""
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    half = RING_WIDTH // 2
    draw.ellipse(
        (half, half, size - 1 - half, size - 1 - half),
        outline=SPEAKING_COLOR,
        width=RING_WIDTH,
    )
    result = img.copy()
    result.paste(overlay, mask=overlay)
    return result


# Mute indicator: semi-transparent dark overlay with a red diagonal slash.
MUTE_OVERLAY_COLOR = (0, 0, 0, 140)
MUTE_SLASH_COLOR = (237, 66, 69, 255)  # Discord red
MUTE_SLASH_WIDTH = 4


def draw_mute_overlay(img: Image.Image, size: int) -> Image.Image:
    """Overlay a mute indicator (dimmed circle + red slash) onto *img*."""
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.ellipse((0, 0, size - 1, size - 1), fill=MUTE_OVERLAY_COLOR)
    pad = size // 6
    draw.line(
        (pad, pad, size - 1 - pad, size - 1 - pad),
        fill=MUTE_SLASH_COLOR,
        width=MUTE_SLASH_WIDTH,
    )
    result = img.copy()
    result.paste(overlay, mask=overlay)
    return result

def compose_overlapping_avatars(
    avatars: list[tuple[Image.Image, bool, bool]],
    canvas_size: int,
    front_index: int | None = None,
) -> Image.Image:
    """Compose avatars in an overlapping stack, with *front_index* on top.

    *avatars* is a list of ``(image, is_speaking, is_muted)`` tuples.
    *front_index* is the index of the avatar to place in front.  When ``None``
    (the default), no reordering is done and the last avatar is on top.
    """
    canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 255))
    n = len(avatars)
    if n == 0:
        return canvas

    # Single avatar — render full size, centred.
    if n == 1:
        img, speaking, muted = avatars[0]
        av = make_circle_avatar(img, canvas_size)
        if speaking:
            av = draw_speaking_ring(av, canvas_size)
        if muted:
            av = draw_mute_overlay(av, canvas_size)
        canvas.paste(av, (0, 0), av)
        return canvas

    # Avatar diameter: large enough to be readable, small enough to overlap.
    avatar_size = int(canvas_size * 0.65)

    # Build a display order where the front avatar is placed at the centre
    # position and the remaining avatars fill the other slots, preserving
    # their relative order.  The front avatar is painted last (on top).
    if front_index is not None and 0 <= front_index < n:
        others = [i for i in range(n) if i != front_index]
        centre = n // 2
        display_order = others[:centre] + [front_index] + others[centre:]
    else:
        display_order = list(range(n))

    # Spread slots horizontally across the canvas with even overlap.
    total_width = avatar_size + (n - 1) * (avatar_size // 2)
    x_start = (canvas_size - total_width) // 2
    y = (canvas_size - avatar_size) // 2

    # Map each original avatar index to the x position of its assigned slot.
    positions = {}
    for slot, orig_idx in enumerate(display_order):
        positions[orig_idx] = x_start + slot * (avatar_size // 2)

    # Paint order: everything except front first, then front on top.
    if front_index is not None and 0 <= front_index < n:
        paint_order = [i for i in display_order if i != front_index] + [front_index]
    else:
        paint_order = display_order

    for idx in paint_order:
        img, speaking, muted = avatars[idx]
        av = make_circle_avatar(img, avatar_size)
        if speaking:
            av = draw_speaking_ring(av, avatar_size)
        if muted:
            av = draw_mute_overlay(av, avatar_size)
        canvas.paste(av, (positions[idx], y), av)

    return canvas
