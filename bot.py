import asyncio
import html as html_mod
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import discord
from aiohttp import web
from dotenv import load_dotenv
from googletrans import Translator

load_dotenv(override=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", BASE_DIR)
DATA_FILE = os.path.join(DATA_DIR, "invites_data.json")
INVITES_CHANNEL_ID = os.getenv("INVITES_CHANNEL_ID")

TICKET_CATEGORY_ID = os.getenv("TICKET_CATEGORY_ID")
TICKET_CLAIMED_CATEGORY_ID = os.getenv("TICKET_CLAIMED_CATEGORY_ID")
TICKET_STAFF_ROLE_ID = os.getenv("TICKET_STAFF_ROLE_ID")
TICKET_TOPICS_FILE = os.path.join(DATA_DIR, "ticket_topics.json")

BASE_URL = os.getenv("BASE_URL", "http://localhost:8080")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
TRANSCRIPTS_DIR = os.path.join(DATA_DIR, "transcripts")

BUILD_TAG = "health-guild-check-2026-08-02"

TOKEN = os.getenv("DISCORD_TOKEN")
TEAM_ROLE_ID = int(os.getenv("TEAM_ROLE_ID", "0") or 0)

intents = discord.Intents.default()
intents.members = True
intents.invites = True

bot = discord.Bot(intents=intents)

translator = Translator()

invites_data: dict = {"invites": {}, "invite_cache": {}, "members": {}}

TICKET_DATA_FILE = os.path.join(DATA_DIR, "tickets_data.json")
tickets_data: dict = {"counter": 0, "tickets": {}, "settings": {}}
panel_selections: dict[tuple[int, int], str] = {}

BUGS_DATA_FILE = os.path.join(DATA_DIR, "bugs_data.json")
bugs_data: dict = {"counter": 0, "bugs": []}
REPORTS_DATA_FILE = os.path.join(DATA_DIR, "reports_data.json")
reports_data: dict = {"counter": 0, "reports": []}

ANTISPAM_WINDOW = 5.0
ANTISPAM_MAX_MESSAGES = 5
ANTISPAM_DUPES = 3
ANTISPAM_WARN_COOLDOWN = 60.0
spam_state: dict[int, list[tuple[float, str]]] = {}
last_spam_warn: dict[int, float] = {}

LANGUAGES = {
    "de": "Deutsch",
    "en": "English",
    "fr": "Français",
    "es": "Español",
}


def load_invites_data():
    global invites_data
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            invites_data = json.load(f)
    for key in ("invites", "invite_cache", "members"):
        invites_data.setdefault(key, {})


def save_invites_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(invites_data, f, indent=2, ensure_ascii=False)


def get_user_stats(guild_id: int, user_id: int) -> dict:
    return (
        invites_data["invites"]
        .setdefault(str(guild_id), {})
        .setdefault(
            str(user_id),
            {"total": 0, "regular": 0, "left": 0, "fake": 0},
        )
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_fake_account(member) -> bool:
    if member.created_at is None:
        return False
    return (discord.utils.utcnow() - member.created_at).days < 7


async def fetch_invite_cache(guild) -> dict:
    cache = {}
    try:
        invites = await guild.invites()
    except discord.Forbidden:
        return cache
    for invite in invites:
        inviter = invite.inviter.id if invite.inviter else None
        cache[invite.code] = {
            "uses": invite.uses,
            "inviter": str(inviter) if inviter else None,
        }
    return cache


@bot.event
async def on_invite_create(invite):
    guild_id = str(invite.guild.id)
    inviter = invite.inviter.id if invite.inviter else None
    invites_data["invite_cache"].setdefault(guild_id, {})[invite.code] = {
        "uses": invite.uses,
        "inviter": str(inviter) if inviter else None,
    }
    save_invites_data()


@bot.event
async def on_invite_delete(invite):
    guild_id = str(invite.guild.id)
    invites_data["invite_cache"].get(guild_id, {}).pop(invite.code, None)
    save_invites_data()


@bot.event
async def on_member_join(member):
    guild = member.guild
    guild_id = str(guild.id)
    joined_via = None

    try:
        invites = await guild.invites()
    except discord.Forbidden:
        invites = []

    new_uses = {inv.code: inv.uses for inv in invites}
    old_cache = invites_data["invite_cache"].setdefault(guild_id, {})

    for code, uses in new_uses.items():
        old = old_cache.get(code)
        if old is not None and uses > old["uses"]:
            joined_via = old["inviter"]
            old_cache[code]["uses"] = uses
            break

    for code, uses in new_uses.items():
        if code not in old_cache:
            old_cache[code] = {"uses": uses, "inviter": None}

    fake = is_fake_account(member)
    invites_data["members"].setdefault(guild_id, {})[str(member.id)] = {
        "invited_by": joined_via,
        "joined_at": now_iso(),
        "fake": fake,
    }

    if joined_via:
        stats = get_user_stats(guild.id, int(joined_via))
        stats["total"] += 1
        if fake:
            stats["fake"] += 1
        else:
            stats["regular"] += 1

    save_invites_data()


@bot.event
async def on_member_remove(member):
    await handle_owner_left(member)
    guild_id = str(member.guild.id)
    record = invites_data["members"].get(guild_id, {}).pop(str(member.id), None)
    if not record or not record.get("invited_by"):
        return

    stats = get_user_stats(member.guild.id, int(record["invited_by"]))
    if record["fake"]:
        stats["fake"] = max(0, stats["fake"] - 1)
    else:
        stats["regular"] = max(0, stats["regular"] - 1)
    stats["left"] += 1

    save_invites_data()


async def handle_owner_left(member):
    guild_id = str(member.guild.id)
    for channel_id, ticket in tickets_data["tickets"].items():
        if ticket.get("guild_id") != guild_id:
            continue
        if ticket.get("creator") != str(member.id):
            continue
        if ticket.get("status") != "open":
            continue
        channel = bot.get_channel(int(channel_id))
        if not channel:
            continue
        support_role_id = ticket.get("support_role_id")
        support_mention = f"<@&{support_role_id}>" if support_role_id else "@team"
        embed = (
            discord.Embed(
                title="Ticket owner left",
                description=(
                    f"{member.display_name} (the owner of this ticket) has left the server.\n"
                    f"{support_mention} can close and delete this ticket."
                ),
                color=0xE74C3C,
                timestamp=discord.utils.utcnow(),
            )
            .set_footer(text="FightLab.net")
        )
        view = OwnerLeftView()
        msg = await channel.send(embed=embed, view=view)
        bot.add_view(view, message_id=msg.id)
        ticket["owner_left_msg_id"] = str(msg.id)
        save_ticket_data()


class LanguageSelect(discord.ui.Select):
    def __init__(self, content: str, title: str, author_name: str, author_icon: str):
        self.content = content
        self.title = title
        self.author_name = author_name
        self.author_icon = author_icon
        options = [
            discord.SelectOption(label=name, value=code)
            for code, name in LANGUAGES.items()
        ]
        super().__init__(
            placeholder="Ankündigung in einer anderen Sprache ansehen…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        lang = self.values[0]
        await interaction.response.defer(ephemeral=True)
        try:
            translated_title = await asyncio.to_thread(
                translator.translate, self.title, dest=lang
            )
            translated_content = await asyncio.to_thread(
                translator.translate, self.content, dest=lang
            )
        except Exception as exc:
            await interaction.followup.send(
                f"Übersetzung fehlgeschlagen: {exc}", ephemeral=True
            )
            return
        embed = (
            discord.Embed(
                title=translated_title.text,
                description=translated_content.text,
                color=0x00FFAA,
                timestamp=discord.utils.utcnow(),
            )
            .set_author(name=self.author_name, icon_url=self.author_icon)
            .set_footer(text="FightLab.net")
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


class NewsView(discord.ui.View):
    def __init__(self, content: str, title: str, author_name: str, author_icon: str):
        super().__init__(timeout=None)
        self.add_item(LanguageSelect(content, title, author_name, author_icon))


class NewsModal(discord.ui.Modal):
    news = discord.ui.InputText(
        label="Nachricht",
        style=discord.InputTextStyle.paragraph,
        placeholder="Deine Ankündigung…",
        required=True,
        max_length=4000,
    )

    def __init__(self, channel: discord.TextChannel):
        super().__init__(self.news, title="Neue News / Ankündigung")
        self.channel = channel

    async def callback(self, interaction: discord.Interaction):
        print("on_submit: Modal wurde abgesendet")
        content = self.news.value
        await interaction.response.defer(ephemeral=True)
        author_name = f"{interaction.user.display_name} | FIGHTLAB.NET"
        author_icon = interaction.user.display_avatar.url
        embed = (
            discord.Embed(
                title="Ankündigung",
                description=content,
                color=0x00FFAA,
                timestamp=discord.utils.utcnow(),
            )
            .set_author(name=author_name, icon_url=author_icon)
            .set_footer(text="FightLab.net")
        )
        try:
            msg = await self.channel.send(embed=embed)
            await msg.edit(view=NewsView(content, "Ankündigung", author_name, author_icon))
        except discord.Forbidden:
            await interaction.followup.send(
                "Ich habe keine Berechtigung, in diesen Kanal zu schreiben.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"Fehler beim Senden: {exc}", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"News in {self.channel.mention} veröffentlicht.", ephemeral=True
        )
        print("on_submit: fertig")


@bot.slash_command(
    name="tellnews",
    description="News / Ankündigung erstellen",
    default_member_permissions=discord.Permissions(manage_messages=True),
)
async def tellnews(
    ctx: discord.ApplicationContext,
    channel: discord.Option(
        discord.TextChannel,
        description="Kanal, in den die News veröffentlicht werden (Standard: aktueller Kanal)",
    ) = None,
):
    if TEAM_ROLE_ID and not any(role.id == TEAM_ROLE_ID for role in ctx.user.roles):
        await ctx.respond(
            "Du bist nicht berechtigt, News zu senden.", ephemeral=True
        )
        return
    target = channel or ctx.channel
    if not isinstance(target, discord.TextChannel):
        await ctx.respond("Der Zielkanal ist kein Textkanal.", ephemeral=True)
        return
    print("tellnews: Modal wird geöffnet")
    await ctx.send_modal(NewsModal(target))


@bot.slash_command(
    name="invites",
    description="Shows the invite statistics of a member",
)
async def invites(
    ctx: discord.ApplicationContext,
    user: discord.Option(
        discord.Member,
        description="Member whose invites should be shown (default: yourself)",
    ) = None,
):
    target = user or ctx.author
    stats = get_user_stats(ctx.guild.id, target.id)
    embed = (
        discord.Embed(
            title=f"Invites of {target.display_name}",
            color=0x00FFAA,
        )
        .set_thumbnail(url=target.display_avatar.url)
        .add_field(name="Total", value=stats["total"], inline=True)
        .add_field(name="Active", value=stats["regular"], inline=True)
        .add_field(name="Left", value=stats["left"], inline=True)
        .add_field(name="Fake", value=stats["fake"], inline=True)
        .set_footer(text="FightLab.net")
    )
    await ctx.respond(embed=embed)


def load_ticket_data():
    global tickets_data
    if os.path.exists(TICKET_DATA_FILE):
        with open(TICKET_DATA_FILE, "r", encoding="utf-8") as f:
            tickets_data = json.load(f)
    tickets_data.setdefault("counter", 0)
    tickets_data.setdefault("tickets", {})
    tickets_data.setdefault("settings", {})


def save_ticket_data():
    with open(TICKET_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(tickets_data, f, indent=2, ensure_ascii=False)


def load_bugs_data():
    global bugs_data
    if os.path.exists(BUGS_DATA_FILE):
        with open(BUGS_DATA_FILE, "r", encoding="utf-8") as f:
            bugs_data = json.load(f)
    bugs_data.setdefault("counter", 0)
    bugs_data.setdefault("bugs", [])


def save_bugs_data():
    with open(BUGS_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(bugs_data, f, indent=2, ensure_ascii=False)


def load_reports_data():
    global reports_data
    if os.path.exists(REPORTS_DATA_FILE):
        with open(REPORTS_DATA_FILE, "r", encoding="utf-8") as f:
            reports_data = json.load(f)
    reports_data.setdefault("counter", 0)
    reports_data.setdefault("reports", [])


def save_reports_data():
    with open(REPORTS_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(reports_data, f, indent=2, ensure_ascii=False)


def get_ticket(channel_id) -> dict | None:
    return tickets_data["tickets"].get(str(channel_id))


def get_ticket_settings(guild_id):
    settings = tickets_data["settings"].setdefault(str(guild_id), {})
    settings["category_id"] = TICKET_CATEGORY_ID or settings.get("category_id") or ""
    settings["claimed_category_id"] = (
        TICKET_CLAIMED_CATEGORY_ID or settings.get("claimed_category_id") or ""
    )
    settings["support_role_id"] = (
        TICKET_STAFF_ROLE_ID or settings.get("support_role_id") or ""
    )
    return settings


def load_ticket_topics() -> list[str]:
    if os.path.exists(TICKET_TOPICS_FILE):
        try:
            with open(TICKET_TOPICS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                topics = [str(t).strip() for t in data if str(t).strip()]
                if topics:
                    return topics
        except (json.JSONDecodeError, OSError):
            pass
    return ["Bug Report", "Support", "General", "Ban Appeal"]


class TopicSelect(discord.ui.Select):
    def __init__(self, topics: list[str]):
        options = [discord.SelectOption(label=topic, value=topic) for topic in topics]
        super().__init__(
            placeholder="Select a topic…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="ticket_topic_select",
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values:
            self.placeholder = f"Topic: {self.values[0]}"
            panel_selections[(interaction.message.id, interaction.user.id)] = self.values[0]
        await interaction.response.edit_message(view=self.view)


def topic_kind(topic: str) -> str | None:
    normalized = re.sub(r"[^a-z]", "", topic.lower())
    if "banappeal" in normalized:
        return "ban_appeal"
    if "apply" in normalized or "application" in normalized:
        return "apply"
    if "cooperation" in normalized or "coop" in normalized or "partner" in normalized:
        return "cooperation"
    return None


class BaseAppModal(discord.ui.Modal):
    def __init__(self, *children, title: str, guild, creator, topic: str):
        super().__init__(*children, title=title)
        self.guild = guild
        self.creator = creator
        self.topic = topic

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ok, result = await create_ticket(
            interaction, self.guild, self.creator, self.topic, details=self.details()
        )
        if not ok:
            await interaction.followup.send(result, ephemeral=True)
            return
        await interaction.followup.send(
            f"Your ticket has been created: {result.mention}", ephemeral=True
        )

    def details(self) -> dict:
        raise NotImplementedError


def parse_ban_date(value: str):
    text = value.strip()
    if not text:
        raise ValueError("empty")
    if re.fullmatch(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", text):
        year, month, day = (int(p) for p in re.split(r"[-/.]", text))
    elif re.fullmatch(r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}", text):
        day, month, year = (int(p) for p in re.split(r"[-/.]", text))
        if year < 100:
            year += 2000
    else:
        raise ValueError(text)
    return datetime(year, month, day)


class BanAppealModal(BaseAppModal):
    ingame = discord.ui.InputText(label="In-Game Name", required=True, max_length=100)
    age = discord.ui.InputText(label="Age", required=True, max_length=10)
    ban_date = discord.ui.InputText(
        label="Ban Date (DD-MM-YYYY)", required=True, max_length=10
    )
    reason = discord.ui.InputText(
        label="Reason why you should be unbanned",
        style=discord.InputTextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    def __init__(self, guild, creator, topic: str):
        super().__init__(
            self.ingame,
            self.age,
            self.ban_date,
            self.reason,
            title="Ban Appeal",
            guild=guild,
            creator=creator,
            topic=topic,
        )

    async def callback(self, interaction: discord.Interaction):
        raw = self.ban_date.value or ""
        try:
            parse_ban_date(raw)
        except ValueError:
            print("BanAppealModal: invalid ban date:", repr(raw))
            await interaction.response.send_message(
                "Please enter the ban date in DD-MM-YYYY format.", ephemeral=True
            )
            return
        await super().callback(interaction)

    def details(self) -> dict:
        return {
            "In-Game Name": self.ingame.value,
            "Age": self.age.value,
            "Ban Date": self.ban_date.value,
            "Unban Reason": self.reason.value,
        }


class ApplyModal(BaseAppModal):
    ingame = discord.ui.InputText(label="In-Game Name", required=True, max_length=100)
    age = discord.ui.InputText(label="Age", required=True, max_length=10)
    language = discord.ui.InputText(label="Language", required=True, max_length=100)
    experience = discord.ui.InputText(
        label="Support Experience (incl. duration)",
        style=discord.InputTextStyle.paragraph,
        required=True,
        max_length=1000,
    )
    why_us = discord.ui.InputText(
        label="Why should we take you",
        style=discord.InputTextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    def __init__(self, guild, creator, topic: str):
        super().__init__(
            self.ingame,
            self.age,
            self.language,
            self.experience,
            self.why_us,
            title="Application",
            guild=guild,
            creator=creator,
            topic=topic,
        )

    def details(self) -> dict:
        return {
            "In-Game Name": self.ingame.value,
            "Age": self.age.value,
            "Language": self.language.value,
            "Support Experience": self.experience.value,
            "Why we should take you": self.why_us.value,
        }


class CooperationModal(BaseAppModal):
    org_name = discord.ui.InputText(
        label="Name of the Organization", required=True, max_length=100
    )
    leader_age = discord.ui.InputText(
        label="Age of the Leader/Representative", required=True, max_length=10
    )
    content_type = discord.ui.InputText(
        label="Type of Content the Organization brings",
        style=discord.InputTextStyle.paragraph,
        required=True,
        max_length=1000,
    )
    socials = discord.ui.InputText(
        label="Website / Social Media",
        required=False,
        max_length=500,
    )

    def __init__(self, guild, creator, topic: str):
        super().__init__(
            self.org_name,
            self.leader_age,
            self.content_type,
            self.socials,
            title="Cooperation",
            guild=guild,
            creator=creator,
            topic=topic,
        )

    def details(self) -> dict:
        return {
            "Organization": self.org_name.value,
            "Age of Leader/Representative": self.leader_age.value,
            "Type of Content": self.content_type.value,
            "Website / Social Media": self.socials.value or "—",
        }


def get_topic_modal(guild, creator, topic: str):
    kind = topic_kind(topic)
    if kind == "ban_appeal":
        return BanAppealModal(guild, creator, topic)
    if kind == "apply":
        return ApplyModal(guild, creator, topic)
    if kind == "cooperation":
        return CooperationModal(guild, creator, topic)
    return None


def is_staff(member, guild_id) -> bool:
    if member.guild_permissions.administrator:
        return True
    role_id = get_ticket_settings(guild_id).get("support_role_id")
    if not role_id:
        return False
    return any(r.id == int(role_id) for r in member.roles)


def sanitize_channel_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9-]", "", name.replace(" ", "-")).lower()
    return cleaned[:32] or "user"


async def strip_view(channel, message_id):
    if not message_id:
        return
    try:
        msg = await channel.fetch_message(int(message_id))
        await msg.edit(view=None)
    except Exception:
        pass


async def create_ticket(interaction, guild, creator, topic: str, details: dict | None = None):
    settings = get_ticket_settings(guild.id)
    category = guild.get_channel(int(settings["category_id"])) if settings.get(
        "category_id"
    ) else None
    support_role = guild.get_role(int(settings["support_role_id"])) if settings.get(
        "support_role_id"
    ) else None
    if not category or not support_role:
        return False, "The ticket system is not configured correctly. Please contact an admin."
    for ticket in tickets_data["tickets"].values():
        if (
            ticket.get("guild_id") == str(guild.id)
            and str(creator.id) == ticket.get("creator")
            and ticket.get("status") == "open"
        ):
            return False, "You already have an open ticket."
    number = tickets_data["counter"] + 1
    tickets_data["counter"] = number
    channel_name = f"ticket-{number}-{sanitize_channel_name(creator.name)}"
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        creator: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
        support_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        ),
    }
    channel = await category.create_text_channel(
        channel_name,
        overwrites=overwrites,
        topic=f"{topic} · Ticket #{number}",
    )
    embed = (
        discord.Embed(
            title=f"Ticket #{number}",
            description=f"{creator.mention} opened a ticket. Our team will take care of your request as soon as possible.",
            color=0x00FFAA,
            timestamp=discord.utils.utcnow(),
        )
        .set_footer(text="FightLab.net")
    )
    view = TicketChannelView()
    msg = await channel.send(embed=embed, view=view)
    bot.add_view(view, message_id=msg.id)
    if details:
        details_embed = discord.Embed(title=topic, color=0x00FFAA)
        for name, value in details.items():
            details_embed.add_field(name=name, value=value, inline=False)
        await channel.send(embed=details_embed)
    tickets_data["tickets"][str(channel.id)] = {
        "number": number,
        "creator": str(creator.id),
        "guild_id": str(guild.id),
        "created_at": now_iso(),
        "last_user_activity": now_iso(),
        "claimed_by": None,
        "status": "open",
        "warnings": 0,
        "support_role_id": str(support_role.id),
        "ticket_msg_id": str(msg.id),
        "close_msg_id": None,
        "topic": topic,
        "details": details,
    }
    save_ticket_data()
    return True, channel


def format_transcript(guild_name: str, channel_name: str, closed_at: str, messages) -> str:
    msg_html = []
    for m in messages:
        author = m.author
        name = html_mod.escape(author.display_name)
        avatar = html_mod.escape(author.display_avatar.url)
        time_str = m.created_at.strftime("%Y-%m-%d %H:%M")
        is_bot = author.bot
        parts = [f'<div class="message">']
        parts.append(
            f'<img class="avatar" src="{avatar}" alt="" loading="lazy">'
            f'<div class="body">'
            f'<div class="head"><span class="name">{name}'
            + (' <span class="badge">BOT</span>' if is_bot else '')
            + f'</span><span class="time">{time_str}</span></div>'
        )
        if m.content:
            content = html_mod.escape(m.content).replace("\n", "<br>")
            parts.append(f'<div class="content">{content}</div>')
        for embed in m.embeds:
            e_title = html_mod.escape(embed.title or "")
            e_desc = html_mod.escape(embed.description or "").replace("\n", "<br>")
            e_color = embed.color.value if embed.color else 0x5865F2
            e_url = html_mod.escape(embed.url or "")
            if e_title or e_desc:
                parts.append(
                    '<div class="embed" style="border-left-color: #{:06x}">'.format(e_color)
                    + (f'<a class="embed-title" href="{e_url}" target="_blank">{e_title}</a>' if e_title else "")
                    + (f'<div class="embed-desc">{e_desc}</div>' if e_desc else "")
                    + "</div>"
                )
        for a in m.attachments:
            a_url = html_mod.escape(a.url)
            if a.content_type and a.content_type.startswith("image/"):
                parts.append(
                    f'<a href="{a_url}" target="_blank">'
                    f'<img class="attach-img" src="{a_url}" alt="{html_mod.escape(a.filename)}" loading="lazy"></a>'
                )
            else:
                parts.append(
                    f'<a class="attach-file" href="{a_url}" target="_blank">📎 {html_mod.escape(a.filename)}</a>'
                )
        for s in m.stickers:
            parts.append(
                f'<div class="sticker">{html_mod.escape(s.name)} (sticker)</div>'
            )
        parts.append("</div></div>")
        msg_html.append("".join(parts))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ticket Transcript</title>
<style>
  :root {{
    --bg: #1e1f22; --panel: #2b2d31; --text: #dbdee1; --dim: #949ba4;
    --accent: #5865f2; --border: #3f4147; --green: #57f287;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    background: var(--bg); color: var(--text);
    font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    min-height: 100vh;
  }}
  .wrap {{ max-width: 860px; margin: 0 auto; padding: 32px 16px 64px; }}
  header {{
    background: linear-gradient(135deg, var(--accent), #7c6cf0);
    border-radius: 16px; padding: 32px; margin-bottom: 24px;
    box-shadow: 0 8px 24px rgba(0,0,0,.35);
  }}
  header .logo {{ font-size: 14px; letter-spacing: 2px; text-transform: uppercase; opacity: .85; }}
  header h1 {{ font-size: 28px; margin-top: 6px; }}
  header .meta {{ margin-top: 10px; font-size: 14px; opacity: .9; }}
  header .meta span {{ display: inline-block; margin-right: 16px; }}
  .message {{
    display: flex; gap: 14px; padding: 14px 16px; border-radius: 10px;
    border-bottom: 1px solid var(--border);
  }}
  .message:hover {{ background: var(--panel); }}
  .avatar {{
    width: 42px; height: 42px; border-radius: 50%; flex-shrink: 0;
    background: var(--panel); object-fit: cover;
  }}
  .body {{ flex: 1; min-width: 0; }}
  .head .name {{ font-weight: 600; color: var(--green); }}
  .head .time {{ color: var(--dim); font-size: 12px; margin-left: 8px; }}
  .badge {{
    display: inline-block; background: var(--accent); color: #fff;
    font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px;
    margin-left: 6px; vertical-align: middle;
  }}
  .content {{ margin-top: 4px; line-height: 1.5; word-break: break-word; }}
  .embed {{
    margin-top: 8px; border-left: 4px solid var(--accent);
    background: var(--panel); border-radius: 6px; padding: 10px 12px;
  }}
  .embed-title {{ color: var(--accent); text-decoration: none; font-weight: 600; }}
  .embed-desc {{ margin-top: 4px; color: var(--text); }}
  .attach-img {{
    max-width: 100%; max-height: 360px; border-radius: 8px;
    margin-top: 8px; display: block;
  }}
  .attach-file {{
    display: inline-block; margin-top: 8px; padding: 8px 12px;
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; color: var(--text); text-decoration: none;
  }}
  .sticker {{ margin-top: 8px; color: var(--dim); }}
  footer {{ text-align: center; color: var(--dim); font-size: 13px; margin-top: 40px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">FightLab.net · Transcript</div>
    <h1>{html_mod.escape(channel_name)}</h1>
    <div class="meta">
      <span>🏰 {html_mod.escape(guild_name)}</span>
      <span>🕓 Closed {html_mod.escape(closed_at)}</span>
      <span>💬 {len(messages)} messages</span>
    </div>
  </header>
  {''.join(msg_html)}
  <footer>Powered by FightLab.net · This transcript is private.</footer>
</div>
</body>
</html>"""


async def create_transcript(channel) -> str | None:
    messages = []
    async for msg in channel.history(limit=None, oldest_first=True):
        messages.append(msg)
    if not messages:
        return None
    token = secrets.token_urlsafe(16)
    os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
    html_content = await asyncio.to_thread(
        format_transcript, channel.guild.name, channel.name, now_iso(), messages
    )
    with open(
        os.path.join(TRANSCRIPTS_DIR, f"{token}.html"), "w", encoding="utf-8"
    ) as f:
        f.write(html_content)
    return token


async def dm_transcript(member, url: str):
    if not member:
        return
    try:
        embed = (
            discord.Embed(
                title="Your ticket transcript",
                description=f"The transcript of your ticket is available here:\n{url}",
                color=0x00FFAA,
            )
            .set_footer(text="FightLab.net")
        )
        await member.send(embed=embed)
    except discord.HTTPException as exc:
        print("Could not DM transcript:", exc)


async def handle_transcript(request):
    token = request.match_info["token"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise web.HTTPNotFound()
    path = os.path.join(TRANSCRIPTS_DIR, f"{token}.html")
    if not os.path.exists(path):
        raise web.HTTPNotFound()
    with open(path, "r", encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html")


async def handle_health(request):
    settings_summary = {}
    for guild_id, settings in tickets_data.get("settings", {}).items():
        settings_summary[guild_id] = {
            "category_id": settings.get("category_id") or None,
            "claimed_category_id": settings.get("claimed_category_id") or None,
            "support_role_id": settings.get("support_role_id") or None,
        }
    guilds = []
    for guild in bot.guilds:
        entry = {"id": str(guild.id), "name": guild.name}
        if TICKET_CATEGORY_ID:
            entry["category_found"] = guild.get_channel(int(TICKET_CATEGORY_ID)) is not None
        if TICKET_CLAIMED_CATEGORY_ID:
            entry["claimed_category_found"] = (
                guild.get_channel(int(TICKET_CLAIMED_CATEGORY_ID)) is not None
            )
        if TICKET_STAFF_ROLE_ID:
            entry["staff_role_found"] = guild.get_role(int(TICKET_STAFF_ROLE_ID)) is not None
        guilds.append(entry)
    return web.json_response(
        {
            "status": "ok",
            "build": BUILD_TAG,
            "env": {
                "TICKET_CATEGORY_ID": TICKET_CATEGORY_ID or None,
                "TICKET_CLAIMED_CATEGORY_ID": TICKET_CLAIMED_CATEGORY_ID or None,
                "TICKET_STAFF_ROLE_ID": TICKET_STAFF_ROLE_ID or None,
                "TEAM_ROLE_ID": TEAM_ROLE_ID or None,
                "BASE_URL": BASE_URL,
                "WEB_PORT": WEB_PORT,
                "DATA_DIR": DATA_DIR,
            },
            "stored_settings": settings_summary,
            "guilds": guilds,
        }
    )


async def web_server():
    app = web.Application()
    app.router.add_get("/transcript/{token}", handle_transcript)
    app.router.add_get("/api/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    print(f"Transcript web server running on http://{WEB_HOST}:{WEB_PORT}")


async def close_ticket(channel):
    ticket = get_ticket(channel.id)
    if not ticket or ticket.get("status") == "closed":
        return False
    ticket["status"] = "closed"
    save_ticket_data()
    creator = channel.guild.get_member(int(ticket["creator"]))
    support_role = channel.guild.get_role(
        int(ticket["support_role_id"])
    ) if ticket.get("support_role_id") else None
    overwrites = channel.overwrites or {}
    if creator:
        overwrites[creator] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=False,
            read_message_history=True,
        )
    if support_role:
        overwrites[support_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        )
    await channel.edit(overwrites=overwrites)
    await strip_view(channel, ticket.get("ticket_msg_id"))
    await strip_view(channel, ticket.get("close_msg_id"))
    ticket["close_msg_id"] = None
    save_ticket_data()

    transcript_url = None
    try:
        token = await create_transcript(channel)
        if token:
            transcript_url = f"{BASE_URL}/transcript/{token}"
            ticket["transcript_token"] = token
            save_ticket_data()
    except Exception as exc:
        print("Transcript error:", exc)

    description = "This ticket has been closed."
    if transcript_url:
        description += f"\n[Open transcript]({transcript_url})"
    embed = (
        discord.Embed(
            title="Ticket closed",
            description=description,
            color=0xE74C3C,
            timestamp=discord.utils.utcnow(),
        )
        .set_footer(text="FightLab.net")
    )
    view = ClosedTicketView()
    msg = await channel.send(embed=embed, view=view)
    bot.add_view(view, message_id=msg.id)
    ticket["closed_msg_id"] = str(msg.id)
    save_ticket_data()
    if transcript_url:
        await dm_transcript(creator, transcript_url)
    return True


async def delete_ticket(channel):
    ticket = tickets_data["tickets"].pop(str(channel.id), None)
    await strip_view(channel, ticket.get("close_msg_id") if ticket else None)
    save_ticket_data()
    await channel.delete(reason="Ticket deleted")


async def send_close_request(channel, ticket, staff):
    owner = channel.guild.get_member(int(ticket["creator"]))
    embed = discord.Embed(
        title="Close Ticket",
        description=(
            f"{staff.display_name} would like to close this ticket.\n"
            f"Are you okay with that? {owner.mention if owner else '@ticket owner'}"
        ),
        color=0xE74C3C,
    )
    view = TicketCloseConfirmView()
    msg = await channel.send(embed=embed, view=view)
    bot.add_view(view, message_id=msg.id)
    ticket["close_msg_id"] = str(msg.id)
    ticket["close_request"] = "owner"
    save_ticket_data()


async def send_staff_close_request(channel, ticket, requester):
    embed = discord.Embed(
        title="Close Ticket",
        description=(
            f"{requester.display_name} would like to close this ticket.\n"
            "A team member must confirm this request."
        ),
        color=0xE74C3C,
    )
    view = TicketStaffCloseConfirmView()
    msg = await channel.send(embed=embed, view=view)
    bot.add_view(view, message_id=msg.id)
    ticket["close_msg_id"] = str(msg.id)
    ticket["close_request"] = "staff"
    save_ticket_data()


class TicketPanelView(discord.ui.View):
    def __init__(self, topics: list[str]):
        super().__init__(timeout=None)
        self.add_item(TopicSelect(topics))
        button = discord.ui.Button(
            label="Create Ticket",
            style=discord.ButtonStyle.success,
            custom_id="ticket_panel_create",
            emoji="🎫",
        )
        button.callback = self.create_ticket_button
        self.add_item(button)

    async def create_ticket_button(self, interaction):
        topic = panel_selections.get((interaction.message.id, interaction.user.id))
        if not topic:
            await interaction.response.send_message(
                "Please select a topic first. No ticket has been created.",
                ephemeral=True,
            )
            return
        modal = get_topic_modal(interaction.guild, interaction.user, topic)
        if modal is not None:
            await interaction.response.send_modal(modal)
            return
        await interaction.response.defer(ephemeral=True)
        ok, result = await create_ticket(
            interaction, interaction.guild, interaction.user, topic
        )
        if not ok:
            await interaction.followup.send(result, ephemeral=True)
            return
        await interaction.followup.send(
            f"Your ticket has been created: {result.mention}", ephemeral=True
        )


class TicketChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_close_channel",
        emoji="🔒",
    )
    async def close_button(self, button, interaction):
        ticket = get_ticket(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message(
                "This is not a ticket channel.", ephemeral=True
            )
            return
        if ticket.get("status") == "closed":
            await interaction.response.send_message(
                "The ticket is already closed.", ephemeral=True
            )
            return
        if interaction.user.id == int(ticket["creator"]):
            await send_staff_close_request(interaction.channel, ticket, interaction.user)
            await interaction.response.send_message(
                "A close request has been sent to the team. A team member must confirm the closure.",
                ephemeral=True,
            )
        elif is_staff(interaction.user, interaction.guild_id):
            await send_close_request(interaction.channel, ticket, interaction.user)
            await interaction.response.send_message(
                "A close request has been sent to the ticket owner.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Only the ticket creator or team members can close this ticket.",
                ephemeral=True,
            )


class TicketCloseConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_confirm_close",
    )
    async def confirm_button(self, button, interaction):
        ticket = get_ticket(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message(
                "This is not a ticket channel.", ephemeral=True
            )
            return
        if ticket.get("status") == "closed":
            await interaction.response.send_message(
                "The ticket is already closed.", ephemeral=True
            )
            return
        if str(interaction.user.id) != ticket["creator"] and not is_staff(
            interaction.user, interaction.guild_id
        ):
            await interaction.response.send_message(
                "Only the ticket creator can confirm the closure.",
                ephemeral=True,
            )
            return
        await close_ticket(interaction.channel)
        await interaction.response.send_message(
            "Ticket closed.", ephemeral=True
        )


class TicketStaffCloseConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Confirm Close",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_staff_confirm_close",
    )
    async def confirm_button(self, button, interaction):
        ticket = get_ticket(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message(
                "This is not a ticket channel.", ephemeral=True
            )
            return
        if ticket.get("status") == "closed":
            await interaction.response.send_message(
                "The ticket is already closed.", ephemeral=True
            )
            return
        if not is_staff(interaction.user, interaction.guild_id):
            await interaction.response.send_message(
                "Only team members can confirm this request.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await close_ticket(interaction.channel)
        await interaction.followup.send("Ticket closed.", ephemeral=True)

    @discord.ui.button(
        label="Deny Close",
        style=discord.ButtonStyle.secondary,
        custom_id="ticket_staff_deny_close",
    )
    async def deny_button(self, button, interaction):
        ticket = get_ticket(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message(
                "This is not a ticket channel.", ephemeral=True
            )
            return
        if not is_staff(interaction.user, interaction.guild_id):
            await interaction.response.send_message(
                "Only team members can deny this request.", ephemeral=True
            )
            return
        await strip_view(interaction.channel, ticket.get("close_msg_id"))
        ticket["close_msg_id"] = None
        save_ticket_data()
        embed = discord.Embed(
            title="Close request denied",
            description="The close request was denied by the team.",
            color=0xF1C40F,
        )
        await interaction.response.send_message(embed=embed)


class OwnerLeftView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close & Delete Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_owner_left_close_delete",
    )
    async def close_delete_button(self, button, interaction):
        ticket = get_ticket(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message(
                "This is not a ticket channel.", ephemeral=True
            )
            return
        if ticket.get("status") != "open":
            await interaction.response.send_message(
                "The ticket is already closed.", ephemeral=True
            )
            return
        if not is_staff(interaction.user, interaction.guild_id):
            await interaction.response.send_message(
                "Only team members can close and delete this ticket.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "The ticket is being closed and deleted…", ephemeral=True
        )
        await delete_ticket(interaction.channel)


class TicketDeleteConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Delete Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_confirm_delete",
    )
    async def confirm_button(self, button, interaction):
        ticket = get_ticket(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message(
                "This is not a ticket channel.", ephemeral=True
            )
            return
        if str(interaction.user.id) != ticket["creator"] and not is_staff(
            interaction.user, interaction.guild_id
        ):
            await interaction.response.send_message(
                "Only the ticket creator can confirm the deletion.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "The ticket is being deleted…", ephemeral=True
        )
        await delete_ticket(interaction.channel)


class ClosedTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Delete Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_closed_delete",
        emoji="🗑️",
    )
    async def delete_button(self, button, interaction):
        ticket = get_ticket(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message(
                "This is not a ticket channel.", ephemeral=True
            )
            return
        if str(interaction.user.id) != ticket.get("creator") and not is_staff(
            interaction.user, interaction.guild_id
        ):
            await interaction.response.send_message(
                "Only the ticket creator or team members can delete this ticket.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "The ticket is being deleted…", ephemeral=True
        )
        await delete_ticket(interaction.channel)


ticket = bot.create_group("ticket", "Ticket System")


@ticket.command(name="create", description="Opens a new ticket")
async def ticket_create(
    ctx: discord.ApplicationContext,
    topic: discord.Option(
        str,
        description="Topic of the ticket",
        required=True,
        choices=[discord.OptionChoice(name=t, value=t) for t in load_ticket_topics()],
    ),
):
    modal = get_topic_modal(ctx.guild, ctx.author, topic)
    if modal is not None:
        await ctx.send_modal(modal)
        return
    await ctx.defer(ephemeral=True)
    ok, result = await create_ticket(ctx, ctx.guild, ctx.author, topic)
    if not ok:
        await ctx.followup.send(result, ephemeral=True)
        return
    await ctx.followup.send(f"Ticket created: {result.mention}", ephemeral=True)


@ticket.command(name="close", description="Closes the ticket")
async def ticket_close(ctx: discord.ApplicationContext):
    await ctx.defer(ephemeral=True)
    ticket_record = get_ticket(ctx.channel_id)
    if not ticket_record:
        await ctx.followup.send("This is not a ticket channel.", ephemeral=True)
        return
    if ticket_record.get("status") == "closed":
        await ctx.followup.send("The ticket is already closed.", ephemeral=True)
        return
    if ctx.author.id == int(ticket_record["creator"]):
        await send_staff_close_request(ctx.channel, ticket_record, ctx.author)
        await ctx.followup.send(
            "A close request has been sent to the team. A team member must confirm the closure.",
            ephemeral=True,
        )
    elif is_staff(ctx.author, ctx.guild.id):
        await send_close_request(ctx.channel, ticket_record, ctx.author)
        await ctx.followup.send(
            "A close request has been sent to the ticket owner.", ephemeral=True
        )
    else:
        await ctx.followup.send("You don't have permission.", ephemeral=True)


@ticket.command(name="delete", description="Deletes the ticket (with confirmation)")
async def ticket_delete(ctx: discord.ApplicationContext):
    await ctx.defer(ephemeral=True)
    ticket_record = get_ticket(ctx.channel_id)
    if not ticket_record:
        await ctx.followup.send("This is not a ticket channel.", ephemeral=True)
        return
    if ctx.author.id != int(ticket_record["creator"]):
        await ctx.followup.send(
            "Only the ticket creator can request a deletion. Admins can use /ticket admindelete.",
            ephemeral=True,
        )
        return
    embed = discord.Embed(
        title="Delete Ticket",
        description="Are you sure you want to delete this ticket?",
        color=0xE74C3C,
    )
    view = TicketDeleteConfirmView()
    msg = await ctx.channel.send(embed=embed, view=view)
    bot.add_view(view, message_id=msg.id)
    await ctx.followup.send("Deletion confirmation has been sent.", ephemeral=True)


@ticket.command(name="claim", description="Claims the ticket (Team)")
async def ticket_claim(ctx: discord.ApplicationContext):
    await ctx.defer(ephemeral=True)
    ticket_record = get_ticket(ctx.channel_id)
    if not ticket_record:
        await ctx.followup.send("This is not a ticket channel.", ephemeral=True)
        return
    if not is_staff(ctx.author, ctx.guild.id):
        await ctx.followup.send("You don't have permission.", ephemeral=True)
        return
    ticket_record["claimed_by"] = str(ctx.author.id)
    save_ticket_data()
    settings = get_ticket_settings(ctx.guild.id)
    claimed_category = ctx.guild.get_channel(int(settings["claimed_category_id"])) if settings.get(
        "claimed_category_id"
    ) else None
    if claimed_category and ctx.channel.category_id != claimed_category.id:
        await ctx.channel.edit(category=claimed_category)
    embed = discord.Embed(
        description=f"{ctx.author.mention} has claimed this ticket.",
        color=0x3498DB,
        timestamp=discord.utils.utcnow(),
    )
    await ctx.channel.send(embed=embed)
    await ctx.followup.send("Ticket claimed.", ephemeral=True)


@ticket.command(
    name="admindelete", description="Deletes the ticket without confirmation (Admin)"
)
async def ticket_admindelete(ctx: discord.ApplicationContext):
    await ctx.defer(ephemeral=True)
    ticket_record = get_ticket(ctx.channel_id)
    if not ticket_record:
        await ctx.followup.send("This is not a ticket channel.", ephemeral=True)
        return
    if not is_staff(ctx.author, ctx.guild.id):
        await ctx.followup.send("You don't have permission.", ephemeral=True)
        return
    await ctx.followup.send("The ticket is being deleted…", ephemeral=True)
    await delete_ticket(ctx.channel)


@ticket.command(
    name="getlogs",
    description="Requests the logs of the ticket creator (Ban Appeal)",
)
async def ticket_getlogs(ctx: discord.ApplicationContext):
    await ctx.defer(ephemeral=True)
    ticket_record = get_ticket(ctx.channel_id)
    if not ticket_record:
        await ctx.followup.send("This is not a ticket channel.", ephemeral=True)
        return
    if topic_kind(ticket_record.get("topic", "")) != "ban_appeal":
        await ctx.followup.send(
            "This command is only available in Ban Appeal tickets.", ephemeral=True
        )
        return
    if str(ctx.author.id) != ticket_record["creator"] and not is_staff(
        ctx.author, ctx.guild.id
    ):
        await ctx.followup.send("You don't have permission.", ephemeral=True)
        return
    expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    embed = discord.Embed(
        title="Logs Requested",
        description=(
            "Please reply to this message with your logs within 10 minutes.\n"
            "Use Discord's reply function so your logs can be linked to this request."
        ),
        color=0x3498DB,
        timestamp=discord.utils.utcnow(),
    )
    msg = await ctx.channel.send(embed=embed)
    ticket_record["logs_request_msg_id"] = str(msg.id)
    ticket_record["logs_request_expires"] = expires
    ticket_record["logs"] = None
    save_ticket_data()
    await ctx.followup.send(
        "Logs requested. Waiting for the reply…", ephemeral=True
    )


@bot.slash_command(
    name="ticketpanel",
    description="Creates a ticket panel (Admin)",
    default_member_permissions=discord.Permissions(manage_channels=True),
)
async def ticketpanel(
    ctx: discord.ApplicationContext,
    channel: discord.Option(
        discord.TextChannel,
        description="Channel for the panel (default: current channel)",
    ) = None,
):
    if not (
        ctx.author.guild_permissions.manage_channels
        or ctx.author.guild_permissions.administrator
    ):
        await ctx.respond("You don't have permission.", ephemeral=True)
        return
    settings = get_ticket_settings(ctx.guild.id)
    if not settings.get("category_id") or not settings.get("support_role_id"):
        await ctx.respond(
            "Please configure TICKET_CATEGORY_ID and TICKET_STAFF_ROLE_ID in the .env file first.",
            ephemeral=True,
        )
        return
    topics = load_ticket_topics()
    embed = (
        discord.Embed(
            title="Support Ticket",
            description="Select a topic and press the button to create a ticket. Our team will help you as soon as possible.",
            color=0x00FFAA,
        )
        .set_footer(text="FightLab.net")
    )
    view = TicketPanelView(topics)
    target = channel or ctx.channel
    await ctx.defer()
    msg = await ctx.followup.send(embed=embed, view=view)
    bot.add_view(view, message_id=msg.id)
    settings["panel_message_id"] = str(msg.id)
    settings["panel_channel_id"] = str(target.id)
    save_ticket_data()


@bot.slash_command(
    name="bug",
    description="Reports a bug",
)
async def bug(
    ctx: discord.ApplicationContext,
    bug: discord.Option(
        str,
        description="Describe the bug",
        required=True,
        max_length=1000,
    ),
):
    load_bugs_data()
    bugs_data["counter"] += 1
    bug_id = bugs_data["counter"]
    bugs_data["bugs"].append(
        {
            "id": bug_id,
            "author_id": str(ctx.author.id),
            "author_name": ctx.author.display_name,
            "content": bug,
            "created_at": now_iso(),
            "status": "open",
        }
    )
    save_bugs_data()
    embed = (
        discord.Embed(
            title=f"Bug reported (#{bug_id})",
            description=bug,
            color=0xE67E22,
        )
        .set_footer(text="FightLab.net")
    )
    await ctx.respond(embed=embed, ephemeral=True)


@bot.slash_command(
    name="showbugs",
    description="Lists all reported bugs (Admin)",
    default_member_permissions=discord.Permissions(manage_messages=True),
)
async def showbugs(
    ctx: discord.ApplicationContext,
    status: discord.Option(
        str,
        description="Filter by status",
        choices=["open", "resolved", "all"],
    ) = "open",
):
    load_bugs_data()
    bugs = [
        b
        for b in bugs_data["bugs"]
        if status == "all" or b.get("status", "open") == status
    ]
    bugs = bugs[-15:][::-1]
    if not bugs:
        await ctx.respond("No bugs found.", ephemeral=True)
        return
    embed = discord.Embed(title=f"Bugs ({status})", color=0xE67E22)
    for b in bugs:
        embed.add_field(
            name=f"#{b['id']} · {b['author_name']} · {b['created_at'][:10]} · {b.get('status', 'open')}",
            value=b["content"][:150],
            inline=False,
        )
    await ctx.respond(embed=embed)


@bot.slash_command(
    name="bugresolve",
    description="Marks a bug as resolved (Admin)",
    default_member_permissions=discord.Permissions(manage_messages=True),
)
async def bugresolve(
    ctx: discord.ApplicationContext,
    bug_id: discord.Option(int, description="ID of the bug", required=True),
):
    load_bugs_data()
    for b in bugs_data["bugs"]:
        if b["id"] == bug_id:
            b["status"] = "resolved"
            save_bugs_data()
            await ctx.respond(f"Bug #{bug_id} marked as resolved.", ephemeral=True)
            return
    await ctx.respond(f"Bug #{bug_id} not found.", ephemeral=True)


@bot.slash_command(
    name="report",
    description="Reports a player",
)
async def report(
    ctx: discord.ApplicationContext,
    player: discord.Option(discord.Member, description="Player to report", required=True),
    reason: discord.Option(
        str,
        description="Reason for the report",
        required=True,
        max_length=1000,
    ),
):
    load_reports_data()
    reports_data["counter"] += 1
    report_id = reports_data["counter"]
    reports_data["reports"].append(
        {
            "id": report_id,
            "reporter_id": str(ctx.author.id),
            "reporter_name": ctx.author.display_name,
            "reported_id": str(player.id),
            "reported_name": player.display_name,
            "reason": reason,
            "created_at": now_iso(),
            "status": "open",
        }
    )
    save_reports_data()
    embed = (
        discord.Embed(
            title=f"Report submitted (#{report_id})",
            description=f"{player.mention} has been reported.",
            color=0xE74C3C,
        )
        .set_footer(text="FightLab.net")
    )
    await ctx.respond(embed=embed, ephemeral=True)


@bot.slash_command(
    name="showreports",
    description="Lists all reports (Admin)",
    default_member_permissions=discord.Permissions(manage_messages=True),
)
async def showreports(
    ctx: discord.ApplicationContext,
    status: discord.Option(
        str,
        description="Filter by status",
        choices=["open", "resolved", "all"],
    ) = "open",
):
    load_reports_data()
    reports = [
        r
        for r in reports_data["reports"]
        if status == "all" or r.get("status", "open") == status
    ]
    reports = reports[-15:][::-1]
    if not reports:
        await ctx.respond("No reports found.", ephemeral=True)
        return
    embed = discord.Embed(title=f"Reports ({status})", color=0xE74C3C)
    for r in reports:
        embed.add_field(
            name=f"#{r['id']} · {r['reported_name']} (by {r['reporter_name']}) · {r['created_at'][:10]} · {r.get('status', 'open')}",
            value=r["reason"][:150],
            inline=False,
        )
    await ctx.respond(embed=embed)


@bot.slash_command(
    name="reportresolve",
    description="Marks a report as resolved (Admin)",
    default_member_permissions=discord.Permissions(manage_messages=True),
)
async def reportresolve(
    ctx: discord.ApplicationContext,
    report_id: discord.Option(int, description="ID of the report", required=True),
):
    load_reports_data()
    for r in reports_data["reports"]:
        if r["id"] == report_id:
            r["status"] = "resolved"
            save_reports_data()
            await ctx.respond(f"Report #{report_id} marked as resolved.", ephemeral=True)
            return
    await ctx.respond(f"Report #{report_id} not found.", ephemeral=True)


async def check_spam(message):
    if message.author.guild_permissions.administrator:
        return
    if is_staff(message.author, message.guild.id):
        return
    now = time.time()
    user_id = message.author.id
    history = spam_state.setdefault(user_id, [])
    history.append((now, message.content))
    while history and now - history[0][0] > ANTISPAM_WINDOW:
        history.pop(0)
    triggered = False
    if len(history) >= ANTISPAM_MAX_MESSAGES:
        dupes = sum(1 for _, c in history if c and c == message.content)
        if dupes >= ANTISPAM_DUPES:
            triggered = True
    if not triggered:
        return
    if now - last_spam_warn.get(user_id, 0) < ANTISPAM_WARN_COOLDOWN:
        return
    last_spam_warn[user_id] = now
    try:
        embed = (
            discord.Embed(
                title="Antispam Warning",
                description=(
                    "You have been spamming in the FightLab.net Discord server.\n"
                    "Please slow down. Repeated spam can lead to a punishment."
                ),
                color=0xE74C3C,
            )
            .set_footer(text="FightLab.net")
        )
        await message.author.send(embed=embed)
    except discord.HTTPException:
        pass


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.guild:
        await check_spam(message)
    ticket_record = get_ticket(message.channel.id)
    if not ticket_record:
        return
    if str(message.author.id) == ticket_record["creator"]:
        ticket_record["last_user_activity"] = now_iso()
        request_id = ticket_record.get("logs_request_msg_id")
        if (
            request_id
            and message.reference
            and str(message.reference.message_id) == request_id
        ):
            try:
                expires = datetime.fromisoformat(ticket_record["logs_request_expires"])
            except (ValueError, TypeError):
                expires = None
            if expires and datetime.now(timezone.utc) <= expires:
                attachments = [attachment.url for attachment in message.attachments]
                ticket_record["logs"] = {
                    "content": message.content,
                    "attachments": attachments,
                    "received_at": now_iso(),
                }
                ticket_record.pop("logs_request_msg_id", None)
                ticket_record.pop("logs_request_expires", None)
                confirm = discord.Embed(
                    title="Logs received",
                    description="Your logs have been received. Thank you!",
                    color=0x2ECC71,
                    timestamp=discord.utils.utcnow(),
                )
                await message.channel.send(embed=confirm)
        save_ticket_data()


async def check_logs_requests():
    now = datetime.now(timezone.utc)
    for channel_id, ticket_record in list(tickets_data["tickets"].items()):
        request_id = ticket_record.get("logs_request_msg_id")
        if not request_id:
            continue
        try:
            expires = datetime.fromisoformat(ticket_record["logs_request_expires"])
        except (ValueError, TypeError):
            continue
        if now <= expires:
            continue
        ticket_record.pop("logs_request_msg_id", None)
        ticket_record.pop("logs_request_expires", None)
        save_ticket_data()
        channel = bot.get_channel(int(channel_id))
        if channel:
            embed = discord.Embed(
                title="Logs Request Expired",
                description="The 10-minute window to send your logs has passed. Please ask the team for a new request.",
                color=0xE74C3C,
                timestamp=discord.utils.utcnow(),
            )
            await channel.send(embed=embed)


async def check_autoclose():
    now = datetime.now(timezone.utc)
    for channel_id, ticket_record in list(tickets_data["tickets"].items()):
        if ticket_record.get("status") != "open":
            continue
        last_activity = ticket_record.get("last_user_activity")
        if not last_activity:
            continue
        try:
            last_dt = datetime.fromisoformat(last_activity)
        except ValueError:
            continue
        if now - last_dt < timedelta(hours=24):
            continue
        channel = bot.get_channel(int(channel_id))
        warnings = ticket_record.get("warnings", 0)
        if warnings < 3:
            ticket_record["warnings"] = warnings + 1
            ticket_record["last_user_activity"] = now_iso()
            save_ticket_data()
            if channel:
                embed = discord.Embed(
                    title=f"Auto-Close Warning {ticket_record['warnings']}/3",
                    description="Nothing has been written in this ticket for over 24 hours. If the creator stays inactive, the ticket will be closed.",
                    color=0xF1C40F,
                )
                await channel.send(embed=embed)
            continue
        if channel:
            await close_ticket(channel)
            embed = discord.Embed(
                title="Auto-Close",
                description="This ticket has been closed automatically due to inactivity.",
                color=0xE74C3C,
            )
            await channel.send(embed=embed)


async def autoclose_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(60)
        try:
            await check_autoclose()
            await check_logs_requests()
        except Exception as exc:
            print("Auto-Close error:", exc)


@bot.event
async def on_ready():
    print(f"BUILD: {BUILD_TAG}")
    print(f"Eingeloggt als {bot.user} (ID: {bot.user.id})")
    load_invites_data()
    for guild in bot.guilds:
        print(f"- {guild.name}")
        invites_data["invite_cache"][str(guild.id)] = await fetch_invite_cache(guild)
    save_invites_data()
    load_ticket_data()
    panel_topics = load_ticket_topics()
    for settings in tickets_data["settings"].values():
        panel_message_id = settings.get("panel_message_id")
        if panel_message_id:
            bot.add_view(TicketPanelView(panel_topics), message_id=int(panel_message_id))
    for ticket_record in tickets_data["tickets"].values():
        close_msg_id = ticket_record.get("close_msg_id")
        if close_msg_id:
            if ticket_record.get("close_request") == "staff":
                bot.add_view(
                    TicketStaffCloseConfirmView(), message_id=int(close_msg_id)
                )
            else:
                bot.add_view(TicketCloseConfirmView(), message_id=int(close_msg_id))
        owner_left_msg_id = ticket_record.get("owner_left_msg_id")
        if owner_left_msg_id:
            bot.add_view(OwnerLeftView(), message_id=int(owner_left_msg_id))
        closed_msg_id = ticket_record.get("closed_msg_id")
        if closed_msg_id:
            bot.add_view(ClosedTicketView(), message_id=int(closed_msg_id))
        if ticket_record.get("status") == "open" and ticket_record.get("ticket_msg_id"):
            bot.add_view(TicketChannelView(), message_id=int(ticket_record["ticket_msg_id"]))
    save_ticket_data()
    bot.loop.create_task(web_server())
    bot.loop.create_task(autoclose_loop())


if __name__ == "__main__":
    bot.run(TOKEN)
