# Discord Event + Party Tracker Bot

Tracks events and parties with:

- `/event` to log an event
- `/party` to log a party
- a dropdown for **1 host**
- a dropdown for **multiple assistants**
- event/party name, English/UK date, time, and winning ID
- automatic host payout of **$500,000 dirty money** per event or party
- separate configured channels for event logs and party logs
- admin-only comped-items checkbox button on each log
- `/money` to check dirty money balances
- automatic weekly report showing each person's hosted count, assisted count, and dirty money earned that week
- automatic weekly money reset every Monday after the weekly report is sent
- `/weeklyreport` for admins to manually send the weekly report
- `/resetmoney` for admins to manually reset all balances
- `/logs` to list recent logs

## Setup

1. Install Python 3.10+.
2. Create a Discord bot in the Discord Developer Portal.
3. Invite the bot with these scopes:
   - `bot`
   - `applications.commands`
4. Give the bot permission to read/send messages in your log channels.
5. Enable **Developer Mode** in Discord so you can copy IDs.
6. Install dependencies:

```bash
pip install -r requirements.txt
```

7. Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

8. Fill in your `.env`:

```env
DISCORD_TOKEN=your-token
DISCORD_GUILD_ID=your-server-id
EVENT_LOG_CHANNEL_ID=channel-for-event-logs
PARTY_LOG_CHANNEL_ID=channel-for-party-logs
WEEKLY_REPORT_CHANNEL_ID=channel-for-weekly-reports
HOST_PAYOUT=500000
WEEKLY_RESET_ENABLED=true
WEEKLY_RESET_DAY=0
WEEKLY_RESET_HOUR_UTC=0
```

9. Run it:

```bash
python bot.py
```

## Commands

### `/event`

Opens a form to log an event. After the form, it shows dropdowns for:

- hosted by: exactly 1 person
- assisted by: 0 to 25 people

The date accepts English/UK format like `10/05/2026`, and the host earns `$500,000 dirty money` by default. The log posts to the normal event or party log channel with a **Comped items** button that only admins can tick or untick.

### `/party`

Same as `/event`, but posts to the party log channel.

### `/money`

Shows dirty money balances. Use the optional `member` field to check one person.

### `/weeklyreport`

Admin-only command that sends the weekly report to `WEEKLY_REPORT_CHANNEL_ID`. The report shows every person who hosted or assisted during the week, with hosted count, assisted count, and dirty money earned.

### `/resetmoney`

Admin-only command that resets everyone's dirty money balance to `$0`.

### `/logs`

Shows recent event and party logs, including whether the winning ID has been comped their items.

## Notes

Discord user dropdowns use Discord's built-in user selector. They do not require you to hard-code staff names.

If a host is also selected as an assistant, the bot removes them from the assistant list automatically so they are not listed twice.

## Weekly report and reset

By default, the bot sends the weekly report every Monday at `00:00 UTC`, then resets dirty money balances. Railway runs in UTC, so adjust `WEEKLY_RESET_HOUR_UTC` if you want a different report/reset time.

- `WEEKLY_RESET_DAY=0` means Monday.
- `WEEKLY_RESET_DAY=6` means Sunday.
- `WEEKLY_RESET_HOUR_UTC=0` means midnight UTC.

## Comped items checkbox

Every event/party log has a `☐ Comped items` button. Only server admins can click it. When an admin clicks it, the log updates to `✅ Comped items`; clicking again unticks it.
"# discord-tracker-entertainment" 
"# discord-tracker-entertainment" 
