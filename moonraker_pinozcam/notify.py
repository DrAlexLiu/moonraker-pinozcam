"""Alerts and remote control over Telegram and Discord.

⚠️ SIX METHODS IN THIS FILE ARE COPIED VERBATIM from the OctoPrint build's
`notify.py` and must stay that way: `handle_telegram_command`,
`handle_discord_command`, `_action_state_problem`, `check_reply`,
`telegram_send_with_reply` and `get_printer_status`. They are checked by
`tools/check_upstream_sync.py`.

The reason is not tidiness. `telegram_bot.py` and `discord_bot.py` are
themselves byte-identical copies, and a handler is the other half of their
protocol -- the custom_id prefixes, which branch answers the user, which
branch stays silent. A re-implementation written from the same description
looked right and was wrong in three separate ways at once: it matched
Telegram's `yes:`/`no:` prefixes against Discord's `confirm:`/`cancel:`
buttons, it looked confirmation nonces up under the wrong channel, and it
returned its replies as strings that both transports discard. Every one of
those is invisible until someone presses a real button.

What is NOT copied is the printer underneath. `_MoonrakerPrinter` presents
the five OctoPrint methods the copied code calls; everything else in this
file is the Klipper-specific half.
"""

import io
import json
import os
import re
import threading
import time
from io import BytesIO

from . import credentials
from .confirm import ConfirmMixin
from .discord_bot import (DiscordBot, buttons as discord_buttons,
                          confirm_buttons as discord_confirm_buttons)
from .telegram_bot import (TelegramBot, buttons as telegram_buttons,
                           confirm_buttons as telegram_confirm_buttons)

MUTED_LINE = ("All alerts are muted for this print -- on Telegram AND "
              "Discord. They come back on automatically when the next print "
              "starts.")


class _MoonrakerPrinter(object):
    """The OctoPrint printer interface, backed by Moonraker.

    Exists so the copied handlers can stay byte-identical: between them they
    call exactly five methods, and this maps those onto MoonrakerClient. The
    state ids are OctoPrint's, because the copied code compares against them
    by name.
    """

    # Klipper's print_stats.state -> OctoPrint's get_state_id().
    # "cancelled" and "complete" both mean the printer is idle and ready,
    # which is what OPERATIONAL means; "standby" is the same before any job.
    _STATES = {
        "printing": "PRINTING",
        "paused": "PAUSED",
        "standby": "OPERATIONAL",
        "complete": "OPERATIONAL",
        "cancelled": "OPERATIONAL",
        "error": "ERROR",
    }

    def __init__(self, client):
        self._client = client

    def get_state_id(self):
        state = self._client.state
        # pause_resume is authoritative: Klipper reports print_stats.state
        # "paused" and the pause_resume object separately, and a macro can
        # set one before the other.
        if state.is_paused:
            return "PAUSED"
        if self._client.state.klippy_state not in ("ready", "unknown"):
            return "CLOSED_WITH_ERROR"
        return self._STATES.get(state.state, state.state.upper())

    def get_current_data(self):
        state = self._client.state
        return {
            "progress": {"completion": state.progress * 100.0},
            "job": {"file": {"name": state.filename} if state.filename
                    else {}},
        }

    def get_current_temperatures(self):
        state = self._client.state
        out = {}
        if state.nozzle_temp is not None:
            out["tool0"] = {"actual": state.nozzle_temp}
        if state.bed_temp is not None:
            out["bed"] = {"actual": state.bed_temp}
        return out

    def pause_print(self):
        self._client.pause_print()

    def resume_print(self):
        self._client.resume_print()

    def cancel_print(self):
        self._client.cancel_print()


class Notifier(ConfirmMixin):
    """Owns the bots and translates their commands into printer actions."""

    MUTED_TEXT = ("🔇 " + MUTED_LINE + " Send /hi (Telegram) or press Check "
                  "to see the camera meanwhile.")
    UNMUTED_TEXT = "🔊 Alerts are back on."

    # ⚠️ Referenced by the copied handle_discord_command but never defined
    # in the OctoPrint build -- `!help` raises AttributeError there. Defined
    # here so the copied branch works; do not "sync" this one away.
    DISCORD_HELP = (
        "PiNozCam commands: `!check` current view and status, `!status` "
        "text only, `!pause` / `!resume` / `!stop` the print (each asks for "
        "confirmation), `!mute` / `!unmute` alerts."
    )

    def __init__(self, config, client, logger, snapshot=None, status=None):
        self._cfg = config
        self._client = client
        # `_logger` is the name the copied methods use.
        self._logger = self._log = logger
        self._printer = _MoonrakerPrinter(client)
        self._snapshot = snapshot      # () -> jpeg bytes (never None)
        self._status = status          # () -> str

        self.telegram_bot = None
        self.discord_bot = None
        self.alerts_muted = False

        # Read by the copied telegram_send_with_reply. This build has no
        # per-message bookkeeping, so the set is written and never read;
        # keeping it means that method needs no edit.
        self.ai_running = False
        self.current_telegram_message_set = set()
        self.current_telegram_message_paused = False

        # ConfirmMixin's state. It already handles what this file would
        # otherwise get wrong: per-channel isolation, a scope bumped at
        # every print so an in-flight reply from a finished job is refused,
        # and check-and-remove as one critical section so two taps arriving
        # together cannot both cancel.
        self.confirm_lock = threading.Lock()
        self.confirm_tokens = {}
        self.confirm_scope = 0

        # The alert budget. Both settings existed and neither did anything.
        self._alerts_sent = 0
        self._last_alert_at = 0.0

    # ---- what the copied code calls into -------------------------------

    @staticmethod
    def redact(text):
        return credentials.redact(text)

    def printer_label(self):
        """A stable label, the way the OctoPrint build derives one.

        The config file wins when set; otherwise the client walks the same
        ladder -- frontend name, hostname, then Moonraker's instance_id.
        """
        return (self._cfg.get("printer", "name", "")
                or self._client.printer_name())

    # The copied code reads this as an attribute in one place and calls
    # printer_label() in another, exactly as upstream does.
    @property
    def printer_id(self):
        """A short, STABLE, colon-free tag for Discord's custom_id.

        ⚠️ Not the display name. Discord packs this into a custom_id with
        colons as separators, so a printer called "Bench: left" would break
        parsing outright, and two printers sharing a name would answer each
        other's buttons. Moonraker mints an instance_id once and keeps it;
        the first eight hex digits are what identify us here, with the
        display name reserved for what humans read.
        """
        configured = (self._cfg.get("printer", "name", "") or "").strip()
        if configured:
            # A user-set id is honoured, minus anything that would break
            # the custom_id encoding.
            return configured.replace(":", "-")[:24]
        return self._client.instance_tag()

    def button_id_settled(self):
        """Whether printer_id is final, so a change in it means a rename.

        A configured name is final the moment it is read; the instance tag
        is not, until Moonraker's database answers.
        """
        if (self._cfg.get("printer", "name", "") or "").strip():
            return True
        self.printer_id                      # resolve before asking
        return self._client.instance_tag_settled()

    def announce_button_id_change(self):
        """Re-issue Discord buttons when the id they carry has changed.

        ⚠️ Renaming a printer silently disables its Discord remote control.
        Every button carries printer_id in its custom_id and DiscordBot
        drops any interaction naming a different printer WITHOUT an ACK, on
        purpose, so the owning instance can answer inside the three-second
        deadline. There is no error, no reply and no INFO log -- the Stop
        button in the channel simply stops working, which is the worst
        moment to discover a rename. So say it, with buttons that work.

        Telegram needs none of this: its buttons carry no printer id.

        Not routed through alert(): this is an operational notice, so it is
        exempt from the alert budget and from mute, exactly as a camera
        going blind is.
        """
        if not self.button_id_settled():
            self._logger.debug("printer id not settled yet; not announcing")
            return
        path = self._cfg.state_path()
        if path is None:
            return
        current = self.printer_id
        previous = self._read_state().get("discord_button_id")
        if previous == current:
            return
        if previous is None:
            # First run under a state file. Nothing was issued under
            # another id by this service, so there is nothing to retract.
            self._write_state(discord_button_id=current)
            return
        if self.discord_bot is None:
            # ⚠️ Deliberately NOT persisted. With no channel to say it on,
            # the announcement is still owed -- keeping the old id means it
            # is made the next time Discord is configured, instead of being
            # lost to a run that happened to have Discord off.
            self._logger.info(
                "Discord button id changed %s -> %s, but Discord is not "
                "configured; older buttons stay dead until it is.",
                previous, current)
            return
        self._logger.info("Discord button id changed %s -> %s; re-issuing "
                          "buttons.", previous, current)
        label = self.printer_label()
        text = ("%s\n\nThis printer's buttons have been re-issued. Buttons "
                "on earlier messages no longer respond -- use the ones "
                "below." % label if label else
                "This printer's buttons have been re-issued. Buttons on "
                "earlier messages no longer respond -- use the ones below.")
        try:
            self.discord_bot.send(
                content=text,
                components=discord_buttons(
                    current,
                    paused=self._printer.get_state_id() == "PAUSED",
                    muted=self.alerts_muted))
        except Exception as exc:                             # noqa: BLE001
            # Not persisted, so the announcement is retried next start.
            self._logger.warning("could not re-issue Discord buttons: %s",
                                 exc)
            return
        self._write_state(discord_button_id=current)

    def _read_state(self):
        path = self._cfg.state_path()
        try:
            with io.open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            # Missing is the normal first run; corrupt is treated the same
            # way, because the only cost is one re-issued message.
            return {}

    def _write_state(self, **fields):
        path = self._cfg.state_path()
        data = self._read_state()
        data.update(fields)
        try:
            # Written whole and renamed, so a power cut cannot leave a
            # half-file that reads as "no memory" and re-announces.
            tmp = path + ".tmp"
            with io.open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(tmp, path)
        except OSError as exc:
            self._logger.warning(
                "could not write %s (%s); a printer rename will re-announce "
                "its Discord buttons on every start", path, exc)

    def current_view_image(self):
        """The current camera view as a PIL image, or None.

        A PIL image and not bytes because that is what the copied code
        passes on: telebot converts one natively, and the Discord branch
        encodes it itself.
        """
        jpeg = self._snapshot() if self._snapshot else None
        if not jpeg:
            return None
        from PIL import Image
        return Image.open(BytesIO(jpeg)).convert("RGB")

    def create_no_camera_image(self, image_size=None):
        from .placeholder import no_signal
        return (no_signal(image_size) if image_size else no_signal())

    # ---- lifecycle -----------------------------------------------------

    def start(self):
        """Start whichever channels are configured. Never fatal."""
        tg = self._cfg.get_section("telegram")
        if tg.get("enabled") and tg.get("token") and tg.get("chat_id"):
            try:
                self.telegram_bot = TelegramBot(
                    tg["token"], tg["chat_id"], self._logger,
                    self.handle_telegram_command)
                if self.telegram_bot.start():
                    self._logger.info("Telegram bot started")
                else:
                    self.telegram_bot = None
            except Exception as exc:                         # noqa: BLE001
                self._logger.error("Telegram bot could not start: %s", exc)
                self.telegram_bot = None

        dc = self._cfg.get_section("discord")
        if dc.get("enabled") and dc.get("bot_token") and dc.get("channel_id"):
            try:
                self.discord_bot = DiscordBot(
                    dc["bot_token"], dc["channel_id"], self._logger,
                    self.handle_discord_command, printer_id=self.printer_id)
                # ⚠️ Deliberately NOT `if bot.start():`. Discord connects on
                # a background thread and start() returns nothing, so a
                # truth test drops the object that was just built while its
                # gateway thread goes on connecting -- which reads in the
                # log as a healthy bot that never delivers. Telegram's
                # start() does return a flag; the two are not symmetric.
                # This is what the OctoPrint build does.
                try:
                    self.discord_bot.start()
                except Exception:
                    self.discord_bot.stop(timeout=0)
                    raise
                # The tag every button carries, and the only one this
                # instance answers. Logged because a mismatch is otherwise
                # undiagnosable: the drop is silent by design.
                self._logger.info("Discord bot started; buttons tagged %s",
                                  self.printer_id)
            except Exception as exc:                         # noqa: BLE001
                self._logger.error("Discord bot could not start: %s", exc)
                self.discord_bot = None

        if self.telegram_bot is None and self.discord_bot is None:
            self._logger.info("No notification channel configured")

    def stop(self):
        for bot, name in ((self.telegram_bot, "Telegram"),
                          (self.discord_bot, "Discord")):
            if bot is None:
                continue
            try:
                bot.stop()
            except Exception as exc:                         # noqa: BLE001
                self._logger.debug("error stopping %s: %s", name, exc)
        self.telegram_bot = self.discord_bot = None

    # ---- outbound ------------------------------------------------------

    def alert(self, caption, image=None, with_buttons=True, budgeted=True):
        """Send one alert to every configured channel.

        `image` is raw JPEG bytes. Outbound only -- the inbound replies go
        through the copied handlers, which answer whoever asked.

        `budgeted=False` exempts a message from max_notification and
        notify_interval. Operational notices use it: a camera that has gone
        blind is not a print failure, and silencing that by an alert quota
        is the opposite of what the quota is for.
        """
        if budgeted and not self._within_budget():
            return False
        if self.alerts_muted:
            self._logger.info("Muted; not sending: %s",
                              caption.split("\n")[0])
            # Muting is a deliberate choice, not a delivery failure: the
            # episode must still latch, or every frame re-fires the action.
            return True
        label = self.printer_label()
        text = "%s\n%s" % (label, caption) if label else caption
        paused = self._printer.get_state_id() == "PAUSED"

        # ⚠️ EVERY channel gets its OWN stream. Handing one BytesIO to both
        # uploads the photo to whichever runs first and 0 bytes to the
        # other: requests reads the object to EOF and nothing rewinds it.
        payload = image
        if payload is not None and not isinstance(payload, bytes):
            payload = payload.read()

        def stream():
            return BytesIO(payload) if payload else None

        # ⚠️ Both transports report success and BOTH answers were thrown
        # away. An alert that reached nobody was indistinguishable from one
        # that reached both channels, so the episode latched, the quota was
        # spent, and no later frame retried. TelegramBot.send returns the
        # message id or None; DiscordBot.send returns True or a falsey
        # value. Anything raising is a failure too, per channel, so one
        # dead channel cannot mask a live one.
        delivered = False
        if self.telegram_bot is not None:
            kb = telegram_buttons(paused=paused,
                                  muted=self.alerts_muted) if with_buttons \
                else None
            try:
                delivered = bool(self.telegram_bot.send(
                    caption=text, image=stream(), keyboard=kb)) or delivered
            except Exception as exc:                         # noqa: BLE001
                self._logger.error("Telegram alert failed: %s", exc)

        if self.discord_bot is not None:
            # ⚠️ printer_id, NOT label. Discord packs this into the button's
            # custom_id and DiscordBot answers only the id it was built
            # with; a button carrying anything else is dropped without an
            # ACK and without an INFO log, so the press does nothing at all
            # and leaves no trace. The two were the same string until
            # printer_id became the instance tag, which is what made this
            # call site wrong while the other two stayed right.
            comp = discord_buttons(
                self.printer_id, paused=paused,
                muted=self.alerts_muted) if with_buttons else None
            try:
                delivered = bool(self.discord_bot.send(
                    content=text, image=stream(),
                    components=comp)) or delivered
            except Exception as exc:                         # noqa: BLE001
                self._logger.error("Discord alert failed: %s", exc)
        if not delivered:
            self._logger.warning(
                "Alert reached no channel; not counting it as sent.")
        return delivered

    def has_channel(self):
        """Whether any channel is live, so a failure to deliver means something.

        With no bot configured there is nothing to retry, and treating that
        as a delivery failure would re-run the failure handler on every
        frame for the whole episode.
        """
        return self.telegram_bot is not None or self.discord_bot is not None

    def _within_budget(self):
        """Whether an alert may be sent now, per the notification settings.

        Suppressed alerts are DROPPED, not queued -- an alert about a
        moment that has passed is worse than none.
        """
        limits = self._cfg.notification
        cap = int(limits.get("max_notification") or 0)
        gap = float(limits.get("notify_interval") or 0)
        now = time.monotonic()
        if cap and self._alerts_sent >= cap:
            self._logger.info(
                "Alert suppressed: %d already sent this print, the limit is "
                "%d.", self._alerts_sent, cap)
            return False
        if gap and self._last_alert_at and now - self._last_alert_at < gap:
            self._logger.info(
                "Alert suppressed: %.0fs since the last one, the minimum "
                "interval is %.0fs.", now - self._last_alert_at, gap)
            return False
        self._alerts_sent += 1
        self._last_alert_at = now
        return True

    def new_print(self):
        """Invalidate outstanding confirmations at every print boundary."""
        self._new_confirm_scope()
        # Muting is per print, which is what MUTED_LINE promises.
        self.alerts_muted = False
        self._alerts_sent = 0
        self._last_alert_at = 0.0

    # ==================================================================
    # COPIED VERBATIM from the OctoPrint build's notify.py.
    # Do not edit. tools/check_upstream_sync.py compares them.
    # ==================================================================

    def handle_telegram_command(self, command, call):
        """Execute one authorised Telegram message or button command."""
        if call is None:
            if command == "/hi":
                image, caption = self.check_reply()
                self.telegram_send_with_reply(image=image, caption=caption,
                                              reply_buttons=4,
                                              disable_notification=True)
                return None
            return ("I am PiNozCam. Send or click /hi or click Check button "
                    "from previous messages to see the current camera view "
                    "and printer info.")

        message_id = call.message.message_id
        data = command
        if data.split(":")[0] in ("yes", "no"):
            parts = data.split(":")
            verb = parts[0]
            action = parts[1] if len(parts) > 2 else ""
            nonce = parts[2] if len(parts) > 2 else ""
            if not action:
                self.telegram_send_with_reply(
                    caption="That button is from an older message and no "
                            "longer works. Press Pause or Stop again.",
                    reply_buttons=0, disable_notification=True)
                return
            if verb == "no":
                self._drop_confirm(action, nonce, channel="telegram")
                self._logger.info(
                    "Telegram user declined %s (message %s)",
                    action, message_id)
                self.telegram_send_with_reply(
                    caption="Never Mind.", reply_buttons=0,
                    disable_notification=True)
                return
            verdict = self._consume_confirm(action, nonce, "telegram")
            if verdict == "expired":
                self.telegram_send_with_reply(
                    caption="You have to respond within %d seconds."
                            % self.CONFIRM_TTL,
                    reply_buttons=0, disable_notification=True)
            elif verdict != "ok":
                self._logger.warning(
                    "Ignoring a Telegram %s confirmation that is no "
                    "longer valid (earlier print, already used, or "
                    "declined).", action)
                self.telegram_send_with_reply(
                    caption="That button is no longer valid. Press "
                            "Pause or Stop again if you still want it.",
                    reply_buttons=0, disable_notification=True)
            else:
                # Same re-check as the Discord side: the print can end
                # between the offer and the tap, and pause/cancel on an
                # idle printer is a no-op that was reported as done.
                problem = self._action_state_problem(action)
                if problem is not None:
                    self._logger.info(
                        "Telegram confirmation of %s refused: %s", action,
                        problem)
                    self.telegram_send_with_reply(
                        caption=problem, reply_buttons=0,
                        disable_notification=True)
                    return
                self._logger.info("Telegram user confirmed %s", action)
                if action == "pause":
                    self._printer.pause_print()
                    self.current_telegram_message_paused = True
                    self.telegram_send_with_reply(
                        caption="The print job has been paused.",
                        reply_buttons=0, disable_notification=True)
                elif action == "resume":
                    self._printer.resume_print()
                    self.current_telegram_message_paused = False
                    self.telegram_send_with_reply(
                        caption="The print job has been resumed.",
                        reply_buttons=0, disable_notification=True)
                elif action == "stop":
                    self._printer.cancel_print()
                    self.telegram_send_with_reply(
                        caption="The print job has been stopped.",
                        reply_buttons=0, disable_notification=True)
            return
        if call.data == "check":
            self._logger.info(
                "User clicked 'Check' button for message ID: %s", message_id)
            image, caption = self.check_reply()
            self.telegram_send_with_reply(image=image, caption=caption,
                                          reply_buttons=4,
                                          disable_notification=True)
        elif call.data in ("mute", "unmute"):
            # too. See PinozcamPlugin.__init__ for why. Set, never
            # toggled -- the button says which way it goes.
            self.alerts_muted = call.data == "mute"
            self._logger.info(
                "User clicked '%s' for message ID: %s",
                "Mute" if self.alerts_muted else "Unmute", message_id)
            self.telegram_send_with_reply(
                caption=(self.MUTED_TEXT if self.alerts_muted
                         else self.UNMUTED_TEXT),
                reply_buttons=0, disable_notification=True)
        # Printer state, not message bookkeeping, authorises actions.
        elif call.data in ("pause", "stop"):
            paused = self._printer.get_state_id() == "PAUSED"
            action = "resume" if (call.data == "pause" and paused) \
                else call.data
            problem = self._action_state_problem(action)
            if problem is not None:
                self._logger.info(
                    "Telegram %s offer refused: %s", action, problem)
                self.telegram_send_with_reply(
                    caption=problem, reply_buttons=0,
                    disable_notification=True)
                return None
            self._logger.info("User clicked '%s' button for message ID: %s",
                              call.data.capitalize(), message_id)
            self.telegram_send_with_reply(
                caption="Are you sure you want to %s the print job?" % action,
                reply_buttons=2, disable_notification=True,
                confirm=(action, self._issue_confirm(action, "telegram")))
        return None

    def handle_discord_command(self, command, interaction):
        """Run one command, from a button click or a typed message.

        interaction is None for typed commands, in which case replies go to
        the channel instead of back through the interaction token.
        """
        def reply(text, components=None, image=None):
            """Send one reply to an interaction or channel."""
            stream = None
            if image is not None:
                stream = BytesIO()
                image.save(stream, format="JPEG")
                stream.seek(0)
            bot = self.discord_bot
            if interaction is not None and bot is not None:
                bot.followup(interaction, text, components=components,
                             image=stream, silent=True)
            elif bot is not None:
                bot.send(content=text, image=stream, components=components,
                         silent=True)

        if command.startswith("confirm:"):
            parts = command.split(":")
            action = parts[1] if len(parts) > 1 else ""
            token = parts[2] if len(parts) > 2 else ""
            verdict = self._consume_confirm(action, token, "discord")
            if verdict == "stale":
                self._logger.warning(
                    "Ignoring a Discord %s button that does not match the "
                    "current request (earlier print, already used, or "
                    "withdrawn by Cancel).", action or "confirm")
                reply("That button is no longer valid. Press Pause or Stop "
                      "again if you still want it.", components=[])
                return
            if verdict == "expired":
                reply("That request expired. Press it again if you still "
                      "want it.", components=[])
                return
            # The print state may have changed since confirmation was offered.
            problem = self._action_state_problem(action)
            if problem is not None:
                self._logger.info(
                    "Discord confirmation of %s refused: %s", action,
                    problem)
                reply(problem, components=[])
                return
            if action == "pause":
                self._printer.pause_print()
            elif action == "resume":
                self._printer.resume_print()
            elif action == "stop":
                self._printer.cancel_print()
            self._logger.info("Discord user confirmed %s", action)
            reply({
                "pause": "Print paused.",
                "resume": "Print resumed.",
                "stop": "Print stopped.",
            }[action], components=[])
            return
        if command.startswith("cancel"):
            # Cancel must invalidate the paired confirmation nonce.
            parts = command.split(":")
            action = parts[1] if len(parts) > 1 else ""
            token = parts[2] if len(parts) > 2 else None
            if action:
                self._drop_confirm(action, token, channel="discord")
            else:
                # An old message whose button predates the action-carrying
                # id. Withdraw everything rather than leave a live Yes.
                self._new_confirm_scope()
            reply("Never mind.", components=[])
            return

        if command == "help":
            reply(self.DISCORD_HELP)
        elif command == "status":
            title, state, progress, nozzle, bed, meta = self.get_printer_status()
            body = ("Printer: %s\nStatus: %s\nProgress: %s\n"
                    "Nozzle: %s\u00b0C\nBed: %s\u00b0C"
                    % (title, state, progress, nozzle, bed))
            if meta:
                body += "\nFile: %s" % meta.get("name", "Unknown")
            reply(body)
        elif command == "check":
            # Camera and printer availability are independent.
            image, caption = self.check_reply()
            # get_state_id(), the idiom the rest of this file and detect.py
            # already use, rather than is_paused(): one way of asking, so the
            # label on this row cannot disagree with an alert's row.
            reply(caption, image=image,
                  components=discord_buttons(
                      self.printer_id,
                      paused=self._printer.get_state_id() == "PAUSED",
                      muted=self.alerts_muted))
        elif command in ("mute", "unmute"):
            # One target-state flag mutes both media idempotently.
            self.alerts_muted = command == "mute"
            # The SAME string both platforms use, from one constant, because
            # a promise that differs between them is one a user has to
            # discover. It says "all alerts" because that is now true.
            reply(self.MUTED_TEXT if self.alerts_muted else self.UNMUTED_TEXT)
        elif command in ("pause", "resume", "stop"):
            # A confirmation step, for the same reason the Telegram path has
            # one: a mis-tap should not end a nine-hour print. The buttons
            # make it one more tap rather than a typed word.
            paused = self._printer.get_state_id() == "PAUSED"
            action = "resume" if (command == "pause" and paused) else command
            # No offer for a printer that cannot honour it: a Yes/No over
            # an idle printer is a live Stop button waiting for the NEXT
            # print to start inside its TTL.
            problem = self._action_state_problem(action)
            if problem is not None:
                reply(problem)
                return
            token = self._issue_confirm(action, "discord")
            reply("Confirm: %s the print? (valid for %ds)"
                  % (action, self.CONFIRM_TTL),
                  components=discord_confirm_buttons(
                      self.printer_id, action, token))

    def _action_state_problem(self, action):
        """Return why action is invalid for the current printer state."""
        state = self._printer.get_state_id()
        if action == "pause":
            if state in ("PRINTING", "RESUMING"):
                return None
            if state in ("PAUSED", "PAUSING"):
                return "The print is already paused."
            return "There is no active print job."
        if action == "resume":
            if state in ("PAUSED", "PAUSING"):
                return None
            if state in ("PRINTING", "RESUMING"):
                return "The print is not paused."
            return "There is no active print job."
        if action == "stop":
            if state in ("PRINTING", "PAUSED", "PAUSING", "RESUMING"):
                return None
            return "There is no active print job."
        return None

    def check_reply(self):
        """Build the shared Telegram/Discord camera and printer reply."""
        title, state, progress, nozzle_temp, bed_temp, file_metadata = (
            self.get_printer_status())
        caption = (f"Printer: {title}\nStatus: {state}\n"
                   f"Progress: {progress}\nNozzle Temp: {nozzle_temp}°C\n"
                   f"Bed Temp: {bed_temp}°C")
        if file_metadata:
            caption += f"\nFile: {file_metadata.get('name', 'Unknown')}"
        # The user-facing view is unmasked; camera failures use a placeholder.
        try:
            image = self.current_view_image()
        except Exception as exc:                        # noqa: BLE001
            self._logger.warning("Check could not get a frame: %s",
                                 self.redact(str(exc)))
            image = None
        if image is None:
            caption += ("\n⚠️ No camera connected -- failure detection is "
                        "blind, but the print above is unaffected.")
            image = self.create_no_camera_image()
        return image, caption

    def telegram_send_with_reply(self, image=None, caption='',
                                 reply_buttons=0,
                                 disable_notification=False, confirm=None):
        """Send Telegram content with optional controls or confirmation."""
        bot = self.telegram_bot
        if bot is None:
            return False
        keyboard = None
        if reply_buttons == 2:
            if not confirm:
                # Refusing is the safe direction: a Yes/No row with no
                # nonce cannot be answered, so sending one would offer the
                # user a button that never works.
                self._logger.error(
                    "Refusing to send a Telegram confirmation with no "
                    "token; this is a programming error.")
                return False
            keyboard = telegram_confirm_buttons(*confirm)
        elif reply_buttons == 4:
            # Read printer state directly so both channels show the same action.
            keyboard = telegram_buttons(
                paused=self._printer.get_state_id() == "PAUSED",
                muted=self.alerts_muted)
        message_id = bot.send(caption=caption, image=image,
                              keyboard=keyboard,
                              silent=disable_notification)
        if message_id is None:
            return False
        if self.ai_running:
            self.current_telegram_message_set.add(message_id)
        return True

    def get_printer_status(self):
        """Return job progress, temperatures and current-file metadata."""

        title = self.printer_label()

        # Get the current printer data
        printer_data = self._printer.get_current_data()

        # Get the printer state
        state_id = self._printer.get_state_id()

        # A printer whose USB has dropped is one of the two independent
        # things that can go wrong here -- the other being the camera -- and
        # it changes what the buttons can do: Pause and Stop have nothing to
        # command. "❓ Unknown" for that left the user with no idea which of
        # the two had failed, on the one message they went looking for an
        # answer in.
        if state_id == "PRINTING":
            state = "🖨️ Printing"
            # OctoPrint reports completion as a percentage from 0 to 100.
            progress = printer_data['progress']['completion']
        elif state_id == "PAUSED":
            state = "⏸️ Paused"
            progress = printer_data['progress']['completion']
        elif state_id == "OPERATIONAL":
            state = "⏹️ Idle"
            progress = 0
        elif state_id in ("OFFLINE", "CLOSED"):
            state = "🔌 Printer disconnected"
            progress = 0
        elif state_id in ("CLOSED_WITH_ERROR", "OFFLINE_AFTER_ERROR",
                          "ERROR"):
            state = "🔌 Printer disconnected (error)"
            progress = 0
        elif state_id in ("CONNECTING", "DETECT_SERIAL", "DETECT_BAUDRATE"):
            state = "🔌 Connecting to the printer"
            progress = 0
        elif state_id in ("CANCELLING", "FINISHING"):
            state = "⏹️ Finishing"
            progress = printer_data['progress']['completion']
        else:
            # Still reachable: OctoPrint can add states, and it is better to
            # show the raw id than to hide it behind a question mark.
            state = "❓ %s" % state_id
            progress = 0

        # Initialize temperature variables
        nozzle_temp = 0
        bed_temp = 0

        # Set default values if any value is None
        state = state or "Unknown"
        progress = f"{progress:.1f}%" if progress is not None else "0.0%"

        # Get temperature information
        temperatures = {}
        for k, v in self._printer.get_current_temperatures().items():
            if re.search(r'^(tool\d+|bed|chamber)$', k):
                temperatures[k] = v
        nozzle_temp = temperatures.get('tool0', {}).get('actual', nozzle_temp)
        bed_temp = temperatures.get('bed', {}).get('actual', bed_temp)

        # Get file metadata
        file_metadata = printer_data.get('job', {}).get('file', {})

        return title, state, progress, nozzle_temp, bed_temp, file_metadata
