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
