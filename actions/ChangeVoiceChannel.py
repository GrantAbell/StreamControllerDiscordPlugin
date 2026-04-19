import io
from enum import StrEnum

from loguru import logger as log
from PIL import Image, ImageDraw, ImageFont

from .DiscordCore import DiscordCore
from .avatar_utils import (
    BUTTON_SIZE,
    make_placeholder_avatar,
    compose_overlapping_avatars,
)
from src.backend.PluginManager.EventAssigner import EventAssigner
from src.backend.PluginManager.InputBases import Input

from GtkHelper.GenerativeUI.EntryRow import EntryRow
from GtkHelper.GenerativeUI.ComboRow import ComboRow
from GtkHelper.GenerativeUI.SwitchRow import SwitchRow
from GtkHelper.GenerativeUI.SpinRow import SpinRow
from gi.repository import GLib  # type: ignore[attr-defined]

from ..discordrpc.commands import (
    VOICE_CHANNEL_SELECT,
    GET_CHANNEL,
    GET_GUILD,
    VOICE_STATE_CREATE,
    VOICE_STATE_DELETE,
    SPEAKING_START,
    SPEAKING_STOP,
)

# User-count badge colours / margin
_BADGE_BG = (32, 34, 37, 230)
_BADGE_FG = (255, 255, 255, 255)
_BADGE_MARGIN = 4

try:
    _badge_font = ImageFont.load_default(size=10)
except Exception:
    _badge_font = ImageFont.load_default()


def _draw_counter_badge(base: Image.Image, count: int, corner: str = "bottom-right") -> Image.Image:
    """Draw a user-count badge in the specified corner of *base*.

    *corner* is one of: "top-left", "top-right", "bottom-left", "bottom-right".
    """
    img = base.convert("RGBA").resize((BUTTON_SIZE, BUTTON_SIZE), Image.LANCZOS)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    text = str(count)
    bbox = draw.textbbox((0, 0), text, font=_badge_font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 5, 3
    bw = max(tw + pad_x * 2, th + pad_y * 2)
    bh = th + pad_y * 2
    right = corner.endswith("right")
    bottom = corner.startswith("bottom")
    if right:
        x2 = BUTTON_SIZE - _BADGE_MARGIN
        x1 = x2 - bw
    else:
        x1 = _BADGE_MARGIN
        x2 = x1 + bw
    if bottom:
        y2 = BUTTON_SIZE - _BADGE_MARGIN
        y1 = y2 - bh
    else:
        y1 = _BADGE_MARGIN
        y2 = y1 + bh
    draw.rounded_rectangle((x1, y1, x2, y2), radius=bh // 2, fill=_BADGE_BG)
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    draw.text((cx, cy), text, fill=_BADGE_FG, font=_badge_font, anchor="mm")
    img.alpha_composite(overlay)
    return img


class Icons(StrEnum):
    VOICE_CHANNEL_ACTIVE = "voice-active"
    VOICE_CHANNEL_INACTIVE = "voice-inactive"


def _compose_avatars(avatars: list[tuple[Image.Image, bool]]) -> Image.Image:
    """Compose avatar images in an overlapping stack, speaking user in front."""
    front = None
    extended = []
    for i, (img, speaking) in enumerate(avatars):
        if speaking:
            front = i
        extended.append((img, speaking, False))
    return compose_overlapping_avatars(extended, BUTTON_SIZE, front_index=front)


class ChangeVoiceChannel(DiscordCore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_configuration = True
        self.icon_keys = [Icons.VOICE_CHANNEL_ACTIVE, Icons.VOICE_CHANNEL_INACTIVE]
        self.current_icon = self.get_icon(Icons.VOICE_CHANNEL_INACTIVE)
        self.icon_name = Icons.VOICE_CHANNEL_INACTIVE

        # Guild info (for fallback display when not in channel)
        self._guild_id: str = None
        self._guild_name: str = None
        self._guild_icon_image: Image.Image = None
        self._guild_channel_id: str = None
        self._channel_name: str = None  # Current channel name for label display

        # Voice channel / avatar state
        self._connected_channel_id: str = None  # channel we're currently in
        self._watching_channel_id: str = None   # channel we're subscribed to (for voice states)
        self._users: dict = {}  # user_id → {username, avatar_hash, avatar_img}
        self._speaking: set = set()  # user_ids currently speaking
        self._fetching_avatars: set = set()  # user_ids with in-flight avatar fetches
        self._events_connected: bool = False
        self._requested_initial_voice_state: bool = False
        self._startup_sync_source_id: int | None = None
        self._startup_sync_attempts: int = 0
        self._last_guild_label_position: str = "none"
        self._last_channel_label_position: str = "none"
        self._label_cache: dict[str, tuple[str, int]] = {
            "guild": ("", 0),
            "channel": ("", 0),
        }

    def on_ready(self):
        super().on_ready()
        if not self._events_connected:
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
            self._events_connected = True

        # Start a short-lived retry loop so startup ordering (settings/backend/auth)
        # does not leave the action without initial guild/channel state.
        self._schedule_startup_sync()

    def _schedule_startup_sync(self):
        if self._startup_sync_source_id is not None:
            return
        self._startup_sync_attempts = 0
        self._startup_sync_source_id = GLib.timeout_add(250, self._run_startup_sync)

    def _run_startup_sync(self):
        self._startup_sync_attempts += 1

        configured_channel = self._channel_row.get_value()
        if configured_channel:
            self._start_watching_configured_channel()

            if self.backend and not self._requested_initial_voice_state:
                self.backend.request_current_voice_channel()
                self._requested_initial_voice_state = True

            self._render_button()
            self._startup_sync_source_id = None
            return False

        if self._startup_sync_attempts >= 40:
            self._startup_sync_source_id = None
            return False
        return True


    def _start_watching_configured_channel(self):
        """Subscribe to voice state events and fetch fresh data for the configured channel.
        Guild-info fetch and voice-state subscription are handled independently.
        """
        if not self.backend:
            return
        channel = self._channel_row.get_value()
        if not channel:
            return

        # --- Guild info (thumbnail / server name) ---
        # Always attempt this regardless of subscription state.
        if self._guild_channel_id != channel:
            try:
                self.backend.get_channel(channel)
            except Exception as ex:
                log.error(f"Failed to request channel info for guild lookup: {ex}")

        # --- Voice state subscription (live user count / avatars) ---
        if channel == self._watching_channel_id:
            return  # Already subscribed
        # Unsubscribe from previous channel
        if self._watching_channel_id:
            try:
                self.backend.unsubscribe_voice_states(self._watching_channel_id)
            except Exception as ex:
                log.error(f"Failed to unsubscribe from previous channel: {ex}")
        self._users.clear()
        self._speaking.clear()
        self._fetching_avatars.clear()
        try:
            subscribed = self.backend.subscribe_voice_states(channel)
            if subscribed:
                self._watching_channel_id = channel
                # Fetch initial user list now that subscription is active
                self.backend.get_channel(channel)
        except Exception as ex:
            log.error(f"Failed to subscribe to voice states: {ex}")

    def _on_voice_channel_select(self, *args, **kwargs):
        if not self.backend:
            self.show_error()
            return
        self.hide_error()
        # Retry watching the configured channel here — this is the first event
        # fired after Discord authenticates, so it covers the case where on_ready
        # was called before the backend was connected.
        self._start_watching_configured_channel()
        data = args[1] if len(args) > 1 else None
        new_channel = data.get("channel_id", None) if data else None
        configured = self._channel_row.get_value()

        # If we were in the configured channel and are now leaving it, remove self
        if self._connected_channel_id == configured and new_channel != configured:
            current_user_id = self.backend.current_user_id
            if current_user_id:
                self._users.pop(current_user_id, None)
                self._speaking.discard(current_user_id)
            try:
                self.backend.unsubscribe_speaking(configured)
            except Exception as ex:
                log.error(f"Failed to unsubscribe speaking: {ex}")
            # Discord silently drops voice-state subscriptions when the local user
            # leaves the channel.  Clear _watching_channel_id so the call to
            # _start_watching_configured_channel below forces a fresh re-subscribe.
            self._watching_channel_id = None

        self._connected_channel_id = new_channel

        if new_channel == configured and new_channel is not None:
            # Joined our configured channel — subscribe to speaking and re-sync user list
            # (voice states already subscribed via _start_watching_configured_channel)
            try:
                self.backend.subscribe_speaking(new_channel)
                self.backend.get_channel(new_channel)
            except Exception as ex:
                log.error(f"Failed to subscribe after joining channel: {ex}")
                self._render_button()
        else:
            # Re-subscribe to voice states for the configured channel (Discord dropped
            # the subscription when we left).  This also fetches a fresh GET_CHANNEL.
            self._start_watching_configured_channel()
            self._render_button()  # Immediate render while waiting for GET_CHANNEL reply

    # Voice state events (join/leave) — used only as refresh triggers
    # Discord's VOICE_STATE_CREATE/DELETE data contains no channel_id, so
    # we cannot determine which channel the event belongs to directly.
    # Instead we use the event as a signal to re-fetch GET_CHANNEL for the
    # channel THIS button is watching.  _on_get_channel then reconciles the
    # user list from the authoritative voice_states array.

    def _on_voice_state_create(self, *args, **kwargs):
        if not self._watching_channel_id:
            return
        try:
            self.backend.get_channel(self._watching_channel_id)
        except Exception as ex:
            log.error(f"Failed to refresh channel on voice state create: {ex}")

    def _on_voice_state_delete(self, *args, **kwargs):
        if not self._watching_channel_id:
            return
        try:
            self.backend.get_channel(self._watching_channel_id)
        except Exception as ex:
            log.error(f"Failed to refresh channel on voice state delete: {ex}")

    # Speaking events
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

    # Channel / guild info
    def _on_get_channel(self, *args, **kwargs):
        data = args[1] if len(args) > 1 else None
        if not data:
            return
        channel_id = data.get("id")
        configured_channel = self._channel_row.get_value()
        if channel_id != configured_channel:
            return

        connected = self._connected_channel_id == configured_channel
        current_user_id = self.backend.current_user_id

        # Reconcile user list against the authoritative voice_states snapshot.
        # Self is excluded only in observer mode (not in the channel);
        # controls avatar *display* only and is handled in _render_button.
        new_user_ids = set()
        for vs in data.get("voice_states", []):
            user_data = vs.get("user", {})
            uid = user_data.get("id")
            if not uid:
                continue
            if uid == current_user_id and not connected:
                continue
            new_user_ids.add(uid)
            if uid not in self._users:
                self._users[uid] = {
                    "username": user_data.get("username", ""),
                    "avatar_hash": user_data.get("avatar"),
                    "avatar_img": None,
                }
            if connected:
                self._submit_avatar_fetch(uid)
        # Remove users who left
        for uid in list(self._users):
            if uid not in new_user_ids:
                self._users.pop(uid)
                self._speaking.discard(uid)

        # Store channel name for label display
        self._channel_name = data.get("name", "")

        # Guild info lookup (only if not yet cached for this channel)
        if self._guild_channel_id != channel_id:
            guild_id = data.get("guild_id")
            if not guild_id:
                self._guild_id = None
                self._guild_channel_id = channel_id
                self._guild_icon_image = None
                self._guild_name = data.get("name", "")
            else:
                self._guild_id = guild_id
                self._guild_channel_id = channel_id
                try:
                    self.backend.get_guild(guild_id)
                except Exception as ex:
                    log.error(f"Failed to request guild info: {ex}")

        self._render_button()

    def _on_get_guild(self, *args, **kwargs):
        data = args[1] if len(args) > 1 else None
        if not data or data.get("id") != self._guild_id:
            return
        self._guild_name = data.get("name", "")
        icon_url = data.get("icon_url")
        if icon_url:
            try:
                self.plugin_base._thread_pool.submit(self._fetch_guild_icon, icon_url)
            except Exception as ex:
                log.error(f"Failed to submit guild icon fetch task: {ex}")
                self._fetch_guild_icon(icon_url)
        else:
            self._guild_icon_image = None
            self._render_button()

    def _fetch_guild_icon(self, icon_url: str):
        image_bytes = None
        try:
            image_bytes = self.backend.fetch_guild_icon(icon_url)
        except Exception as ex:
            log.error(f"Failed to fetch guild icon: {ex}")
        if image_bytes:
            try:
                self._guild_icon_image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
            except Exception as ex:
                log.error(f"Failed to decode guild icon: {ex}")
                self._guild_icon_image = None
        else:
            self._guild_icon_image = None
        self._render_button()

    # Avatar fetching
    def _submit_avatar_fetch(self, user_id: str):
        """Submit an avatar fetch task if not already cached or in-progress."""
        user = self._users.get(user_id)
        if not user or user.get("avatar_img") is not None:
            return
        if not user.get("avatar_hash"):  # No real avatar; placeholder renders immediately
            return
        if user_id in self._fetching_avatars:
            return
        self._fetching_avatars.add(user_id)
        self.plugin_base._thread_pool.submit(self._fetch_avatar, user_id)

    def _fetch_avatar(self, user_id: str):
        user = self._users.get(user_id)
        if not user:
            self._fetching_avatars.discard(user_id)
            return
        avatar_hash = user.get("avatar_hash")
        if not avatar_hash:
            self._fetching_avatars.discard(user_id)
            return
        try:
            image_bytes = self.backend.fetch_avatar(user_id, avatar_hash)
            if image_bytes:
                user["avatar_img"] = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        except Exception as ex:
            log.error(f"Failed to fetch avatar for {user_id}: {ex}")
        self._fetching_avatars.discard(user_id)
        self._render_button()

    # Rendering
    def display_icon(self):
        self._render_button()

    def _render_button(self):
        # Ensure configured-channel metadata and subscriptions are loaded even if
        # initial auth/state events were missed during startup ordering.
        self._start_watching_configured_channel()

        configured = self._channel_row.get_value()
        connected = (
            self._connected_channel_id is not None
            and self._connected_channel_id == configured
        )

        # Build label texts based on settings
        guild_position = self._label_position_row.get_value() or "bottom"
        channel_position = self._channel_name_label_position_row.get_value() or "none"
        guild_text = self._guild_name or ""
        channel_text = self._channel_name or ""

        if connected:
            # Trigger fetches for any users who joined before avatars were loaded
            for uid in list(self._users):
                self._submit_avatar_fetch(uid)
            show_self = self._show_self_row.get_value()
            current_user_id = self.backend.current_user_id if self.backend else None
            avatars = []
            for uid, u in list(self._users.items()):
                if not show_self and uid == current_user_id:
                    continue
                avatar_img = u.get("avatar_img")
                if avatar_img is None:
                    avatar_img = make_placeholder_avatar(u.get("username", "?"), uid, BUTTON_SIZE)
                avatars.append((avatar_img, uid in self._speaking))
            if avatars:
                self.set_media(image=_compose_avatars(avatars))
            elif self._users:
                # Users present but no visible avatars (e.g. show_self=False and alone,
                # or avatars still loading) — show count badge so channel feels occupied.
                count = len(self._users)
                corner = self._badge_corner_row.get_value() or "bottom-right"
                if self._guild_icon_image is not None:
                    self.set_media(image=_draw_counter_badge(self._guild_icon_image, count, corner))
                else:
                    self.current_icon = self.get_icon(Icons.VOICE_CHANNEL_ACTIVE)
                    icon_asset = self.current_icon
                    _, base = icon_asset.get_values() if icon_asset else (None, None)
                    if base is not None:
                        self.set_media(image=_draw_counter_badge(base, count, corner))
                    else:
                        super().display_icon()
            else:
                self.current_icon = self.get_icon(Icons.VOICE_CHANNEL_ACTIVE)
                super().display_icon()
        else:
            # Observer mode: guild/voice icon with a user-count badge when occupied
            count = len(self._users)
            if self._guild_icon_image is not None:
                base = self._guild_icon_image
            else:
                self.current_icon = self.get_icon(Icons.VOICE_CHANNEL_INACTIVE)
                icon_asset = self.current_icon
                _, base = icon_asset.get_values() if icon_asset else (None, None)

            if base is not None and count > 0:
                corner = self._badge_corner_row.get_value() or "bottom-right"
                self.set_media(image=_draw_counter_badge(base, count, corner))
            elif self._guild_icon_image is not None:
                self.set_media(image=self._guild_icon_image)
            else:
                # Empty channel, no guild icon — show inactive voice icon.
                super().display_icon()

        self._render_labels(guild_text, guild_position, channel_text, channel_position)

    def _render_labels(
        self,
        guild_text: str,
        guild_position: str,
        channel_text: str,
        channel_position: str,
    ):
        guild_font_size = int(self._guild_label_font_size_row.get_value())
        channel_font_size = int(self._channel_label_font_size_row.get_value())

        # Clear stale label positions only when the target position changes.
        if self._last_guild_label_position != "none" and self._last_guild_label_position != guild_position:
            self.set_label("", position=self._last_guild_label_position)
        if self._last_channel_label_position != "none" and self._last_channel_label_position != channel_position:
            self.set_label("", position=self._last_channel_label_position)

        # Update guild label only when text/font/position actually changed.
        previous_guild_text, previous_guild_font = self._label_cache["guild"]
        guild_needs_update = (
            guild_position != "none"
            and bool(guild_text)
            and (
                previous_guild_text != guild_text
                or previous_guild_font != guild_font_size
                or self._last_guild_label_position != guild_position
            )
        )
        if guild_needs_update:
            self.set_label(
                guild_text,
                position=guild_position,
                font_size=guild_font_size,
                outline_width=2,
                outline_color=[0, 0, 0, 255],
            )
            self._label_cache["guild"] = (guild_text, guild_font_size)
        elif guild_position == "none" or not guild_text:
            if self._last_guild_label_position != "none":
                self.set_label("", position=self._last_guild_label_position)
            self._label_cache["guild"] = ("", guild_font_size)

        # Update channel label only when text/font/position actually changed.
        previous_channel_text, previous_channel_font = self._label_cache["channel"]
        channel_needs_update = (
            channel_position != "none"
            and bool(channel_text)
            and (
                previous_channel_text != channel_text
                or previous_channel_font != channel_font_size
                or self._last_channel_label_position != channel_position
            )
        )
        if channel_needs_update:
            self.set_label(
                channel_text,
                position=channel_position,
                font_size=channel_font_size,
                outline_width=2,
                outline_color=[0, 0, 0, 255],
            )
            self._label_cache["channel"] = (channel_text, channel_font_size)
        elif channel_position == "none" or not channel_text:
            if self._last_channel_label_position != "none":
                self.set_label("", position=self._last_channel_label_position)
            self._label_cache["channel"] = ("", channel_font_size)

        self._last_guild_label_position = guild_position
        self._last_channel_label_position = channel_position


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
            items=["top", "center", "bottom", "none"],
            title="Server name label position",
            auto_add=False,
            complex_var_name=True,
            on_change=lambda *_: self._render_button(),
        )
        self._guild_label_font_size_row = SpinRow(
            action_core=self,
            var_name="change_voice_channel.guild_label_font_size",
            default_value=8,
            min=6,
            max=32,
            step=1,
            digits=0,
            title="Server name font size",
            auto_add=False,
            complex_var_name=True,
            on_change=lambda *_: self._render_button(),
        )
        self._channel_name_label_position_row = ComboRow(
            action_core=self,
            var_name="change_voice_channel.channel_label_position",
            default_value="none",
            items=["none", "top", "center", "bottom"],
            title="Channel name label position",
            auto_add=False,
            complex_var_name=True,
            on_change=lambda *_: self._render_button(),
        )
        self._channel_label_font_size_row = SpinRow(
            action_core=self,
            var_name="change_voice_channel.channel_label_font_size",
            default_value=8,
            min=6,
            max=32,
            step=1,
            digits=0,
            title="Channel name font size",
            auto_add=False,
            complex_var_name=True,
            on_change=lambda *_: self._render_button(),
        )
        self._show_self_row = SwitchRow(
            action_core=self,
            var_name="change_voice_channel.show_self",
            default_value=True,
            title="Show my own avatar",
            subtitle="Include yourself in the user grid when connected",
            auto_add=False,
            complex_var_name=True,
            on_change=lambda *_: self._render_button(),
        )
        self._badge_corner_row = ComboRow(
            action_core=self,
            var_name="change_voice_channel.badge_corner",
            default_value="bottom-right",
            items=["top-left", "top-right", "bottom-left", "bottom-right"],
            title="User count badge corner",
            auto_add=False,
            complex_var_name=True,
            on_change=lambda *_: self._render_button(),
        )

    def _on_channel_id_changed(self, widget, new_value, old_value):
        """Invalidate all cached state and re-subscribe when the channel ID is changed."""
        if self._watching_channel_id:
            try:
                self.backend.unsubscribe_voice_states(self._watching_channel_id)
                self.backend.unsubscribe_speaking(self._watching_channel_id)
            except Exception:
                pass
        self._guild_channel_id = None
        self._guild_icon_image = None
        self._guild_name = None
        self._guild_id = None
        self._channel_name = None
        self._watching_channel_id = None
        self._last_guild_label_position = "none"
        self._last_channel_label_position = "none"
        self._label_cache["guild"] = ("", 0)
        self._label_cache["channel"] = ("", 0)
        self._users.clear()
        self._speaking.clear()
        self._fetching_avatars.clear()
        self._render_button()
        self._start_watching_configured_channel()

    def get_config_rows(self):
        return [
            self._channel_row._widget,
            self._label_position_row._widget,
            self._guild_label_font_size_row._widget,
            self._channel_name_label_position_row._widget,
            self._channel_label_font_size_row._widget,
            self._show_self_row._widget,
            self._badge_corner_row._widget,
        ]

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
        configured = self._channel_row.get_value()

        # Toggle behavior only when already connected to this configured channel.
        # If connected to another channel, switch to the configured one instead.
        if self._connected_channel_id is not None and self._connected_channel_id == configured:
            try:
                self.backend.change_voice_channel(None)
            except Exception as ex:
                log.error(ex)
                self.show_error(3)
            return

        channel = configured
        try:
            self.backend.change_voice_channel(channel)
        except Exception as ex:
            log.error(ex)
            self.show_error(3)

