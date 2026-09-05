# Telegram and Discord setup

[← Back to README](../README.md)

Both integrations are optional, and both are the SAME code as the
OctoPrint build -- `telegram_bot.py`, `discord_bot.py` and the command
handlers are copied byte-for-byte, so the bots behave identically.

⚠️ One thing the OctoPrint build has and this one does not: a **Connection
Test** button. Credentials are checked when the service starts, so save the
file and watch the log:

```
sudo systemctl restart moonraker-pinozcam
journalctl -u moonraker-pinozcam -n 30
```

`Telegram bot started` and `Discord bot started` mean the credentials were
accepted.

With either one connected, your phone gets failure alerts with the analysed
picture, and these buttons work from the chat:

- 🔍 **Check** — see the current camera view and printer status.
- 🔇 **Mute / Unmute** — control alerts for the current print.
- ⏸️ **Pause / Resume** — step in without opening Mainsail.
- ⏹️ **Stop** — cancel the print after a confirmation.

Typed commands work too: `/hi` on Telegram, `!check` on Discord.

## ✈️ Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the
   **Bot Token**.
2. Get your **Chat ID** (for a private chat, a group, or a supergroup). Chat
   IDs must contain at least five digits, with an optional leading minus sign
   for a group.
3. Put both in `~/printer_data/config/moonraker-pinozcam.cfg`:

   ```ini
   [telegram]
   enabled = true
   token = 1234567890:AA...
   chat_id = 987654321
   ```

   Then restart the service. The settings page at
   `http://<printer>:58888` can also set them, but it never reads a saved
   token back -- it shows dots. Type over them to replace, clear the field
   to remove, leave them alone to keep.

Use **one bot token per printer**. Several printers may post into the same
chat, but Telegram permits only one active update consumer per bot token.
Sharing a token makes button delivery unpredictable and produces a Telegram
`409 Conflict`.

## 💬 Discord

1. Create a bot at
   [discord.com/developers](https://discord.com/developers/applications).
2. Invite it with permission to view the channel, send messages, and attach
   files.
3. Put its **Bot Token** and the channel's 17–20 digit **Channel ID**
   in the same file:

   ```ini
   [discord]
   enabled = true
   bot_token = MTIx...
   channel_id = 000000000000000000
   ```

   Channel IDs are handled as text so a very large ID cannot be rounded.

No incoming port, public domain, or TLS certificate is needed — the bot only
makes outbound connections. If the button connection drops temporarily, alerts
are still delivered; buttons recover on reconnect.

## Monitoring several printers

- **Telegram:** one bot per printer; they may all post into the same group.
- **Discord:** printers may share one bot and one channel. Buttons carry a
  hidden per-installation identity, so only the printer that created a message
  answers its buttons. Typed commands such as `!check` have no target — in a
  shared channel, use buttons instead.

Give every printer a distinct name so its messages are easy to
recognise. Set `name` under `[printer]`; if you leave it empty the label is
taken from what you named the printer in Mainsail or Fluidd, then the
hostname, then `printer-<id>` derived from Moonraker's own `instance_id`.

## Privacy notes

| Optional configuration | Data sent outside your network |
|---|---|
| Telegram credentials enabled | analysed camera image and print status to Telegram |
| Discord credentials enabled | analysed camera image and print status to Discord; commands read from the configured channel |

Anyone allowed to operate the configured chat/channel controls may request
a view or operate the printer, so use a private destination and protect bot
tokens as passwords. Tokens and camera credentials are redacted from logs.

⚠️ The page on port 58888 has **no login** and answers anyone who can reach
the printer. That is why it never hands a saved token back. Do not
port-forward it, or Moonraker, to the internet; see
[REMOTE_ACCESS.md](REMOTE_ACCESS.md).
