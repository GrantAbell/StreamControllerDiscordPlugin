import io
import math
from enum import StrEnum

import requests
from loguru import logger as log
from PIL import Image, ImageDraw

from .DiscordCore import DiscordCore
from src.backend.PluginManager.EventAssigner import EventAssigner
from src.backend.PluginManager.InputBases import Input

from GtkHelper.GenerativeUI.EntryRow import EntryRow

from ..discordrpc.commands import (
    VOICE_CHANNEL_SELECT,
    GET_CHANNEL,
    GET_GUILD,
    VOICE_STATE_CREATE,
    VOICE_STATE_DELETE,
    SPEAKING_START,
    SPEAKING_STOP,
)

from GtkHelper.GenerativeUI.ComboRow import ComboRow

# Button canvas size (Stream Deck key render size)
_BUTTON_SIZE = 72

# Speaking indicator ring colour (Discord green)
_SPEAKING_COLOR = (88, 201, 96, 255)
_RING_WIDTH = 3


class Icons(StrEnum):
    VOICE_CHANNEL_ACTIVE = "voice-active"
    VOICE_CHANNEL_INACTIVE = "voice-inactive"


def _make_circle_avatar(img: Image.Image, size: int) -> Image.Image:
    """Resize *img* to *size*×*size* and clip it to a circle."""
    img = img.convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    result.paste(img, mask=mask)
    return result


def _draw_speaking_ring(img: Image.Image, size: int) -> Image.Image:
    """Draw a green ring around an avatar image."""
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    half = _RING_WIDTH // 2
    draw.ellipse(
        (half, half, size - 1 - half, size - 1 - half),
        outline=_SPEAKING_COLOR,
        width=_RING_WIDTH,
    )
    result = img.copy()
    result.paste(overlay, mask=overlay)
    return result


def _compose_avatars(avatars: list[tuple[Image.Image, bool]]) -> Image.Image:
    """Compose up to 4 avatar images (with optional speaking ring) onto a button canvas.

    *avatars* is a list of ``(image, is_speaking)`` tuples.
    """
    canvas = Image.new("RGBA", (_BUTTON_SIZE, _BUTTON_SIZE), (0, 0, 0, 255))
    n = min(len(avatars), 4)
    if n == 0:
        return canvas

    # Determine grid: 1→full, 2→side-by-side, 3-4→2×2
    if n == 1:
        size = _BUTTON_SIZE
        positions = [(0, 0)]
    elif n == 2:
        size = _BUTTON_SIZE // 2
        positions = [(0, size // 2), (size, size // 2)]  # centred vertically
    else:
        size = _BUTTON_SIZE // 2
        positions = [(0, 0), (size, 0), (0, size), (size, size)]

    for i, (img, speaking) in enumerate(avatars[:n]):
        avatar = _make_circle_avatar(img, size)
        if speaking:
            avatar = _draw_speaking_ring(avatar, size)
        x, y = positions[i]
        canvas.paste(avatar, (x, y), avatar)

    return canvas


class ChangeVoiceChannel(DiscordCore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_configuration = True
        self._current_channel: str = ""
        self.icon_keys = [Icons.VOICE_CHANNEL_ACTIVE, Icons.VOICE_CHANNEL_INACTIVE]
        self.current_icon = self.get_icon(Icons.VOICE_CHANNEL_INACTIVE)
        self.icon_name = Icons.VOICE_CHANNEL_INACTIVE

        # Guild info (for fallback display when not in channel)
        self._guild_id: str = None
        self._guild_name: str = None
        self._guild_icon_image: Image.Image = None
        self._guild_channel_id: str = None

        # Voice channel / avatar state
        self._connected_channel_id: str = None  # channel we're currently in
        self._users: dict = {}  # user_id → {username, avatar_hash, avatar_img}
        self._speaking: set = set()  # user_ids currently speaking

    def on_ready(self):
        super().on_ready()
        self.plugin_base.connect_to_event(
            event_id=f"{self.plugin_base.get_plugin_id()}::{VOICE_CHANNEL_SELECT}",
            callback=self._on_voice_channel_select,
        )
        self.plugin_base.connect_to_event(
            event_id=f"{self.plugin_base.get_plugin_id()}::{GET_CHANNEL}",
            callback=self._on_get_channel,
        )
        self.plugin_base.connect_to_event(
            event_id=f"{self.plugin_base.get_plugin_id()}::{GET_GUILD}",
            callback=self._on_get_guild,
        )
        self.plugin_base.connect_to_event(
            event_id=f"{self.plugin_base.get_plugin_id()}::{SPEAKING_START}",
            callback=self._on_speaking_start,
        )
        self.plugin_base.connect_to_event(
            event_id=f"{self.plugin_base.get_plugin_id()}::{SPEAKING_STOP}",
            callback=self._on_speaking_stop,
        )
        self.plugin_base.connect_to_event(
            event_id=f"{self.plugin_base.get_plugin_id()}::{VOICE_STATE_CREATE}",
            callback=self._on_voice_state_create,
        )
        self.plugin_base.connect_to_event(
            event_id=f"{self.plugin_base.get_plugin_id()}::{VOICE_STATE_DELETE}",
            callback=self._on_voice_state_delete,
        )
        # Eagerly fetch guild info for already-configured channel
        self._request_guild_info()

    # ------------------------------------------------------------------
    # Voice channel select
    # ------------------------------------------------------------------

    def _on_voice_channel_select(self, *args, **kwargs):
        if not self.backend:
            self.show_error()
            return
        self.hide_error()
        data = args[1] if len(args) > 1 else None
        new_channel = data.get("channel_id", None) if data else None

        # Leaving or switching – unsubscribe old channel
        if self._connected_channel_id and self._connected_channel_id != new_channel:
            try:
                self.backend.unsubscribe_voice_states(self._connected_channel_id)
                self.backend.unsubscribe_speaking(self._connected_channel_id)
            except Exception as ex:
                log.error(f"Failed to unsubscribe from channel: {ex}")
            self._users.clear()
            self._speaking.clear()

        configured = self._channel_row.get_value()

        if new_channel is None:
            # Disconnected
            self._connected_channel_id = None
            self._current_channel = ""
            self.icon_name = Icons.VOICE_CHANNEL_INACTIVE
            self.current_icon = self.get_icon(self.icon_name)
            self._render_button()
        elif new_channel == configured:
            # Connected to our configured channel
            self._connected_channel_id = new_channel
            self._current_channel = new_channel

            # Subscribe to speaking + voice state changes
            try:
                self.backend.subscribe_voice_states(new_channel)
                self.backend.subscribe_speaking(new_channel)
                # Fetch full user list
                self.backend.get_channel(new_channel)
            except Exception as ex:
                log.error(f"Failed to subscribe to channel events: {ex}")
        else:
            # Connected, but to a different channel than configured
            self._connected_channel_id = new_channel
            self._current_channel = new_channel
            self.icon_name = Icons.VOICE_CHANNEL_ACTIVE
            self.current_icon = self.get_icon(self.icon_name)
            # Still request guild info for the configured channel button display
            self._request_guild_info()
            self._render_button()

    # ------------------------------------------------------------------
    # Voice state events (join/leave)
    # ------------------------------------------------------------------

    def _on_voice_state_create(self, *args, **kwargs):
        data = args[1] if len(args) > 1 else None
        if not data:
            return
        user_data = data.get("user", {})
        user_id = user_data.get("id")
        if not user_id or user_id == self.backend.current_user_id:
            return
        if user_id not in self._users:
            self._users[user_id] = {
                "username": user_data.get("username", ""),
                "avatar_hash": user_data.get("avatar"),
                "avatar_img": None,
            }
            self.plugin_base._thread_pool.submit(self._fetch_avatar, user_id)

    def _on_voice_state_delete(self, *args, **kwargs):
        data = args[1] if len(args) > 1 else None
        if not data:
            return
        user_data = data.get("user", {})
        user_id = user_data.get("id")
        if not user_id:
            return
        self._users.pop(user_id, None)
        self._speaking.discard(user_id)
        self._render_button()

    # ------------------------------------------------------------------
    # Speaking events
    # ------------------------------------------------------------------

    def _on_speaking_start(self, *args, **kwargs):
        data = args[1] if len(args) > 1 else None
        if not data:
            return
        user_id = str(data.get("user_id", ""))
        if not user_id:
            return
        self._speaking.add(user_id)
        self._render_button()

    def _on_speaking_stop(self, *args, **kwargs):
        data = args[1] if len(args) > 1 else None
        if not data:
            return
        user_id = str(data.get("user_id", ""))
        if not user_id:
            return
        self._speaking.discard(user_id)
        self._render_button()

    # ------------------------------------------------------------------
    # Channel / guild info (for fallback display)
    # ------------------------------------------------------------------

    def _request_guild_info(self):
        if not self.backend:
            return
        channel = self._channel_row.get_value()
        if not channel or channel == self._guild_channel_id:
            return
        try:
            self.backend.get_channel(channel)
        except Exception as ex:
            log.error(f"Failed to request channel info: {ex}")

    def _on_get_channel(self, *args, **kwargs):
        data = args[1] if len(args) > 1 else None
        if not data:
            return
        channel_id = data.get("id")
        configured_channel = self._channel_row.get_value()

        # ---------- populate users when joining our configured channel ----------
        if channel_id == self._connected_channel_id == configured_channel:
            current_user_id = self.backend.current_user_id
            for vs in data.get("voice_states", []):
                user_data = vs.get("user", {})
                uid = user_data.get("id")
                if not uid or uid == current_user_id:
                    continue
                if uid not in self._users:
                    self._users[uid] = {
                        "username": user_data.get("username", ""),
                        "avatar_hash": user_data.get("avatar"),
                        "avatar_img": None,
                    }
                    self.plugin_base._thread_pool.submit(self._fetch_avatar, uid)
            # Fall through — also fetch guild info if not yet cached

        # ---------- guild icon lookup for the configured button ----------
        if channel_id != configured_channel:
            return
        if self._guild_channel_id == channel_id:
            return  # Already have (or are fetching) guild info for this channel
        guild_id = data.get("guild_id")
        if not guild_id:
            self._guild_id = None
            self._guild_channel_id = channel_id
            self._guild_icon_image = None
            self._guild_name = data.get("name", "")
            self._render_button()
            return
        self._guild_id = guild_id
        self._guild_channel_id = channel_id
        try:
            self.backend.get_guild(guild_id)
        except Exception as ex:
            log.error(f"Failed to request guild info: {ex}")

    def _on_get_guild(self, *args, **kwargs):
        data = args[1] if len(args) > 1 else None
        if not data or data.get("id") != self._guild_id:
            return
        self._guild_name = data.get("name", "")
        icon_url = data.get("icon_url")
        if icon_url:
            self.plugin_base._thread_pool.submit(self._fetch_guild_icon, icon_url)
        else:
            self._guild_icon_image = None
            self._render_button()

    def _fetch_guild_icon(self, icon_url: str):
        try:
            resp = requests.get(icon_url, timeout=10)
            resp.raise_for_status()
            self._guild_icon_image = Image.open(io.BytesIO(resp.content)).convert("RGBA")
        except Exception as ex:
            log.error(f"Failed to fetch guild icon: {ex}")
            self._guild_icon_image = None
        self._render_button()

    # ------------------------------------------------------------------
    # Avatar fetching
    # ------------------------------------------------------------------

    def _fetch_avatar(self, user_id: str):
        user = self._users.get(user_id)
        if not user:
            return
        avatar_hash = user.get("avatar_hash")
        if avatar_hash:
            url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png?size=64"
        else:
            # Default Discord avatar (based on discriminator bucket)
            url = "https://cdn.discordapp.com/embed/avatars/0.png"
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            user["avatar_img"] = Image.open(io.BytesIO(resp.content)).convert("RGBA")
        except Exception as ex:
            log.error(f"Failed to fetch avatar for {user_id}: {ex}")
        self._render_button()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def display_icon(self):
        """Override: keep our composed image if connected, else default."""
        in_channel = self._connected_channel_id == self._channel_row.get_value() and self._connected_channel_id is not None
        if in_channel:
            self._render_button()
        elif self._guild_icon_image is not None:
            self.set_media(image=self._guild_icon_image)
        else:
            super().display_icon()

    def _render_button(self):
        configured = self._channel_row.get_value()
        in_our_channel = (
            self._connected_channel_id is not None
            and self._connected_channel_id == configured
        )

        if in_our_channel:
            # Compose avatar grid
            avatars = [
                (u["avatar_img"], uid in self._speaking)
                for uid, u in list(self._users.items())
                if u.get("avatar_img") is not None
            ]
            if avatars:
                composed = _compose_avatars(avatars)
                self.set_media(image=composed)
            else:
                # In channel but avatars still loading – show active voice icon
                self.current_icon = self.get_icon(Icons.VOICE_CHANNEL_ACTIVE)
                super().display_icon()
            self.set_top_label("")
            self.set_center_label("")
            self.set_bottom_label("")
        elif self._guild_icon_image is not None:
            self.set_media(image=self._guild_icon_image)
            self.set_top_label("")
            self.set_center_label("")
            self.set_bottom_label("")
        else:
            # Show default voice icon + server name label at user-chosen position
            self.current_icon = self.get_icon(Icons.VOICE_CHANNEL_INACTIVE)
            super().display_icon()
            label_text = self._guild_name or ""
            position = self._label_position_row.get_value() or "bottom"
            for pos in ("top", "center", "bottom"):
                if pos == position and label_text:
                    self.set_label(
                        label_text,
                        position=pos,
                        font_size=8,
                        outline_width=2,
                        outline_color=[0, 0, 0, 255],
                    )
                else:
                    self.set_label("", position=pos)

    # ------------------------------------------------------------------
    # Config UI
    # ------------------------------------------------------------------

    def create_generative_ui(self):
        self._channel_row = EntryRow(
            action_core=self,
            var_name="change_voice_channel.text",
            default_value="",
            title="change-channel-voice",
            auto_add=False,
            complex_var_name=True,
            on_change=self._on_channel_id_changed,
        )
        self._label_position_row = ComboRow(
            action_core=self,
            var_name="change_voice_channel.label_position",
            default_value="bottom",
            items=["top", "center", "bottom"],
            title="Server name label position",
            auto_add=False,
            complex_var_name=True,
        )

    def _on_channel_id_changed(self, widget, new_value, old_value):
        """Invalidate guild cache and re-fetch when the channel ID is changed."""
        self._guild_channel_id = None
        self._guild_icon_image = None
        self._guild_name = None
        self._guild_id = None
        self._render_button()
        self._request_guild_info()

    def get_config_rows(self):
        return [self._channel_row._widget, self._label_position_row._widget]

    def create_event_assigners(self):
        self.event_manager.add_event_assigner(
            EventAssigner(
                id="change-channel",
                ui_label="change-channel",
                default_event=Input.Key.Events.DOWN,
                callback=self._on_change_channel,
            )
        )

    def _on_change_channel(self, _):
        if self._connected_channel_id is not None:
            try:
                self.backend.change_voice_channel(None)
            except Exception as ex:
                log.error(ex)
                self.show_error(3)
            return
        channel = self._channel_row.get_value()
        try:
            self.backend.change_voice_channel(channel)
        except Exception as ex:
            log.error(ex)
            self.show_error(3)

