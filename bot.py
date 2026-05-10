import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")  # optional, faster slash command sync while testing
DB_PATH = os.getenv("DATABASE_PATH", "events.db")
EVENT_LOG_CHANNEL_ID = os.getenv("EVENT_LOG_CHANNEL_ID")
PARTY_LOG_CHANNEL_ID = os.getenv("PARTY_LOG_CHANNEL_ID")
WEEKLY_REPORT_CHANNEL_ID = os.getenv("WEEKLY_REPORT_CHANNEL_ID")
HOST_PAYOUT = int(os.getenv("HOST_PAYOUT", "500000"))
WEEKLY_RESET_ENABLED = os.getenv("WEEKLY_RESET_ENABLED", "true").lower() == "true"
WEEKLY_RESET_DAY = int(os.getenv("WEEKLY_RESET_DAY", "0"))  # 0 = Monday, 6 = Sunday
WEEKLY_RESET_HOUR_UTC = int(os.getenv("WEEKLY_RESET_HOUR_UTC", "0"))

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in .env")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


def db_connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    with db_connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                log_type TEXT NOT NULL,
                event_name TEXT NOT NULL,
                event_date TEXT NOT NULL,
                event_time TEXT NOT NULL,
                winning_id TEXT NOT NULL,
                host_user_id INTEGER NOT NULL,
                host_display_name TEXT NOT NULL,
                assisted_user_ids TEXT,
                assisted_display_names TEXT,
                notes TEXT,
                payout INTEGER NOT NULL DEFAULT 0,
                created_by_user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                comped_items INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        try:
            con.execute("ALTER TABLE logs ADD COLUMN comped_items INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS money_balances (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                dirty_money INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_settings (
                guild_id INTEGER NOT NULL,
                setting_key TEXT NOT NULL,
                setting_value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, setting_key)
            )
            """
        )
        con.commit()


def validate_date(date_text: str) -> str:
    text = date_text.strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError("Date must be in English/UK format DD/MM/YYYY, for example 10/05/2026.")


def format_english_date(iso_date: str) -> str:
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d %B %Y")
    except ValueError:
        return iso_date


def validate_time(time_text: str) -> str:
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(time_text.strip(), fmt).strftime("%H:%M")
        except ValueError:
            pass
    raise ValueError("Time must be in 24-hour HH:MM format, for example 19:30.")


def parse_channel_id(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


async def send_embed_to_channel(interaction: discord.Interaction, channel_id_value: Optional[str], embed: discord.Embed, view: Optional[discord.ui.View] = None):
    channel_id = parse_channel_id(channel_id_value)
    if not channel_id:
        return False

    channel = interaction.guild.get_channel(channel_id) if interaction.guild else None
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.DiscordException:
            return False

    return await channel.send(embed=embed, view=view)


async def send_to_log_channel(interaction: discord.Interaction, log_type: str, embed: discord.Embed, view: Optional[discord.ui.View] = None):
    channel_id_value = EVENT_LOG_CHANNEL_ID if log_type == "event" else PARTY_LOG_CHANNEL_ID
    return await send_embed_to_channel(interaction, channel_id_value, embed, view=view)


async def send_embed_to_channel_id(channel_id_value: Optional[str], embed: discord.Embed):
    channel_id = parse_channel_id(channel_id_value)
    if not channel_id:
        return False
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        await channel.send(embed=embed)
        return True
    except discord.DiscordException:
        return False


def get_week_key(now: datetime) -> str:
    monday = now.date()
    monday = monday.fromordinal(monday.toordinal() - monday.weekday())
    return monday.isoformat()


def get_previous_week_range(now: datetime):
    this_monday = now.date().fromordinal(now.date().toordinal() - now.date().weekday())
    previous_monday = this_monday.fromordinal(this_monday.toordinal() - 7)
    return previous_monday.isoformat(), this_monday.isoformat()


def build_weekly_report_embed(guild: discord.Guild, week_start: str, week_end: str) -> discord.Embed:
    with db_connect() as con:
        host_rows = con.execute(
            """
            SELECT host_user_id, host_display_name, COUNT(*) AS hosted, COALESCE(SUM(payout), 0) AS earned
            FROM logs
            WHERE guild_id = ? AND event_date >= ? AND event_date < ?
            GROUP BY host_user_id, host_display_name
            """,
            (guild.id, week_start, week_end),
        ).fetchall()

        assist_rows = con.execute(
            """
            SELECT assisted_user_ids, assisted_display_names
            FROM logs
            WHERE guild_id = ? AND event_date >= ? AND event_date < ? AND assisted_user_ids IS NOT NULL AND assisted_user_ids != ''
            """,
            (guild.id, week_start, week_end),
        ).fetchall()

    stats = {}
    for user_id, display_name, hosted, earned in host_rows:
        stats[int(user_id)] = {
            "name": display_name,
            "hosted": int(hosted),
            "assisted": 0,
            "earned": int(earned or 0),
        }

    for ids_text, names_text in assist_rows:
        ids = [x for x in (ids_text or "").split(",") if x]
        names = [x.strip() for x in (names_text or "").split(",")]
        for index, user_id_text in enumerate(ids):
            try:
                user_id = int(user_id_text)
            except ValueError:
                continue
            name = names[index] if index < len(names) and names[index] else f"User {user_id}"
            stats.setdefault(user_id, {"name": name, "hosted": 0, "assisted": 0, "earned": 0})
            stats[user_id]["assisted"] += 1

    start_text = format_english_date(week_start)
    # week_end is exclusive, so display the day before it.
    end_date = datetime.strptime(week_end, "%Y-%m-%d").date().fromordinal(datetime.strptime(week_end, "%Y-%m-%d").date().toordinal() - 1).isoformat()
    end_text = format_english_date(end_date)

    embed = discord.Embed(
        title="Weekly Event & Party Report",
        description=f"Report for **{start_text}** to **{end_text}**",
        color=discord.Color.gold(),
    )

    if not stats:
        embed.add_field(name="No activity", value="No events or parties were logged this week.", inline=False)
        return embed

    sorted_stats = sorted(stats.items(), key=lambda item: (-item[1]["hosted"], -item[1]["assisted"], item[1]["name"].lower()))
    lines = []
    for user_id, data in sorted_stats:
        lines.append(
            f"<@{user_id}> — Hosted: **{data['hosted']}** | Assisted: **{data['assisted']}** | Dirty money: **${data['earned']:,}**"
        )

    # Discord embed fields have a 1024 character limit, so split into chunks.
    chunk = ""
    part = 1
    for line in lines:
        if len(chunk) + len(line) + 1 > 1000:
            embed.add_field(name=f"Totals {part}", value=chunk, inline=False)
            chunk = ""
            part += 1
        chunk += line + "\n"
    if chunk:
        embed.add_field(name=f"Totals {part}", value=chunk, inline=False)

    return embed


async def send_weekly_report_if_due(guild: discord.Guild, force: bool = False) -> bool:
    now = datetime.now(timezone.utc)
    if not force:
        if now.weekday() != WEEKLY_RESET_DAY or now.hour < WEEKLY_RESET_HOUR_UTC:
            return False

    week_start, week_end = get_previous_week_range(now)
    setting_key = "last_weekly_report_week"
    now_text = now.isoformat(timespec="seconds")

    with db_connect() as con:
        row = con.execute(
            "SELECT setting_value FROM bot_settings WHERE guild_id = ? AND setting_key = ?",
            (guild.id, setting_key),
        ).fetchone()
        if not force and row and row[0] == week_start:
            return False

    embed = build_weekly_report_embed(guild, week_start, week_end)
    sent = await send_embed_to_channel_id(WEEKLY_REPORT_CHANNEL_ID, embed)
    if not sent:
        return False

    with db_connect() as con:
        con.execute(
            """
            INSERT INTO bot_settings (guild_id, setting_key, setting_value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, setting_key) DO UPDATE SET
                setting_value = excluded.setting_value,
                updated_at = excluded.updated_at
            """,
            (guild.id, setting_key, week_start, now_text),
        )
        con.commit()
    return True


def reset_money_for_guild_if_due(guild_id: int, force: bool = False) -> bool:
    now = datetime.now(timezone.utc)
    if not force:
        if now.weekday() != WEEKLY_RESET_DAY or now.hour < WEEKLY_RESET_HOUR_UTC:
            return False

    week_key = get_week_key(now)
    setting_key = "last_money_reset_week"
    now_text = now.isoformat(timespec="seconds")

    with db_connect() as con:
        row = con.execute(
            "SELECT setting_value FROM bot_settings WHERE guild_id = ? AND setting_key = ?",
            (guild_id, setting_key),
        ).fetchone()
        if not force and row and row[0] == week_key:
            return False

        con.execute(
            "UPDATE money_balances SET dirty_money = 0, updated_at = ? WHERE guild_id = ?",
            (now_text, guild_id),
        )
        con.execute(
            """
            INSERT INTO bot_settings (guild_id, setting_key, setting_value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, setting_key) DO UPDATE SET
                setting_value = excluded.setting_value,
                updated_at = excluded.updated_at
            """,
            (guild_id, setting_key, week_key, now_text),
        )
        con.commit()
    return True

async def run_weekly_money_reset():
    if not WEEKLY_RESET_ENABLED:
        return
    for guild in bot.guilds:
        try:
            did_report = await send_weekly_report_if_due(guild)
            if did_report:
                print(f"Weekly report sent for guild {guild.id}")

            did_reset = reset_money_for_guild_if_due(guild.id)
            if did_reset:
                print(f"Weekly dirty money reset completed for guild {guild.id}")
        except Exception as exc:
            print(f"Weekly report/reset failed for guild {guild.id}: {exc}")


@tasks.loop(minutes=30)
async def weekly_money_reset_loop():
    await run_weekly_money_reset()


def add_dirty_money(guild_id: int, member: discord.Member, amount: int):
    now = datetime.utcnow().isoformat(timespec="seconds")
    with db_connect() as con:
        con.execute(
            """
            INSERT INTO money_balances (guild_id, user_id, display_name, dirty_money, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                display_name = excluded.display_name,
                dirty_money = money_balances.dirty_money + excluded.dirty_money,
                updated_at = excluded.updated_at
            """,
            (guild_id, member.id, member.display_name, amount, now),
        )
        balance = con.execute(
            "SELECT dirty_money FROM money_balances WHERE guild_id = ? AND user_id = ?",
            (guild_id, member.id),
        ).fetchone()[0]
        con.commit()
    return balance


class LogDetailsModal(discord.ui.Modal):
    def __init__(self, log_type: str):
        self.log_type = log_type
        title = "Log Event" if log_type == "event" else "Log Party"
        super().__init__(title=title)

        self.event_name = discord.ui.TextInput(label="Name", placeholder="Example: Race Night", max_length=100)
        self.event_date = discord.ui.TextInput(label="Date", placeholder="DD/MM/YYYY, e.g. 10/05/2026", max_length=10)
        self.event_time = discord.ui.TextInput(label="Time", placeholder="HH:MM, 24-hour time", max_length=5)
        self.winning_id = discord.ui.TextInput(label="Winning ID", placeholder="Winner / ID / ticket number", max_length=100)
        self.notes = discord.ui.TextInput(
            label="Notes", placeholder="Optional", required=False, style=discord.TextStyle.paragraph, max_length=500
        )

        self.add_item(self.event_name)
        self.add_item(self.event_date)
        self.add_item(self.event_time)
        self.add_item(self.winning_id)
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            clean_date = validate_date(str(self.event_date.value))
            clean_time = validate_time(str(self.event_time.value))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        details = {
            "log_type": self.log_type,
            "event_name": str(self.event_name.value).strip(),
            "event_date": clean_date,
            "event_time": clean_time,
            "winning_id": str(self.winning_id.value).strip(),
            "notes": str(self.notes.value).strip() or None,
        }
        view = StaffSelectView(details, interaction.user.id)
        await interaction.response.send_message(
            "Choose **1 host** and any assistants, then press **Submit log**.", view=view, ephemeral=True
        )


class HostSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Select the person who hosted", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        self.view.host = self.values[0]
        await interaction.response.defer()


class AssistSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Select people who assisted", min_values=0, max_values=25)

    async def callback(self, interaction: discord.Interaction):
        self.view.assistants = list(self.values)
        await interaction.response.defer()



def build_log_embed_from_row(row) -> discord.Embed:
    (
        record_id,
        log_type,
        event_name,
        event_date,
        event_time,
        winning_id,
        host_user_id,
        host_display_name,
        assisted_user_ids,
        assisted_display_names,
        notes,
        payout,
        comped_items,
        created_by_user_id,
    ) = row

    title = "Event Logged" if log_type == "event" else "Party Logged"
    embed = discord.Embed(title=title, color=discord.Color.green() if not comped_items else discord.Color.blue())
    embed.add_field(name="Record ID", value=f"#{record_id}", inline=True)
    embed.add_field(name="Name", value=event_name, inline=False)
    embed.add_field(name="Date", value=format_english_date(event_date), inline=True)
    embed.add_field(name="Time", value=event_time, inline=True)
    embed.add_field(name="Winning ID", value=winning_id, inline=False)
    embed.add_field(name="Comped items", value="✅ Yes" if comped_items else "☐ No", inline=True)
    embed.add_field(name="Hosted by", value=f"<@{host_user_id}>", inline=False)

    if assisted_user_ids:
        assisted_mentions = ", ".join(f"<@{uid}>" for uid in assisted_user_ids.split(",") if uid)
    else:
        assisted_mentions = "None"
    embed.add_field(name="Assisted by", value=assisted_mentions, inline=False)
    embed.add_field(name="Host payout", value=f"${payout:,} dirty money", inline=True)
    if notes:
        embed.add_field(name="Notes", value=notes[:1024], inline=False)
    embed.set_footer(text=f"Logged by <@{created_by_user_id}>")
    return embed


class CompedItemsButton(discord.ui.Button):
    def __init__(self, record_id: int):
        super().__init__(label="☐ Comped items", style=discord.ButtonStyle.secondary)
        self.record_id = record_id

    async def callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only admins can tick or untick comped items.", ephemeral=True)
            return

        guild_id = interaction.guild_id or 0
        with db_connect() as con:
            row = con.execute("SELECT comped_items FROM logs WHERE guild_id = ? AND id = ?", (guild_id, self.record_id)).fetchone()
            if not row:
                await interaction.response.send_message("I could not find this log in the database.", ephemeral=True)
                return
            new_value = 0 if row[0] else 1
            con.execute("UPDATE logs SET comped_items = ? WHERE guild_id = ? AND id = ?", (new_value, guild_id, self.record_id))
            updated = con.execute(
                """
                SELECT id, log_type, event_name, event_date, event_time, winning_id,
                       host_user_id, host_display_name, assisted_user_ids, assisted_display_names,
                       notes, payout, comped_items, created_by_user_id
                FROM logs WHERE guild_id = ? AND id = ?
                """,
                (guild_id, self.record_id),
            ).fetchone()
            con.commit()

        self.label = "✅ Comped items" if new_value else "☐ Comped items"
        self.style = discord.ButtonStyle.success if new_value else discord.ButtonStyle.secondary
        await interaction.response.edit_message(embed=build_log_embed_from_row(updated), view=self.view)


class CompedItemsView(discord.ui.View):
    def __init__(self, record_id: int):
        super().__init__(timeout=None)
        self.add_item(CompedItemsButton(record_id))


class StaffSelectView(discord.ui.View):
    def __init__(self, details: dict, created_by_user_id: int):
        super().__init__(timeout=300)
        self.details = details
        self.created_by_user_id = created_by_user_id
        self.host: Optional[discord.Member] = None
        self.assistants: list[discord.Member] = []
        self.add_item(HostSelect())
        self.add_item(AssistSelect())

    @discord.ui.button(label="Submit log", style=discord.ButtonStyle.green)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.created_by_user_id:
            await interaction.response.send_message("Only the person who started this log can submit it.", ephemeral=True)
            return
        if not self.host:
            await interaction.response.send_message("Pick a host first.", ephemeral=True)
            return

        guild_id = interaction.guild_id or 0
        # Do not count host as an assistant if selected in both boxes.
        assistants = [m for m in self.assistants if m.id != self.host.id]
        assistant_ids = ",".join(str(m.id) for m in assistants) or None
        assistant_names = ", ".join(m.display_name for m in assistants) or None

        balance = add_dirty_money(guild_id, self.host, HOST_PAYOUT)

        with db_connect() as con:
            cur = con.execute(
                """
                INSERT INTO logs
                (guild_id, log_type, event_name, event_date, event_time, winning_id,
                 host_user_id, host_display_name, assisted_user_ids, assisted_display_names,
                 notes, payout, created_by_user_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    self.details["log_type"],
                    self.details["event_name"],
                    self.details["event_date"],
                    self.details["event_time"],
                    self.details["winning_id"],
                    self.host.id,
                    self.host.display_name,
                    assistant_ids,
                    assistant_names,
                    self.details["notes"],
                    HOST_PAYOUT,
                    interaction.user.id,
                    datetime.utcnow().isoformat(timespec="seconds"),
                ),
            )
            record_id = cur.lastrowid
            con.commit()

        with db_connect() as con:
            row = con.execute(
                """
                SELECT id, log_type, event_name, event_date, event_time, winning_id,
                       host_user_id, host_display_name, assisted_user_ids, assisted_display_names,
                       notes, payout, comped_items, created_by_user_id
                FROM logs WHERE guild_id = ? AND id = ?
                """,
                (guild_id, record_id),
            ).fetchone()

        embed = build_log_embed_from_row(row)
        embed.add_field(name="Host balance", value=f"${balance:,} dirty money", inline=True)
        log_view = CompedItemsView(record_id)
        sent_main_log = await send_to_log_channel(interaction, self.details["log_type"], embed, view=log_view)
        for child in self.children:
            child.disabled = True

        if sent_main_log:
            msg = "Log saved and posted to the event/party log channel. Admins can tick the comped-items box on the log."
        else:
            msg = "Log saved. No configured log channel could be accessed, so it was only saved here."
        await interaction.response.edit_message(content=msg, embed=embed, view=self)


@bot.tree.command(name="event", description="Log an event")
async def event(interaction: discord.Interaction):
    await interaction.response.send_modal(LogDetailsModal("event"))


@bot.tree.command(name="party", description="Log a party")
async def party(interaction: discord.Interaction):
    await interaction.response.send_modal(LogDetailsModal("party"))


@bot.tree.command(name="money", description="Check dirty money balances")
@app_commands.describe(member="Optional member to check. Leave empty for top balances.")
async def money(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    guild_id = interaction.guild_id or 0
    with db_connect() as con:
        if member:
            row = con.execute(
                "SELECT dirty_money FROM money_balances WHERE guild_id = ? AND user_id = ?",
                (guild_id, member.id),
            ).fetchone()
            amount = row[0] if row else 0
            await interaction.response.send_message(
                f"{member.mention} has **${amount:,} dirty money**.", ephemeral=True
            )
            return

        rows = con.execute(
            """
            SELECT display_name, dirty_money
            FROM money_balances
            WHERE guild_id = ?
            ORDER BY dirty_money DESC, display_name ASC
            LIMIT 25
            """,
            (guild_id,),
        ).fetchall()

    if not rows:
        await interaction.response.send_message("No dirty money has been earned yet.", ephemeral=True)
        return
    description = "\n".join(f"**{name}**: ${amount:,}" for name, amount in rows)
    await interaction.response.send_message(
        embed=discord.Embed(title="Dirty Money Balances", description=description, color=discord.Color.gold()),
        ephemeral=True,
    )


@bot.tree.command(name="resetmoney", description="Manually reset all dirty money balances to 0")
@app_commands.checks.has_permissions(administrator=True)
async def resetmoney(interaction: discord.Interaction):
    guild_id = interaction.guild_id or 0
    reset_money_for_guild_if_due(guild_id, force=True)
    await interaction.response.send_message("All dirty money balances have been reset to **$0**.", ephemeral=True)


@bot.tree.command(name="weeklyreport", description="Send the weekly hosted/assisted/dirty money report now")
@app_commands.checks.has_permissions(administrator=True)
async def weeklyreport(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    sent = await send_weekly_report_if_due(interaction.guild, force=True)
    if sent:
        await interaction.response.send_message("Weekly report sent to the configured report channel.", ephemeral=True)
    else:
        await interaction.response.send_message(
            "I could not send the report. Check that WEEKLY_REPORT_CHANNEL_ID is set and that I can send messages there.",
            ephemeral=True,
        )


@bot.tree.command(name="logs", description="List recent event and party logs")
@app_commands.describe(limit="How many records to show, max 20")
async def logs(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 20] = 10):
    guild_id = interaction.guild_id or 0
    with db_connect() as con:
        rows = con.execute(
            """
            SELECT id, log_type, event_name, event_date, event_time, host_display_name, assisted_display_names, winning_id, comped_items
            FROM logs
            WHERE guild_id = ?
            ORDER BY event_date DESC, event_time DESC, id DESC
            LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()

    if not rows:
        await interaction.response.send_message("No logs yet.", ephemeral=True)
        return

    lines = []
    for record_id, log_type, name, date, time, host, assistants, winning_id, comped_items in rows:
        comped_text = "comped: yes" if comped_items else "comped: no"
        lines.append(
            f"`#{record_id}` **{log_type.title()}**: {name} — host: {host} — assisted: {assistants or 'None'} — {format_english_date(date)} {time} — winning ID: `{winning_id}` — {comped_text}"
        )
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.event
async def on_ready():
    init_db()
    if not weekly_money_reset_loop.is_running():
        weekly_money_reset_loop.start()
    await run_weekly_money_reset()
    print(f"Logged in as {bot.user} ({bot.user.id})")


@bot.event
async def setup_hook():
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"Slash commands synced to guild {GUILD_ID}")
    else:
        await bot.tree.sync()
        print("Global slash commands synced. Global sync can take a while to appear.")


async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    message = "Something went wrong while running that command."
    if isinstance(error, app_commands.MissingPermissions):
        message = "You do not have permission to use that command."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
    raise error


bot.tree.on_error = on_tree_error
bot.run(TOKEN)
