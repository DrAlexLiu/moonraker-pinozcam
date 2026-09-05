"""Alerts and remote control over Telegram and Discord.

Both bot modules are shared verbatim with the OctoPrint build, so this file
only supplies what they call back into: the printer actions, the state they
report, and the confirmation flow that guards a destructive command.
"""

import io
import threading
import time

import threading as _threading

from .confirm import ConfirmMixin


class Notifier(ConfirmMixin):
    """Owns the bots and translates their commands into printer actions."""

    def __init__(self, config, client, logger, snapshot=None,
                 status=None):
        self._cfg = config
        self._client = client
        self._log = logger
        self._snapshot = snapshot      # () -> (jpeg_bytes | None)
        self._status = status          # () -> str, one line for /hi

        self.telegram = None
        self.discord = None
        self._muted = False

        # ConfirmMixin's state. It already handles what this file would
        # otherwise get wrong: per-channel isolation, a scope bumped at
        # every print so an in-flight reply from a finished job is refused,
        # and check-and-remove as one critical section so two taps arriving
        # together cannot both cancel.
        self.confirm_lock = _threading.Lock()
        self.confirm_tokens = {}
        self.confirm_scope = 0

    # ---- lifecycle ----------------------------------------------------

    def start(self):
        """Start whichever channels are configured. Never fatal."""
        tg = self._cfg.get_section("telegram")
        if tg.get("enabled") and tg.get("token") and tg.get("chat_id"):
            try:
                from .telegram_bot import TelegramBot
                self.telegram = TelegramBot(
                    tg["token"], tg["chat_id"], self._log, self._on_command)
                if self.telegram.start():
                    self._log.info("Telegram bot started")
                else:
                    self.telegram = None
            except Exception as exc:                         # noqa: BLE001
                self._log.error("Telegram bot could not start: %s", exc)
                self.telegram = None

        dc = self._cfg.get_section("discord")
        if dc.get("enabled") and dc.get("bot_token") and dc.get("channel_id"):
            try:
                from .discord_bot import DiscordBot
                self.discord = DiscordBot(
                    dc["bot_token"], dc["channel_id"], self._log,
                    self._on_command, printer_id=self.printer_id)
                # ⚠️ Deliberately NOT `if self.discord.start():`. Discord
                # connects on a background thread and start() returns
                # nothing, so a truth test on it always fails and drops the
                # object that was just built -- while its gateway thread
                # goes on connecting, which is what made this look like a
                # working bot that never delivered anything. Telegram's
                # start() does return a flag; these two are not symmetric.
                try:
                    self.discord.start()
                except Exception:
                    self.discord.stop(timeout=0)
                    raise
                self._log.info("Discord bot started")
            except Exception as exc:                         # noqa: BLE001
                self._log.error("Discord bot could not start: %s", exc)
                self.discord = None

        if self.telegram is None and self.discord is None:
            self._log.info("No notification channel configured")

    def stop(self):
        for bot, name in ((self.telegram, "Telegram"),
                          (self.discord, "Discord")):
            if bot is None:
                continue
            try:
                bot.stop()
            except Exception as exc:                         # noqa: BLE001
                self._log.debug("error stopping %s: %s", name, exc)
        self.telegram = self.discord = None

    @property
    def printer_id(self):
        """A stable short label so one chat can serve several printers.

        The config file wins when it is set; otherwise the client resolves
        it the way the OctoPrint build does -- frontend name, hostname,
        then Moonraker's instance_id.
        """
        return (self._cfg.get("printer", "name", "")
                or self._client.printer_name())

    # ---- outbound -----------------------------------------------------

    def alert(self, caption, image=None, with_buttons=True):
        """Send one alert to every configured channel.

        `image` is raw JPEG bytes, or a file-like positioned at its start.
        """
        if self._muted:
            self._log.info("Muted; not sending: %s", caption.split("\n")[0])
            return
        label = self.printer_id
        text = "%s\n%s" % (label, caption) if label else caption

        # ⚠️ EVERY channel gets its OWN stream. Handing one BytesIO to both
        # uploads the photo to whichever runs first and 0 bytes to the
        # other: requests reads the object to EOF and nothing rewinds it.
        # The bug hid for as long as Discord failed to start, because only
        # one channel was ever live at a time. The OctoPrint build avoids
        # it by passing a PIL Image and encoding a fresh stream per
        # channel; this build already has the encoded bytes, so it copies
        # those instead.
        payload = image
        if payload is not None and not isinstance(payload, bytes):
            payload = payload.read()

        def stream():
            return io.BytesIO(payload) if payload else None

        if self.telegram is not None:
            from . import telegram_bot
            kb = telegram_bot.buttons(
                paused=self._client.state.is_paused,
                muted=self._muted) if with_buttons else None
            self.telegram.send(caption=text, image=stream(), keyboard=kb)

        if self.discord is not None:
            from . import discord_bot
            comp = discord_bot.buttons(
                label, paused=self._client.state.is_paused,
                muted=self._muted) if with_buttons else None
            self.discord.send(content=text, image=stream(), components=comp)

    # ---- inbound ------------------------------------------------------

    def _on_command(self, name, call):
        """Handle one button press or typed command from either platform.

        Returns a string when the caller should reply with it, else None.
        """
        name = (name or "").strip()

        if name.startswith("yes:") or name.startswith("no:"):
            return self._resolve_confirmation(name)

        if name in ("check", "/check", "/hi", ""):
            return self._do_check(name)
        if name in ("mute", "unmute"):
            self._muted = (name == "mute")
            return "Muted." if self._muted else "Unmuted."
        if name in ("pause", "stop"):
            return self._offer_confirmation(name)

        return ("Unknown command. Use the buttons on an alert, or /hi for "
                "status.")

    def _do_check(self, name):
        """Answer a status request, with a fresh photo when available."""
        line = self._status() if self._status else str(self._client.state)
        jpeg = self._snapshot() if self._snapshot else None
        if jpeg is not None:
            self.alert(line, image=jpeg, with_buttons=True)
            return None            # the photo IS the answer
        return line

    def _offer_confirmation(self, action):
        """Ask before doing anything destructive.

        Pause is included deliberately: an unattended print paused by
        accident cools and often cannot be resumed cleanly.
        """
        state = self._client.state
        if not state.is_printing and action == "pause":
            return "Nothing is printing right now."

        verb = "pause" if action == "pause" else "STOP"
        text = "Really %s the print on %s?" % (verb, self.printer_id)
        # One nonce per channel: a Telegram button must not be answerable
        # from Discord, and vice versa.
        if self.telegram is not None:
            from . import telegram_bot
            nonce = self._issue_confirm(action, "telegram")
            self.telegram.send(
                caption=text,
                keyboard=telegram_bot.confirm_buttons(action, nonce))
        if self.discord is not None:
            from . import discord_bot
            nonce = self._issue_confirm(action, "discord")
            self.discord.send(
                content=text,
                components=discord_bot.confirm_buttons(
                    self.printer_id, action, nonce))
        return None

    def _resolve_confirmation(self, data, channel="telegram"):
        """Act on a confirmation reply, or explain why it is not valid."""
        try:
            answer, action, nonce = data.split(":", 2)
        except ValueError:
            return "That button is malformed."

        if answer == "no":
            self._consume_confirm(action, nonce, channel)
            return "Cancelled; the print was left alone."

        verdict = self._consume_confirm(action, nonce, channel)
        if verdict == "expired":
            return "That request expired. Press the button again."
        if verdict != "ok":
            # Covers a reused button and one found in old chat history.
            return "That button is no longer valid. Press it again."

        # Re-validate: the print may have finished, failed or been paused by
        # someone else while the confirmation sat unanswered.
        state = self._client.state
        if not state.is_printing:
            return "The printer is no longer printing (%s); nothing done." \
                % state.state
        try:
            if action == "pause":
                self._client.pause_print()
                return "Paused."
            self._client.cancel_print()
            return "Print cancelled."
        except Exception as exc:                             # noqa: BLE001
            self._log.error("Could not %s: %s", action, exc)
            return "Could not %s the print: %s" % (action, exc)

    def new_print(self):
        """Invalidate outstanding confirmations at every print boundary."""
        self._new_confirm_scope()
