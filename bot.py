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

STAFF_ROLE_ID = 11436694074093604904  # kept as-is per your config

PAYRATES = {
    "KTL": 5000,
    "ETL": 3000,
    "TS":  5000,
}

URGENT_BONUS = {
    "TL": 2000,   # applies to KTL + ETL
    "TS": 5000,   # applies to TS
}

DEADLINE_HOURS = {"normal": 48, "urgent": 3}
MAX_ACTIVE     = 2
TL_ROLES       = ["KTL", "ETL"]

# Fallback channel names when ID lookup fails (e.g. test servers)
CHANNEL_NAME_FALLBACK = {
    "TL": ["lelang-tl", "auction-tl", "tl-auction"],
    "TS": ["lelang-ts", "auction-ts", "ts-auction"],
}


# ================= INTENTS =================
# Members intent removed â€” not needed for slash command interactions
intents = discord.Intents.default()
intents.message_content = True


# ================= BOT =================
class ScanBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.pool: Optional[asyncpg.Pool] = None

    async def setup_hook(self):
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        await init_db(self.pool)
        self.add_view(AuctionView(self))
        await self.tree.sync()
        print("âœ… Bot ready")
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
            id            SERIAL PRIMARY KEY,
            auction_id    INTEGER,
            chapter       TEXT,
            role          TEXT,
            assignee_id   TEXT,
            assignee_name TEXT,
            status        TEXT DEFAULT 'available',
            claimed_at    TIMESTAMPTZ,
            deadline_at   TIMESTAMPTZ,
            done_at       TIMESTAMPTZ
        )
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
    # OWNER first â€” bypass everything
    if is_owner(interaction.user):
        return True
    # then guild check
    if interaction.guild_id == GUILD_ID:
        return True
    await interaction.response.send_message(
        "âŒ Bot ini hanya aktif di server resmi.", ephemeral=True
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
            "SELECT * FROM chapter_assignments WHERE auction_id=$1 ORDER BY chapter",
            auction_id
        )

    if not auction:
        return discord.Embed(title="âŒ Auction not found", color=discord.Color.red())

    urgent = auction["urgent"]
    mode   = "ðŸ”´ URGENT" if urgent else "ðŸŸ¢ Normal"

    embed = discord.Embed(
        title=f"ðŸ“‹ {auction['project_name']}",
        color=discord.Color.red() if urgent else discord.Color.blue()
    )
    embed.add_field(name="Mode", value=mode, inline=True)

    # Payrates field
    roles_present = [r for r in ["KTL", "ETL", "TS"] if any(x["role"] == r for x in rows)]
    if roles_present:
        rate_lines = "\n".join(
            f"**{r}**: {effective_rate(r, urgent) // 1000}k" for r in roles_present
        )
        embed.add_field(name="ðŸ’° Bayaran", value=rate_lines, inline=True)

    deadline_h = DEADLINE_HOURS["urgent"] if urgent else DEADLINE_HOURS["normal"]
    embed.add_field(name="â° Deadline", value=f"{deadline_h} jam", inline=True)

    for role in ["KTL", "ETL", "TS"]:
        rrows = [r for r in rows if r["role"] == role]
        if not rrows:
            continue
        text = []
        for r in rrows:
            if r["status"] == "available":
                text.append(f"â³ Ch {r['chapter']}")
            elif r["status"] == "claimed":
                text.append(f"ðŸ”’ Ch {r['chapter']} - <@{r['assignee_id']}>")
            else:
                text.append(f"âœ… Ch {r['chapter']} - <@{r['assignee_id']}>")
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
                "âŒ Bot ini hanya aktif di server resmi.", ephemeral=True
            )

        # Role check (by ID)
        if not user_has_role(member, self.role):
            return await interaction.response.send_message(
                f"âŒ Kamu tidak punya role **{self.role}** untuk klaim ini.", ephemeral=True
            )

        async with pool.acquire() as conn:
            auction = await conn.fetchrow(
                "SELECT * FROM auctions WHERE auction_message_id=$1",
                str(interaction.message.id)
            )

        if not auction:
            return await interaction.response.send_message("âŒ Auction tidak ditemukan.", ephemeral=True)

        # Max active check
        active = await count_active(pool, str(interaction.guild_id), str(member.id))
        if active >= MAX_ACTIVE:
            return await interaction.response.send_message(
                f"âŒ Kamu sudah punya **{MAX_ACTIVE}** chapter aktif. Selesaikan dulu sebelum klaim lagi.",
                ephemeral=True
            )

        # Next available chapter (ascending order)
        async with pool.acquire() as conn:
            ch = await conn.fetchrow("""
                SELECT * FROM chapter_assignments
                WHERE auction_id=$1 AND role=$2 AND status='available'
                ORDER BY chapter LIMIT 1
            """, auction["id"], self.role)

        if not ch:
            return await interaction.response.send_message(
                f"âŒ Tidak ada chapter **{self.role}** yang tersedia.", ephemeral=True
            )

        deadline = datetime.now(timezone.utc) + timedelta(
            hours=DEADLINE_HOURS["urgent"] if auction["urgent"] else DEADLINE_HOURS["normal"]
        )

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
                f"ðŸŽ‰ {member.mention} mengambil **{self.role} Ch {ch['chapter']}**!\n"
                f"â° Deadline: <t:{int(deadline.timestamp())}:F> (<t:{int(deadline.timestamp())}:R>)"
            )


class AuctionView(discord.ui.View):
    def __init__(self, bot_instance, allowed_roles: Optional[list] = None):
        super().__init__(timeout=None)
        # allowed_roles=None means all roles (used by setup_hook to register persistent views)
        roles = allowed_roles if allowed_roles is not None else ["KTL", "ETL", "TS"]
        for r in roles:
            self.add_item(ClaimButton(r))


# ================= /auction =================
@bot.tree.command(name="auction", description="Buat lelang chapter baru (jalankan di channel project)")
@app_commands.describe(
    ktl="Chapter KTL, pisah koma (contoh: 50,51)",
    etl="Chapter ETL, pisah koma (contoh: 50,51)",
    ts="Chapter TS, pisah koma (contoh: 50,51)",
    urgent="Mode urgent: deadline 3 jam + bonus bayaran"
)
async def auction_cmd(
    interaction: discord.Interaction,
    ktl: Optional[str] = None,
    etl: Optional[str] = None,
    ts:  Optional[str] = None,
    urgent: bool = False,
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
            "âŒ Masukkan minimal 1 chapter (ktl/etl/ts).", ephemeral=True
        )

    # Route to correct auction channel (ID first, then name fallback)
    is_ts_only = bool(chapters["TS"]) and not chapters["KTL"] and not chapters["ETL"]
    group      = "TS" if is_ts_only else "TL"
    channel    = find_auction_channel(interaction.guild, group)

    if not channel:
        fallback_names = " / ".join(CHANNEL_NAME_FALLBACK[group])
        return await interaction.response.send_message(
            f"âŒ Channel auction tidak ditemukan. "
            f"Pastikan ada channel dengan nama: **{fallback_names}**",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    async with bot.pool.acquire() as conn:
        auction_id = await conn.fetchval("""
            INSERT INTO auctions (guild_id, project_channel_id, project_name, urgent)
            VALUES ($1, $2, $3, $4) RETURNING id
        """, str(interaction.guild_id), str(interaction.channel_id),
            interaction.channel.name, urgent)

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

    # Build role mention string
    mentions = []
    try:
        mentions.append(f"<@&{STAFF_ROLE_ID}>")
    except Exception:
        pass
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

    await interaction.followup.send("âœ… Auction berhasil dibuat!", ephemeral=True)


# ================= AUTO TS =================
async def auto_create_ts_auction(client: ScanBot, auction_id: int, guild_id: str,
                                  project_channel_id: str, chapter: str):
    async with client.pool.acquire() as conn:
        existing = await conn.fetchrow("""
            SELECT 1 FROM chapter_assignments
            WHERE auction_id=$1 AND chapter=$2 AND role='TS'
        """, auction_id, chapter)

        if existing:
            return

        await conn.execute("""
            INSERT INTO chapter_assignments (auction_id, chapter, role, status)
            VALUES ($1, $2, 'TS', 'available')
        """, auction_id, chapter)

    guild = client.get_guild(int(guild_id))
    if not guild:
        return

    channel = guild.get_channel(int(project_channel_id))
    if channel:
        await channel.send(
            f"ðŸš¨ **AUTO AUCTION TS CREATED**\n"
            f"ðŸ“Œ Ch {chapter} belum dilelang untuk TS\n"
            f"ðŸ‘‰ Silakan claim di channel lelang!"
        )

    await refresh_auction_message(client, auction_id)


# ================= MARK DONE =================
async def mark_done(interaction: discord.Interaction, role: str, chapter: Optional[str] = None):
    if not await home_guild_check(interaction):
        return

    pool       = interaction.client.pool
    channel_id = str(interaction.channel_id)
    user_id    = str(interaction.user.id)

    async with pool.acquire() as conn:
        if chapter:
            row = await conn.fetchrow("""
                SELECT ca.id, ca.assignee_id, ca.assignee_name, ca.auction_id,
                       a.project_channel_id, a.auction_channel_id, a.guild_id
                FROM chapter_assignments ca
                JOIN auctions a ON a.id = ca.auction_id
                WHERE a.project_channel_id=$1 AND ca.chapter=$2
                  AND ca.role=$3 AND ca.status='claimed'
            """, channel_id, chapter, role)
        else:
            # Auto-detect: look up active chapters for this user and role
            active_rows = await conn.fetch("""
                SELECT ca.id, ca.chapter, ca.assignee_id, ca.assignee_name, ca.auction_id,
                       a.project_channel_id, a.auction_channel_id, a.guild_id
                FROM chapter_assignments ca
                JOIN auctions a ON a.id = ca.auction_id
                WHERE a.project_channel_id=$1 AND ca.assignee_id=$2
                  AND ca.role=$3 AND ca.status='claimed'
                ORDER BY ca.chapter
            """, channel_id, user_id, role)

            if not active_rows:
                return await interaction.response.send_message(
                    f"âŒ Kamu tidak punya **{role}** chapter aktif di channel ini.", ephemeral=True
                )
            if len(active_rows) > 1:
                ch_list = ", ".join(r["chapter"] for r in active_rows)
                cmd_name = f"{'ktl' if role == 'KTL' else role.lower()}done"
                return await interaction.response.send_message(
                    f"ðŸ“‹ Kamu punya beberapa chapter **{role}** aktif: **Ch {ch_list}**\n"
                    f"Gunakan `/{cmd_name} chapter:<nomor>` untuk menentukan.", ephemeral=True
                )
            row     = active_rows[0]
            chapter = row["chapter"]

    if not row:
        return await interaction.response.send_message(
            f"âŒ Tidak ada **{role} Ch {chapter}** yang sedang dikerjakan di channel ini.",
            ephemeral=True
        )

    if row["assignee_id"] != user_id:
        return await interaction.response.send_message(
            f"âŒ Chapter ini bukan milikmu (dikerjakan oleh **{row['assignee_name']}**).",
            ephemeral=True
        )

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE chapter_assignments SET status='done', done_at=NOW() WHERE id=$1",
            row["id"]
        )

    await interaction.response.send_message(
        f"âœ… **{role} Ch {chapter}** selesai! Dikerjakan oleh {interaction.user.mention}."
    )

    await refresh_auction_message(interaction.client, row["auction_id"])

    # AUTO TS CHECK AFTER TL DONE
    if role in TL_ROLES:
        async with pool.acquire() as conn:
            ts_row = await conn.fetchrow("""
                SELECT assignee_id, status
                FROM chapter_assignments
                WHERE auction_id=$1 AND chapter=$2 AND role='TS'
            """, row["auction_id"], chapter)

        # kalau TS belum ada / belum diambil â†’ auto create
        if not ts_row or ts_row["status"] != "claimed":
            await auto_create_ts_auction(
                interaction.client,
                row["auction_id"],
                row["guild_id"],
                row["project_channel_id"],
                chapter
            )
        else:
            await interaction.channel.send(
                f"ðŸ“¢ TS sudah ada untuk Ch {chapter} â†’ <@{ts_row['assignee_id']}>"
            )


@bot.tree.command(name="ktldone", description="Tandai KTL chapter selesai")
@app_commands.describe(chapter="Nomor chapter (kosongkan untuk auto-detect)")
async def ktldone(interaction: discord.Interaction, chapter: Optional[str] = None):
    await mark_done(interaction, "KTL", chapter)


@bot.tree.command(name="etldone", description="Tandai ETL chapter selesai")
@app_commands.describe(chapter="Nomor chapter (kosongkan untuk auto-detect)")
async def etldone(interaction: discord.Interaction, chapter: Optional[str] = None):
    await mark_done(interaction, "ETL", chapter)


@bot.tree.command(name="tsdone", description="Tandai TS chapter selesai")
@app_commands.describe(chapter="Nomor chapter (kosongkan untuk auto-detect)")
async def tsdone(interaction: discord.Interaction, chapter: Optional[str] = None):
    await mark_done(interaction, "TS", chapter)


# ================= DEADLINE CHECK =================
@tasks.loop(minutes=10)
async def deadline_check():
    if not bot.pool:
        return

    now = datetime.now(timezone.utc)

    async with bot.pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT ca.*, a.project_channel_id, a.guild_id, a.id AS auction_id
            FROM chapter_assignments ca
            JOIN auctions a ON a.id = ca.auction_id
            WHERE ca.status='claimed'
              AND ca.deadline_at IS NOT NULL
              AND ca.deadline_at <= $1
        """, now)

    for r in rows:
        guild = bot.get_guild(int(r["guild_id"]))
        if not guild:
            continue

        ch = guild.get_channel(int(r["project_channel_id"]))
        if ch:
            notice = (
                f"âš ï¸ **DEADLINE HABIS!**\n"
                f"<@{r['assignee_id']}> tidak menyelesaikan "
                f"**{r['role']} Ch {r['chapter']}** tepat waktu.\n"
                f"Chapter akan dilelang ulang."
            )
            if OWNER_ID:
                notice += f"\nðŸ”” <@{OWNER_ID}> perlu reauction **{r['role']} Ch {r['chapter']}**"
            await ch.send(notice)

        async with bot.pool.acquire() as conn:
            await conn.execute("""
                UPDATE chapter_assignments
                SET status='available', assignee_id=NULL, assignee_name=NULL,
                    claimed_at=NULL, deadline_at=NULL
                WHERE id=$1
            """, r["id"])

        await refresh_auction_message(bot, r["auction_id"])


bot.run(TOKEN)
