print("=== BOT.PY LOADED ===")
import os
import asyncpg
import discord
from discord import app_commands
from discord.ext import tasks
from datetime import datetime, timedelta, timezone
from typing import Optional

TOKEN        = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# ================= CONFIG =================
GUILD_ID = 1424245362117447753
OWNER_ID = 1352261320677916732  # Set your Discord user ID here for test/bypass mode

AUCTION_CHANNELS = {
    "TL": 1510325217896042536,  # KTL + ETL
    "TS": 1510325258224271421,  # TS only
}

ROLE_IDS = {
    "KTL": 1424283808269860884,
    "ETL": 1424325518832177193,
    "TS":  1424324905108770899,
}

PAYRATES = {
    "KTL": 5000,
    "ETL": 3000,
    "TS":  5000,
}

URGENT_BONUS = {
    "TL": 2000,   # applies to KTL + ETL
    "TS": 2000,   # applies to TS
}

DEADLINE_HOURS = {"normal": 48, "urgent": 3}

# Per-role urgent deadlines (Fitur #4)
URGENT_DEADLINES = {
    "KTL": 2,
    "ETL": 2,
    "TS":  3,
}

MAX_ACTIVE     = 2
TL_ROLES       = ["KTL", "ETL"]

ADMIN_ROLE_ID = 1424282282142732348  # Role to ping for TS alerts
UPLOADER_ROLE_ID = 1436698468470231080  # Role to ping when TS ready upload

# (hours_remaining, stage_number, label) — checked every 5 min
REMINDER_STAGES = [
    (24, 1, "⏰ Deadline dalam **24 jam**"),
    (12, 2, "⏰ Deadline dalam **12 jam**"),
    ( 6, 3, "🟠 Deadline dalam **6 jam**"),
    ( 3, 4, "🟠 Deadline dalam **3 jam**"),
    ( 1, 5, "🔴 SEGERA! Deadline dalam **1 jam**"),
]

# Fallback channel names when ID lookup fails (e.g. test servers)
CHANNEL_NAME_FALLBACK = {
    "TL": ["lelang-tl", "auction-tl", "tl-auction"],
    "TS": ["lelang-ts", "auction-ts", "ts-auction"],
}


# ================= INTENTS =================
# Members intent removed — not needed for slash command interactions
intents = discord.Intents.default()
intents.message_content = True


# ================= BOT =================
class ScanBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.pool: Optional[asyncpg.Pool] = None

    async def setup_hook(self):
        print("=== SETUP_HOOK CALLED ===")
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        await init_db(self.pool)
        self.add_view(AuctionView(self))
        guild = discord.Object(id=GUILD_ID)
        # Debug: print auction params BEFORE sync
        auction_cmd_obj = self.tree.get_command("auction")
        if auction_cmd_obj:
            print(f"DEBUG auction params: {[p.name for p in auction_cmd_obj.parameters]}")
        else:
            print("DEBUG: auction command NOT found in tree")
        # Step 1: guild sync dulu (local global tree masih utuh)
        self.tree.clear_commands(guild=guild)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        print(f"✅ Synced {len(synced)} commands to guild {GUILD_ID}")
        for cmd in synced:
            print(f"  COMMAND: {cmd.name}")
            if cmd.name == "auction":
                print(f"  DISCORD RETURNED auction options: {[o.name for o in cmd.options]}")

        # Step 2: hapus global commands dari Discord via HTTP API
        # (tanpa menyentuh local tree, supaya restart berikut masih bisa copy_global_to)
        try:
            await self.http.bulk_upsert_global_commands(self.application_id, [])
            print("DEBUG: stale global commands wiped from Discord via HTTP API")
        except Exception as e:
            print(f"DEBUG: global wipe skipped ({e})")
        print("✅ Bot ready")
        deadline_check.start()

    async def on_ready(self):
        print(f"Bot online: {self.user}")


bot = ScanBot()


# ================= DATABASE =================
async def init_db(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS auctions (
            id                  SERIAL PRIMARY KEY,
            guild_id            TEXT,
            project_channel_id  TEXT,
            project_name        TEXT,
            auction_message_id  TEXT,
            auction_channel_id  TEXT,
            urgent              BOOLEAN DEFAULT FALSE,
            created_at          TIMESTAMPTZ DEFAULT NOW()
        )
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS chapter_assignments (
            id             SERIAL PRIMARY KEY,
            auction_id     INTEGER,
            chapter        TEXT,
            role           TEXT,
            assignee_id    TEXT,
            assignee_name  TEXT,
            status         TEXT DEFAULT 'available',
            claimed_at     TIMESTAMPTZ,
            deadline_at    TIMESTAMPTZ,
            done_at        TIMESTAMPTZ,
            reminder_stage INTEGER DEFAULT 0
        )
        """)
        # Safe migration for existing tables
        await conn.execute("""
        ALTER TABLE chapter_assignments
        ADD COLUMN IF NOT EXISTS reminder_stage INTEGER DEFAULT 0
        """)
        # Safe migration: new columns for custom deadline & rate
        await conn.execute("""
        ALTER TABLE auctions ADD COLUMN IF NOT EXISTS custom_deadline INTEGER
        """)
        await conn.execute("""
        ALTER TABLE auctions ADD COLUMN IF NOT EXISTS custom_rate INTEGER
        """)


# ================= HELPERS =================
def find_auction_channel(guild: discord.Guild, group: str) -> Optional[discord.TextChannel]:
    """Try channel ID first; fall back to name list for test servers."""
    ch = guild.get_channel(AUCTION_CHANNELS[group])
    if ch:
        return ch
    for name in CHANNEL_NAME_FALLBACK.get(group, []):
        for text_ch in guild.text_channels:
            if text_ch.name.lower() == name.lower():
                return text_ch
    return None


def effective_rate(role: str, urgent: bool) -> int:
    base = PAYRATES.get(role, 0)
    if not urgent:
        return base
    bonus_key = "TS" if role == "TS" else "TL"
    return base + URGENT_BONUS[bonus_key]


def parse_deadline(value: str) -> Optional[int]:
    """Parse '2h' → 2 hours, '1d' → 24 hours. Returns hours int or None."""
    v = value.strip().lower()
    try:
        if v.endswith("h"):
            return int(v[:-1])
        if v.endswith("d"):
            return int(v[:-1]) * 24
    except ValueError:
        pass
    return None


def parse_rate(value: str) -> Optional[int]:
    """Parse '5k' → 5000, '10k' → 10000, '15000' → 15000. Returns int or None."""
    v = value.strip().lower()
    try:
        if v.endswith("k"):
            return int(float(v[:-1]) * 1000)
        return int(v)
    except ValueError:
        return None


def user_has_role(member: discord.Member, role_name: str) -> bool:
    role_id = ROLE_IDS.get(role_name)
    if not role_id:
        return False
    return any(r.id == role_id for r in member.roles)


def is_owner(user) -> bool:
    """True if user is the bot owner. Always checks owner FIRST before any guild restriction."""
    if not user or not OWNER_ID:
        return False
    return int(user.id) == int(OWNER_ID)


async def home_guild_check(interaction: discord.Interaction) -> bool:
    """Return True if owner (bypass all guilds) OR interaction is from the home guild."""
    # OWNER first — bypass everything
    if is_owner(interaction.user):
        return True
    # then guild check
    if interaction.guild_id == GUILD_ID:
        return True
    await interaction.response.send_message(
        "❌ Bot ini hanya aktif di server resmi.", ephemeral=True
    )
    return False


async def count_active(pool: asyncpg.Pool, guild_id: str, user_id: str) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT COUNT(*) FROM chapter_assignments ca
            JOIN auctions a ON a.id = ca.auction_id
            WHERE a.guild_id=$1 AND ca.assignee_id=$2 AND ca.status='claimed'
        """, guild_id, user_id)
        return row[0]


# ================= EMBED =================
async def build_embed(pool: asyncpg.Pool, auction_id: int) -> discord.Embed:
    async with pool.acquire() as conn:
        auction = await conn.fetchrow("SELECT * FROM auctions WHERE id=$1", auction_id)
        rows    = await conn.fetch(
            "SELECT * FROM chapter_assignments WHERE auction_id=$1 ORDER BY LPAD(chapter, 10, '0')",
            auction_id
        )

    if not auction:
        return discord.Embed(title="❌ Auction not found", color=discord.Color.red())

    custom_dl    = auction["custom_deadline"] if "custom_deadline" in auction.keys() else None
    custom_rate  = auction["custom_rate"]     if "custom_rate"     in auction.keys() else None
    # Auto-urgent: custom deadline 1–3h counts as urgent (Fitur #3)
    auto_urgent  = custom_dl is not None and 1 <= custom_dl <= 3
    urgent       = auction["urgent"] or auto_urgent
    if auto_urgent and not auction["urgent"]:
        mode = "🔥 URGENT (custom)"
    elif urgent:
        mode = "🔴 URGENT"
    else:
        mode = "🟢 Normal"
    project_name = auction["project_name"] or "unknown"
    proj_ch_id   = auction["project_channel_id"]
    ch_mention   = f"<#{proj_ch_id}>" if proj_ch_id else ""

    embed = discord.Embed(
        title=f"📋 #{project_name}",
        description=ch_mention or None,
        color=discord.Color.red() if urgent else discord.Color.blue()
    )
    embed.add_field(name="Mode", value=mode, inline=True)

    # Payrates field — use custom_rate if set, else effective_rate as before
    roles_present = [r for r in ["KTL", "ETL", "TS"] if any(x["role"] == r for x in rows)]
    if roles_present:
        if custom_rate is not None:
            rate_lines = "\n".join(f"**{r}**: {custom_rate // 1000}k" for r in roles_present)
        else:
            rate_lines = "\n".join(
                f"**{r}**: {effective_rate(r, urgent) // 1000}k" for r in roles_present
            )
        embed.add_field(name="💰 Bayaran", value=rate_lines, inline=True)

    # Deadline display — custom_deadline takes priority
    if custom_dl is not None:
        dl_display = f"{custom_dl} jam (custom)"
    elif urgent:
        dl_display = f"{DEADLINE_HOURS['urgent']} jam"
    else:
        dl_display = f"{DEADLINE_HOURS['normal']} jam"
    embed.add_field(name="⏰ Deadline", value=dl_display, inline=True)

    for role in ["KTL", "ETL", "TS"]:
        rrows = [r for r in rows if r["role"] == role]
        if not rrows:
            continue
        text = []
        for r in rrows:
            if r["status"] == "available":
                text.append(f"⏳ Ch {r['chapter']}")
            elif r["status"] == "claimed":
                text.append(f"🔒 Ch {r['chapter']} - <@{r['assignee_id']}>")
            else:
                text.append(f"✅ Ch {r['chapter']} - <@{r['assignee_id']}>")
        embed.add_field(name=role, value="\n".join(text), inline=True)

    return embed


# ================= REFRESH AUCTION MESSAGE =================
async def refresh_auction_message(client: ScanBot, auction_id: int):
    async with client.pool.acquire() as conn:
        auction = await conn.fetchrow("SELECT * FROM auctions WHERE id=$1", auction_id)

    if not auction or not auction["auction_message_id"]:
        return

    guild = client.get_guild(int(auction["guild_id"]))
    if not guild:
        return

    ch = guild.get_channel(int(auction["auction_channel_id"]))
    if not ch:
        return

    try:
        msg = await ch.fetch_message(int(auction["auction_message_id"]))
    except discord.NotFound:
        return

    new_embed = await build_embed(client.pool, auction_id)

    async with client.pool.acquire() as conn:
        # Only show buttons for roles that actually exist in this auction
        role_rows = await conn.fetch(
            "SELECT DISTINCT role FROM chapter_assignments WHERE auction_id=$1",
            auction_id
        )
        auction_roles = [r["role"] for r in role_rows if r["role"] in ["KTL", "ETL", "TS"]]
        auction_roles.sort(key=lambda x: ["KTL", "ETL", "TS"].index(x))

        view = AuctionView(client, allowed_roles=auction_roles or None)

        for item in view.children:
            if isinstance(item, ClaimButton):
                avail = await conn.fetchval("""
                    SELECT COUNT(*) FROM chapter_assignments
                    WHERE auction_id=$1 AND role=$2 AND status='available'
                """, auction_id, item.role)
                item.disabled = (avail == 0)

    await msg.edit(embed=new_embed, view=view)


# ================= VIEW =================
class ClaimButton(discord.ui.Button):
    def __init__(self, role: str):
        super().__init__(
            label=f"Claim {role}",
            style=discord.ButtonStyle.primary,
            custom_id=f"claim_{role}"
        )
        self.role = role

    async def callback(self, interaction: discord.Interaction):
        pool   = interaction.client.pool
        member = interaction.user

        # Guild lock
        if interaction.guild_id != GUILD_ID and not is_owner(member):
            return await interaction.response.send_message(
                "❌ Bot ini hanya aktif di server resmi.", ephemeral=True
            )

        # Role check (by ID)
        if not user_has_role(member, self.role):
            return await interaction.response.send_message(
                f"❌ Kamu tidak punya role **{self.role}** untuk klaim ini.", ephemeral=True
            )

        async with pool.acquire() as conn:
            auction = await conn.fetchrow(
                "SELECT * FROM auctions WHERE auction_message_id=$1",
                str(interaction.message.id)
            )

        if not auction:
            return await interaction.response.send_message("❌ Auction tidak ditemukan.", ephemeral=True)

        # Max active check
        active = await count_active(pool, str(interaction.guild_id), str(member.id))
        if active >= MAX_ACTIVE:
            return await interaction.response.send_message(
                f"❌ Kamu sudah punya **{MAX_ACTIVE}** chapter aktif. Selesaikan dulu sebelum klaim lagi.",
                ephemeral=True
            )

        # Next available chapter (ascending order)
        async with pool.acquire() as conn:
            ch = await conn.fetchrow("""
                SELECT * FROM chapter_assignments
                WHERE auction_id=$1 AND role=$2 AND status='available'
                ORDER BY LPAD(chapter, 10, '0') LIMIT 1
            """, auction["id"], self.role)

        if not ch:
            return await interaction.response.send_message(
                f"❌ Tidak ada chapter **{self.role}** yang tersedia.", ephemeral=True
            )

        # Deadline priority: custom_deadline > urgent per-role > normal (Fitur #4)
        custom_dl   = auction["custom_deadline"] if "custom_deadline" in auction.keys() else None
        auto_urgent = custom_dl is not None and 1 <= custom_dl <= 3
        effective_urgent = auction["urgent"] or auto_urgent
        if custom_dl is not None:
            deadline_hours = custom_dl
        elif effective_urgent:
            deadline_hours = URGENT_DEADLINES.get(self.role, DEADLINE_HOURS["urgent"])
        else:
            deadline_hours = DEADLINE_HOURS["normal"]
        deadline = datetime.now(timezone.utc) + timedelta(hours=deadline_hours)

        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE chapter_assignments
                SET status='claimed', assignee_id=$1, assignee_name=$2,
                    claimed_at=NOW(), deadline_at=$3
                WHERE id=$4
            """, str(member.id), member.display_name, deadline, ch["id"])

        await interaction.response.defer(ephemeral=True)
        await refresh_auction_message(interaction.client, auction["id"])

        # Ping in project channel
        project_ch = interaction.guild.get_channel(int(auction["project_channel_id"]))
        if project_ch:
            await project_ch.send(
                f"🎉 {member.mention} mengambil **{self.role} Ch {ch['chapter']}**!\n"
                f"⏰ Deadline: <t:{int(deadline.timestamp())}:F> (<t:{int(deadline.timestamp())}:R>)"
            )


class AuctionView(discord.ui.View):
    def __init__(self, bot_instance, allowed_roles: Optional[list] = None):
        super().__init__(timeout=None)
        # allowed_roles=None means all roles (used by setup_hook to register persistent views)
        roles = allowed_roles if allowed_roles is not None else ["KTL", "ETL", "TS"]
        for r in roles:
            self.add_item(ClaimButton(r))


# ================= /sync (owner only) =================
@bot.tree.command(name="sync", description="Force re-sync slash commands (owner only)")
async def sync_cmd(interaction: discord.Interaction):
    if not OWNER_ID or interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("❌ Hanya owner yang bisa menjalankan ini.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    await interaction.followup.send(f"✅ Berhasil sync {len(synced)} command.", ephemeral=True)


# ================= /auction =================
@bot.tree.command(name="auction", description="Buat lelang chapter baru (jalankan di channel project)")
@app_commands.describe(
    ktl="Chapter KTL, pisah koma (contoh: 50,51)",
    etl="Chapter ETL, pisah koma (contoh: 50,51)",
    ts="Chapter TS, pisah koma (contoh: 50,51)",
    urgent="Mode urgent: deadline pendek + bonus bayaran",
    deadline="Custom deadline, contoh: 2h / 1d (opsional)",
    rate="Custom bayaran semua role, contoh: 5k / 8000 (opsional)",
)
async def auction_cmd(
    interaction: discord.Interaction,
    ktl: Optional[str] = None,
    etl: Optional[str] = None,
    ts:  Optional[str] = None,
    urgent: bool = False,
    deadline: Optional[str] = None,
    rate: Optional[str] = None,
):
    if not await home_guild_check(interaction):
        return

    chapters = {
        "KTL": [c.strip() for c in (ktl or "").split(",") if c.strip()],
        "ETL": [c.strip() for c in (etl or "").split(",") if c.strip()],
        "TS":  [c.strip() for c in (ts  or "").split(",") if c.strip()],
    }

    if not any(chapters.values()):
        return await interaction.response.send_message(
            "❌ Masukkan minimal 1 chapter (ktl/etl/ts).", ephemeral=True
        )

    # Parse new optional params
    custom_dl_h: Optional[int] = None
    if deadline:
        custom_dl_h = parse_deadline(deadline)
        if custom_dl_h is None:
            return await interaction.response.send_message(
                "❌ Format deadline tidak valid. Gunakan contoh: `2h`, `1d`", ephemeral=True
            )

    custom_rate_val: Optional[int] = None
    if rate:
        custom_rate_val = parse_rate(rate)
        if custom_rate_val is None:
            return await interaction.response.send_message(
                "❌ Format rate tidak valid. Gunakan contoh: `5k`, `8000`", ephemeral=True
            )

    # Auto-urgent if custom deadline 1–3h (Fitur #3)
    auto_urgent = custom_dl_h is not None and 1 <= custom_dl_h <= 3
    effective_urgent = urgent or auto_urgent

    # Route to correct auction channel (ID first, then name fallback)
    is_ts_only = bool(chapters["TS"]) and not chapters["KTL"] and not chapters["ETL"]
    group      = "TS" if is_ts_only else "TL"
    channel    = find_auction_channel(interaction.guild, group)

    if not channel:
        fallback_names = " / ".join(CHANNEL_NAME_FALLBACK[group])
        return await interaction.response.send_message(
            f"❌ Channel auction tidak ditemukan. "
            f"Pastikan ada channel dengan nama: **{fallback_names}**",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    async with bot.pool.acquire() as conn:
        auction_id = await conn.fetchval("""
            INSERT INTO auctions (guild_id, project_channel_id, project_name, urgent,
                                  custom_deadline, custom_rate)
            VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
        """, str(interaction.guild_id), str(interaction.channel_id),
            interaction.channel.name, effective_urgent, custom_dl_h, custom_rate_val)

        rows_to_insert = [
            (auction_id, ch, role)
            for role, chs in chapters.items()
            for ch in chs
        ]
        if rows_to_insert:
            await conn.executemany(
                "INSERT INTO chapter_assignments (auction_id, chapter, role) VALUES ($1, $2, $3)",
                rows_to_insert
            )

    embed = await build_embed(bot.pool, auction_id)
    # Only show buttons for roles that were actually included in this auction
    allowed_roles = [r for r in ["KTL", "ETL", "TS"] if chapters[r]]
    view = AuctionView(bot, allowed_roles=allowed_roles)

    # Build role mention string (no global staff ping — role-specific only)
    mentions = []
    if group == "TL":
        if chapters["KTL"]:
            mentions.append(f"<@&{ROLE_IDS['KTL']}>")
        if chapters["ETL"]:
            mentions.append(f"<@&{ROLE_IDS['ETL']}>")
    else:
        mentions.append(f"<@&{ROLE_IDS['TS']}>")

    msg = await channel.send(content=" ".join(mentions), embed=embed, view=view)

    async with bot.pool.acquire() as conn:
        await conn.execute("""
            UPDATE auctions SET auction_message_id=$1, auction_channel_id=$2 WHERE id=$3
        """, str(msg.id), str(channel.id), auction_id)

    await interaction.followup.send("✅ Auction berhasil dibuat!", ephemeral=True)


# ================= EXECUTE MARK DONE (called after dropdown selection) =================
async def execute_mark_done(interaction: discord.Interaction, role: str, chapter: str, row):
    """Run DB update + notifications after user confirms via dropdown.
    The initial interaction is already responded to (dropdown was shown),
    so we use followup / channel.send for all further messages."""
    pool    = interaction.client.pool
    user_id = str(interaction.user.id)

    # Safety: re-check ownership (race condition guard)
    if row["assignee_id"] != user_id:
        return await interaction.followup.send(
            f"❌ Chapter ini bukan milikmu.", ephemeral=True
        )

    async with pool.acquire() as conn:
        updated = await conn.fetchval(
            """UPDATE chapter_assignments
               SET status='done', done_at=NOW()
               WHERE id=$1 AND status='claimed'
               RETURNING id""",
            row["id"]
        )

    if not updated:
        return await interaction.followup.send(
            f"❌ **{role} #{chapter}** sudah selesai atau tidak lagi aktif.", ephemeral=True
        )

    # Public confirmation in the project channel
    await interaction.channel.send(
        f"✅ **{role} #{chapter}** selesai! Dikerjakan oleh {interaction.user.mention}."
    )

    await refresh_auction_message(interaction.client, row["auction_id"])

    admin_ping    = f"<@&{ADMIN_ROLE_ID}>\n"    if ADMIN_ROLE_ID    else ""
    uploader_ping = f"<@&{UPLOADER_ROLE_ID}>\n" if UPLOADER_ROLE_ID else ""

    # ── TS LOGIC ──────────────────────────────────────────────────────────
    if role == "TS":
        # 1. Per-chapter: ping uploader
        await interaction.channel.send(
            f"{uploader_ping}"
            f"📢 TS **#{chapter}** sudah selesai!\n"
            f"Silakan upload."
        )

        # 2. Final: if ALL TS in this auction are done → ping admin once
        async with pool.acquire() as conn:
            all_ts = await conn.fetch("""
                SELECT status FROM chapter_assignments
                WHERE auction_id=$1 AND role='TS'
            """, row["auction_id"])

        if all_ts and all(r["status"] == "done" for r in all_ts):
            await interaction.channel.send(
                f"{admin_ping}"
                f"📢 Project ini sudah selesai di TS~\n"
                f"Tidak ada TS yang tersisa lagi."
            )

    # ── TL LOGIC ──────────────────────────────────────────────────────────
    if role in TL_ROLES:
        # Only 1 notif: when ALL TL in the entire auction are done → ping admin once
        async with pool.acquire() as conn:
            all_tl = await conn.fetch("""
                SELECT status FROM chapter_assignments
                WHERE auction_id=$1 AND role=ANY($2::text[])
            """, row["auction_id"], TL_ROLES)

        if all_tl and all(r["status"] == "done" for r in all_tl):
            await interaction.channel.send(
                f"{admin_ping}"
                f"📢 SEMUA TL SELESAI!\n"
                f"👉 Silakan lanjutkan membuat Lelang TS."
            )


# ================= CHAPTER SELECT UI =================
class ChapterSelect(discord.ui.Select):
    def __init__(self, role: str, rows: list):
        options = [
            discord.SelectOption(
                label=f"#{r['chapter']}",
                value=r["chapter"],
                description=f"{role} • sedang dikerjakan"
            )
            for r in rows
        ]
        super().__init__(
            placeholder=f"Pilih chapter {role} yang selesai (bisa lebih dari 1)…",
            min_values=1,
            max_values=len(options),
            options=options,
        )
        self.role = role
        self.rows_map = {r["chapter"]: r for r in rows}

    async def callback(self, interaction: discord.Interaction):
        selected = self.values  # list of selected chapters

        # Disable dropdown so it can't be clicked twice
        self.disabled = True
        chapter_list = ", ".join(f"#{c}" for c in selected)
        await interaction.response.edit_message(
            content=f"⏳ Memproses **{self.role}** chapter: {chapter_list}…",
            view=self.view
        )

        for chapter in selected:
            row = self.rows_map.get(chapter)
            if not row:
                await interaction.followup.send(
                    f"❌ Chapter #{chapter} tidak ditemukan, coba lagi.", ephemeral=True
                )
                continue
            await execute_mark_done(interaction, self.role, chapter, row)


class ChapterSelectView(discord.ui.View):
    def __init__(self, role: str, rows: list):
        super().__init__(timeout=60)
        self.add_item(ChapterSelect(role, rows))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ================= SHOW PICKER (entry point for /done commands) =================
async def show_chapter_picker(interaction: discord.Interaction, role: str):
    if not await home_guild_check(interaction):
        return

    pool       = interaction.client.pool
    channel_id = str(interaction.channel_id)
    user_id    = str(interaction.user.id)

    async with pool.acquire() as conn:
        active_rows = await conn.fetch("""
            SELECT ca.id, ca.chapter, ca.assignee_id, ca.assignee_name, ca.auction_id,
                   a.project_channel_id, a.auction_channel_id, a.guild_id
            FROM chapter_assignments ca
            JOIN auctions a ON a.id = ca.auction_id
            WHERE a.project_channel_id=$1
              AND ca.assignee_id=$2
              AND ca.role=$3
              AND ca.status='claimed'
            ORDER BY LPAD(ca.chapter, 10, '0')
        """, channel_id, user_id, role)

    if not active_rows:
        return await interaction.response.send_message(
            f"❌ Kamu tidak punya **{role}** chapter aktif di channel ini.", ephemeral=True
        )

    view = ChapterSelectView(role, active_rows)
    chapter_list = "  ".join(f"`#{r['chapter']}`" for r in active_rows)
    await interaction.response.send_message(
        f"📋 Pilih chapter **{role}** yang selesai:\n{chapter_list}",
        view=view,
        ephemeral=True
    )


# ================= /done COMMANDS =================
@bot.tree.command(name="ktldone", description="Tandai KTL chapter selesai")
async def ktldone(interaction: discord.Interaction):
    await show_chapter_picker(interaction, "KTL")


@bot.tree.command(name="etldone", description="Tandai ETL chapter selesai")
async def etldone(interaction: discord.Interaction):
    await show_chapter_picker(interaction, "ETL")


@bot.tree.command(name="tsdone", description="Tandai TS chapter selesai")
async def tsdone(interaction: discord.Interaction):
    await show_chapter_picker(interaction, "TS")


# ================= DEADLINE CHECK =================
@tasks.loop(minutes=5)
async def deadline_check():
    if not bot.pool:
        return

    now = datetime.now(timezone.utc)

    async with bot.pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT ca.id, ca.assignee_id, ca.role, ca.chapter,
                   ca.deadline_at, ca.reminder_stage,
                   a.project_channel_id, a.guild_id, a.id AS auction_id,
                   a.custom_deadline
            FROM chapter_assignments ca
            JOIN auctions a ON a.id = ca.auction_id
            WHERE ca.status='claimed' AND ca.deadline_at IS NOT NULL
        """)

    for r in rows:
        guild = bot.get_guild(int(r["guild_id"]))
        if not guild:
            continue

        project_ch  = guild.get_channel(int(r["project_channel_id"]))
        remaining_s = (r["deadline_at"] - now).total_seconds()

        # ── EXPIRED ──────────────────────────────────────────────────────
        if remaining_s <= 0:
            if project_ch:
                notice = (
                    f"⚠️ **DEADLINE HABIS!**\n"
                    f"<@{r['assignee_id']}> tidak menyelesaikan "
                    f"**{r['role']} #{r['chapter']}** tepat waktu.\n"
                    f"📁 Project: <#{r['project_channel_id']}>\n"
                    f"Chapter akan dilelang ulang."
                )
                # Fitur #6: ping admin role, not OWNER_ID
                if ADMIN_ROLE_ID:
                    notice += f"\n🔔 <@&{ADMIN_ROLE_ID}> perlu reauction **{r['role']} #{r['chapter']}**"
                await project_ch.send(notice)

            async with bot.pool.acquire() as conn:
                await conn.execute("""
                    UPDATE chapter_assignments
                    SET status='available', assignee_id=NULL, assignee_name=NULL,
                        claimed_at=NULL, deadline_at=NULL, reminder_stage=0
                    WHERE id=$1
                """, r["id"])

            await refresh_auction_message(bot, r["auction_id"])
            continue

        # ── TIERED REMINDERS ─────────────────────────────────────────────
        remaining_h = remaining_s / 3600
        custom_dl   = r["custom_deadline"]  # hours int or None
        is_short    = custom_dl is not None and 1 <= custom_dl <= 3

        if is_short:
            # Fitur #5: urgent-only reminders, stages 91/92 (never conflict with normal 1-5)
            # Deadline 3h → remind at ≤2h (stage 91) and ≤1h (stage 92)
            # Deadline 2h or 1h → remind at ≤1h (stage 91)
            urgent_stages = []
            if custom_dl >= 3:
                urgent_stages = [
                    (2, 91, "⏰ Sisa **2 jam** lagi!"),
                    (1, 92, "🔴 Sisa **1 jam** lagi!"),
                ]
            else:
                urgent_stages = [(1, 91, "🔴 Sisa **1 jam** lagi!")]

            best_stage = 0
            best_label = ""
            for hours, stage_num, label in urgent_stages:
                if remaining_h <= hours and stage_num > r["reminder_stage"] and stage_num > best_stage:
                    best_stage = stage_num
                    best_label = label

            if best_stage > 0 and project_ch:
                ts = int(r["deadline_at"].timestamp())
                await project_ch.send(
                    f"⏰ **Reminder Deadline (Urgent)**\n"
                    f"<@{r['assignee_id']}> | **{r['role']} #{r['chapter']}**\n"
                    f"📁 Project: <#{r['project_channel_id']}>\n"
                    f"{best_label}\n"
                    f"Deadline: <t:{ts}:F> (<t:{ts}:R>)"
                )
                async with bot.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE chapter_assignments SET reminder_stage=$1 WHERE id=$2",
                        best_stage, r["id"]
                    )
        else:
            # Normal tiered reminders (unchanged)
            best_stage = 0
            best_label = ""
            for hours, stage_num, label in REMINDER_STAGES:
                if remaining_h <= hours and stage_num > best_stage:
                    best_stage = stage_num
                    best_label = label

            if best_stage > r["reminder_stage"] and project_ch:
                ts = int(r["deadline_at"].timestamp())
                await project_ch.send(
                    f"⏰ **Reminder Deadline**\n"
                    f"<@{r['assignee_id']}> | **{r['role']} #{r['chapter']}**\n"
                    f"📁 Project: <#{r['project_channel_id']}>\n"
                    f"{best_label}\n"
                    f"Deadline: <t:{ts}:F> (<t:{ts}:R>)"
                )
                async with bot.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE chapter_assignments SET reminder_stage=$1 WHERE id=$2",
                        best_stage, r["id"]
                    )


# ================= /unclaim =================
class UnclaimReasonModal(discord.ui.Modal, title="Alasan Unclaim"):
    reason = discord.ui.TextInput(
        label="Alasan unclaim",
        placeholder="Contoh: Tidak bisa menyelesaikan, ada keperluan mendadak...",
        required=True,
        max_length=200,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, row):
        super().__init__()
        self.row = row

    async def on_submit(self, interaction: discord.Interaction):
        pool    = interaction.client.pool
        row     = self.row
        user_id = str(interaction.user.id)

        # Safety: ensure chapter still belongs to this user
        if row["assignee_id"] != user_id:
            return await interaction.response.send_message(
                "❌ Chapter ini bukan milikmu.", ephemeral=True
            )

        async with pool.acquire() as conn:
            updated = await conn.fetchval("""
                UPDATE chapter_assignments
                SET status='available', assignee_id=NULL, assignee_name=NULL,
                    claimed_at=NULL, deadline_at=NULL
                WHERE id=$1 AND status='claimed' AND assignee_id=$2
                RETURNING id
            """, row["id"], user_id)

        if not updated:
            return await interaction.response.send_message(
                "❌ Gagal unclaim. Chapter sudah tidak di-claim atau bukan milikmu.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            f"✅ Berhasil unclaim **{row['role']} #{row['chapter']}**",
            ephemeral=True,
        )

        await refresh_auction_message(interaction.client, row["auction_id"])

        project_ch = interaction.guild.get_channel(int(row["project_channel_id"]))
        if project_ch:
            admin_ping = f"<@&{ADMIN_ROLE_ID}>\n" if ADMIN_ROLE_ID else ""
            await project_ch.send(
                f"{admin_ping}"
                f"⚠️ **Chapter dilepas (UNCLAIM)**\n"
                f"📌 **{row['role']} #{row['chapter']}**\n"
                f"👤 {interaction.user.mention}\n"
                f"📝 Alasan: {self.reason.value}"
            )


class UnclaimSelect(discord.ui.Select):
    def __init__(self, rows: list):
        options = [
            discord.SelectOption(
                label=f"{r['role']} #{r['chapter']}",
                value=str(r["id"]),
                description="sedang di-claim oleh kamu",
            )
            for r in rows
        ]
        super().__init__(
            placeholder="Pilih chapter yang ingin di-unclaim…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.rows_map = {str(r["id"]): r for r in rows}

    async def callback(self, interaction: discord.Interaction):
        selected_id = self.values[0]
        row = self.rows_map.get(selected_id)
        if not row:
            return await interaction.response.send_message(
                "❌ Chapter tidak ditemukan, coba lagi.", ephemeral=True
            )
        await interaction.response.send_modal(UnclaimReasonModal(row))


class UnclaimSelectView(discord.ui.View):
    def __init__(self, rows: list):
        super().__init__(timeout=60)
        self.add_item(UnclaimSelect(rows))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


@bot.tree.command(name="unclaim", description="Lepaskan chapter yang sedang kamu kerjakan")
async def unclaim_cmd(interaction: discord.Interaction):
    if not await home_guild_check(interaction):
        return

    pool       = interaction.client.pool
    user_id    = str(interaction.user.id)
    channel_id = str(interaction.channel_id)

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT ca.id, ca.chapter, ca.role, ca.assignee_id, ca.auction_id,
                   a.project_channel_id, a.guild_id
            FROM chapter_assignments ca
            JOIN auctions a ON a.id = ca.auction_id
            WHERE a.project_channel_id=$1
              AND ca.assignee_id=$2
              AND ca.status='claimed'
            ORDER BY LPAD(ca.chapter, 10, '0')
        """, channel_id, user_id)

    if not rows:
        return await interaction.response.send_message(
            "❌ Kamu tidak punya chapter yang sedang di-claim di channel ini.",
            ephemeral=True,
        )

    chapter_list = "  ".join(f"`{r['role']} #{r['chapter']}`" for r in rows)
    await interaction.response.send_message(
        f"📋 Pilih chapter yang ingin di-unclaim:\n{chapter_list}",
        view=UnclaimSelectView(rows),
        ephemeral=True,
    )


bot.run(TOKEN)
