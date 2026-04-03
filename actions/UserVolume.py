import io

import requests
from loguru import logger as log
from PIL import Image

from .DiscordCore import DiscordCore
from .avatar_utils import (
    BUTTON_SIZE,
    make_circle_avatar,
    draw_speaking_ring,
    make_placeholder_avatar,
)
from src.backend.PluginManager.EventAssigner import EventAssigner
from src.backend.PluginManager.InputBases import Input

from GtkHelper.GenerativeUI.SwitchRow import SwitchRow

from ..discordrpc.commands import (
    VOICE_STATE_CREATE,
    VOICE_STATE_DELETE,
    VOICE_STATE_UPDATE,
    VOICE_CHANNEL_SELECT,
    GET_CHANNEL,
    SPEAKING_START,
    SPEAKING_STOP,
)


class UserVolume(DiscordCore):
    """Action for controlling per-user volume via dial.

    Dial behavior:
    - Rotate: Adjust volume of selected user (+/- 5% per tick)
    - Press: Cycle to next user in voice channel

    Display:
    - Top label: Current voice channel name (or "Not in voice")
    - Center label: Username/nick
    - Bottom label: Volume percentage
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_configuration = True

        # Current state
        self._users: list = []  # List of user dicts [{id, username, nick, volume, muted, avatar_hash, avatar_img}, ...]
        self._current_user_index: int = 0
        self._current_channel_id: str = None
        self._current_channel_name: str = ""
        self._in_voice_channel: bool = False
        self._speaking: set = set()          # user_ids currently speaking
        self._fetching_avatars: set = set()  # user_ids with in-flight avatar fetches
        self._self_input_volume: int = 100   # Tracked locally; mic input volume is write-only via RPC

        # Volume adjustment step (percentage points per dial tick)
        self.VOLUME_STEP = 5

    def create_generative_ui(self):
        self._control_self_row = SwitchRow(
            action_core=self,
            var_name="user_volume.control_self",
            default_value=False,
            title="Control my mic volume",
            subtitle="Include yourself so the dial adjusts your microphone input volume",
            auto_add=False,
            complex_var_name=True,
        )

    def get_config_rows(self):
        return [self._control_self_row._widget]

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
                event_id=f"{self.plugin_base.get_plugin_id()}::{SPEAKING_START}",
                callback=self._on_speaking_start,
                )
        self.plugin_base.connect_to_event(
                event_id=f"{self.plugin_base.get_plugin_id()}::{SPEAKING_STOP}",
                callback=self._on_speaking_stop,
                )

        # Initialize display
        self._update_display()

        # Request current voice channel state (in case we're already in a channel)
        self.backend.request_current_voice_channel()

    def create_event_assigners(self):
        # Dial rotation: adjust volume
        self.event_manager.add_event_assigner(
            EventAssigner(
                id="volume-up",
                ui_label="volume-up",
                default_event=Input.Dial.Events.TURN_CW,
                callback=self._on_volume_up,
            )
        )
        self.event_manager.add_event_assigner(
            EventAssigner(
                id="volume-down",
                ui_label="volume-down",
                default_event=Input.Dial.Events.TURN_CCW,
                callback=self._on_volume_down,
            )
        )

        # Dial press: cycle user
        self.event_manager.add_event_assigner(
            EventAssigner(
                id="cycle-user",
                ui_label="cycle-user",
                default_event=Input.Dial.Events.DOWN,
                callback=self._on_cycle_user,
            )
        )

        # Also support key press for cycling (for key-based assignment)
        self.event_manager.add_event_assigner(
            EventAssigner(
                id="cycle-user-key",
                ui_label="cycle-user-key",
                default_event=Input.Key.Events.DOWN,
                callback=self._on_cycle_user,
            )
        )

    # === Event Handlers ===

    def _on_volume_up(self, _):
        """Increase current user's volume."""
        self._adjust_volume(self.VOLUME_STEP)

    def _on_volume_down(self, _):
        """Decrease current user's volume."""
        self._adjust_volume(-self.VOLUME_STEP)

    def _on_cycle_user(self, _):
        """Cycle to next user in voice channel."""
        if not self._users:
            return
        self._current_user_index = (self._current_user_index + 1) % len(self._users)
        self._update_display()

    def _adjust_volume(self, delta: int):
        """Adjust current user's volume by delta."""
        if not self._users or self._current_user_index >= len(self._users):
            return

        user = self._users[self._current_user_index]
        current_volume = user.get("volume", 100)

        if user.get("is_self"):
            new_volume = max(0, min(100, current_volume + delta))
            try:
                self.backend.set_input_volume(new_volume)
                user["volume"] = new_volume
                self._self_input_volume = new_volume
                self._update_display()
            except Exception as ex:
                log.error(f"Failed to set input volume: {ex}")
                self.show_error(3)
            return

        new_volume = max(0, min(200, current_volume + delta))

        try:
            if self.backend.set_user_volume(user["id"], new_volume):
                user["volume"] = new_volume
                self._update_display()
        except Exception as ex:
            log.error(f"Failed to set user volume: {ex}")
            self.show_error(3)

    # === Discord Event Callbacks ===

    def _on_voice_channel_select(self, *args, **kwargs):
        """Handle user joining/leaving voice channel."""
        data = args[1]
        try:
            if data is None or data.get("channel_id") is None:
                # Left voice channel - unsubscribe from previous channel
                if self._current_channel_id:
                    self.backend.unsubscribe_voice_states(self._current_channel_id)
                    self.backend.unsubscribe_speaking(self._current_channel_id)
                self._in_voice_channel = False
                self._current_channel_id = None
                self._current_channel_name = ""
                self._users.clear()
                self._current_user_index = 0
                self._speaking.clear()
                self._fetching_avatars.clear()
                self._self_input_volume = 100
                self.backend.clear_voice_channel_users()
            else:
                # Joined voice channel
                new_channel_id = data.get("channel_id")

                # If switching channels, unsubscribe from old channel first
                if self._current_channel_id and self._current_channel_id != new_channel_id:
                    self.backend.unsubscribe_voice_states(self._current_channel_id)
                    self.backend.unsubscribe_speaking(self._current_channel_id)
                    self._users.clear()
                    self._current_user_index = 0
                    self._speaking.clear()
                    self._fetching_avatars.clear()
                    self._self_input_volume = 100

                self._in_voice_channel = True
                self._current_channel_id = new_channel_id
                self._current_channel_name = data.get("name", "Voice")

                # Register frontend callbacks for voice state events
                self.plugin_base.add_callback(VOICE_STATE_CREATE, self._on_voice_state_create)
                self.plugin_base.add_callback(VOICE_STATE_DELETE, self._on_voice_state_delete)
                self.plugin_base.add_callback(VOICE_STATE_UPDATE, self._on_voice_state_update)

                # Subscribe to voice state and speaking events via backend (with channel_id)
                self.backend.subscribe_voice_states(self._current_channel_id)
                self.backend.subscribe_speaking(self._current_channel_id)

                # Fetch initial user list
                self.backend.get_channel(self._current_channel_id)

            self._update_display()
        except Exception as ex:
            log.error(f"UserVolume[{id(self)}]: Error in _on_voice_channel_select: {ex}")

    def _on_get_channel(self, *args, **kwargs):
        """Handle GET_CHANNEL response with initial user list."""
        data = args[1]
        if not data:
            return

        # Check if this is for our current channel
        channel_id = data.get("id")
        if channel_id != self._current_channel_id:
            return

        # Update channel name if available
        if data.get("name"):
            self._current_channel_name = data.get("name")

        # Process voice_states array
        voice_states = data.get("voice_states", [])
        current_user_id = self.backend.current_user_id

        for vs in voice_states:
            user_data = vs.get("user", {})
            user_id = user_data.get("id")

            if not user_id:
                continue

            # Self: inject as first entry when the toggle is enabled
            if user_id == current_user_id:
                if self._control_self_row.get_value() and not any(u.get("is_self") for u in self._users):
                    self_info = {
                        "id": user_id,
                        "username": user_data.get("username", "Me"),
                        "nick": vs.get("nick"),
                        "volume": self._self_input_volume,
                        "muted": False,
                        "avatar_hash": user_data.get("avatar"),
                        "avatar_img": None,
                        "is_self": True,
                    }
                    self._users.insert(0, self_info)
                    self._submit_avatar_fetch(user_id)
                continue

            user_info = {
                "id": user_id,
                "username": user_data.get("username", "Unknown"),
                "nick": vs.get("nick"),
                "volume": vs.get("volume", 100),
                "muted": vs.get("mute", False),
                "avatar_hash": user_data.get("avatar"),
                "avatar_img": None,
            }

            # Add if not already present (idempotent)
            if not any(u["id"] == user_id for u in self._users):
                self._users.append(user_info)
                self._submit_avatar_fetch(user_id)

            # Update backend cache
            self.backend.update_voice_channel_user(
                user_id,
                user_info["username"],
                user_info["nick"],
                user_info["volume"],
                user_info["muted"]
            )

        self._update_display()

    def _on_voice_state_create(self, data: dict):
        """Handle user joining voice channel."""
        if not data:
            return

        user_data = data.get("user", {})
        user_id = user_data.get("id")
        if not user_id:
            return

        # Filter out self
        if user_id == self.backend.current_user_id:
            return

        user_info = {
            "id": user_id,
            "username": user_data.get("username", "Unknown"),
            "nick": data.get("nick"),
            "volume": data.get("volume", 100),
            "muted": data.get("mute", False),
            "avatar_hash": user_data.get("avatar"),
            "avatar_img": None,
        }

        # Add to local list (avoid duplicates)
        if not any(u["id"] == user_id for u in self._users):
            self._users.append(user_info)
            self._submit_avatar_fetch(user_id)

        # Update backend cache
        self.backend.update_voice_channel_user(
            user_id,
            user_info["username"],
            user_info["nick"],
            user_info["volume"],
            user_info["muted"]
        )

        self._update_display()

    def _on_voice_state_delete(self, data: dict):
        """Handle user leaving voice channel."""
        if not data:
            return

        user_data = data.get("user", {})
        user_id = user_data.get("id")
        if not user_id:
            return

        # Remove from local list
        self._users = [u for u in self._users if u["id"] != user_id]
        self._speaking.discard(user_id)
        self._fetching_avatars.discard(user_id)

        # Adjust current index if needed
        if self._current_user_index >= len(self._users):
            self._current_user_index = max(0, len(self._users) - 1)

        # Update backend cache
        self.backend.remove_voice_channel_user(user_id)

        self._update_display()

    def _on_voice_state_update(self, data: dict):
        """Handle user voice state change (volume, mute, etc)."""
        if not data:
            return

        user_data = data.get("user", {})
        user_id = user_data.get("id")
        if not user_id:
            return

        # Find and update user
        for user in self._users:
            if user["id"] == user_id:
                if "volume" in data:
                    user["volume"] = data.get("volume")
                if "mute" in data:
                    user["muted"] = data.get("mute")
                if "nick" in data:
                    user["nick"] = data.get("nick")
                break

        self._update_display()

    # === Speaking ===

    def _on_speaking_start(self, *args, **kwargs):
        """Handle user starting to speak."""
        data = args[1] if len(args) > 1 else None
        if not data:
            return
        user_id = str(data.get("user_id", ""))
        if not user_id:
            return
        self._speaking.add(user_id)
        self._update_display()

    def _on_speaking_stop(self, *args, **kwargs):
        """Handle user stopping speaking."""
        data = args[1] if len(args) > 1 else None
        if not data:
            return
        user_id = str(data.get("user_id", ""))
        if not user_id:
            return
        self._speaking.discard(user_id)
        self._update_display()

    # === Avatar fetching ===

    def _submit_avatar_fetch(self, user_id: str):
        """Submit an avatar fetch task if not already cached or in-progress."""
        user = next((u for u in self._users if u["id"] == user_id), None)
        if not user or user.get("avatar_img") is not None:
            return
        if not user.get("avatar_hash"):  # No real avatar; placeholder renders immediately
            return
        if user_id in self._fetching_avatars:
            return
        self._fetching_avatars.add(user_id)
        self.plugin_base._thread_pool.submit(self._fetch_avatar, user_id)

    def _fetch_avatar(self, user_id: str):
        user = next((u for u in self._users if u["id"] == user_id), None)
        if not user:
            self._fetching_avatars.discard(user_id)
            return
        avatar_hash = user.get("avatar_hash")
        if not avatar_hash:
            self._fetching_avatars.discard(user_id)
            return
        url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png?size=64"
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            user["avatar_img"] = Image.open(io.BytesIO(resp.content)).convert("RGBA")
        except Exception as ex:
            log.error(f"Failed to fetch avatar for {user_id}: {ex}")
        self._fetching_avatars.discard(user_id)
        self._update_display()

    # === Display ===

    def _update_display(self):
        """Update the dial display with current user info."""
        if not self._in_voice_channel or not self._users:
            self.set_top_label("Not in voice" if not self._in_voice_channel else self._current_channel_name[:12])
            self.set_center_label("")
            self.set_bottom_label("No users" if self._in_voice_channel else "")
            # Clear any lingering avatar image so the display resets cleanly
            self.set_media(image=Image.new("RGBA", (BUTTON_SIZE, BUTTON_SIZE), (0, 0, 0, 255)))
            return

        # Truncate channel name for space
        channel_display = self._current_channel_name[:12] if len(self._current_channel_name) > 12 else self._current_channel_name
        self.set_top_label(channel_display)

        if self._current_user_index < len(self._users):
            user = self._users[self._current_user_index]
            display_name = user.get("nick") or user.get("username", "Unknown")
            volume = user.get("volume", 100)
            is_speaking = user["id"] in self._speaking

            # Build avatar image
            avatar_src = user.get("avatar_img")
            if avatar_src is not None:
                avatar = make_circle_avatar(avatar_src, BUTTON_SIZE)
            else:
                avatar = make_placeholder_avatar(display_name, user["id"], BUTTON_SIZE)
                # Kick off a fetch if not already in-flight
                self._submit_avatar_fetch(user["id"])
            if is_speaking:
                avatar = draw_speaking_ring(avatar, BUTTON_SIZE)
            self.set_media(image=avatar)

            # Truncate name for label overlay
            display_name = display_name[:10] if len(display_name) > 10 else display_name
            self.set_center_label(display_name)
            self.set_bottom_label(f"{volume}%")
        else:
            self.set_center_label("")
            self.set_bottom_label("No selection")
