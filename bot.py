import os
import asyncpg
import discord
from discord import app_commands
from discord.ext import tasks
from datetime import datetime, timedelta, timezone
from typing import Optional

TOKEN        = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

PAYRATES        = {"KTL": 5000, "ETL": 3000, "TS": 5000}
URGENT_BONUS    = 2000
DEADLINE_HOURS  = {"normal": 48, "urgent": 3}
AUCTION_CHANNEL = "lelang-pj"
MAX_ACTIVE      = 2

intents = discord.Intents.default()
intents.message_content = True


# ─────────────────────────────────────────────
# Bot
# ─────────────────────────────────────────────
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
        print("✅ Slash commands synced")
        deadline_check.start()

    async def on_ready(self):
        print(f"✅ Bot online sebagai {self.user}")


bot = ScanBot()


# ─────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────
async def init_db(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS auctions (
                id                  SERIAL PRIMARY KEY,
                guild_id            TEXT NOT NULL,
                project_channel_id  TEXT NOT NULL,
                project_name        TEXT NOT NULL,
                auction_message_id  TEXT,
                auction_channel_id  TEXT,
                urgent              BOOLEAN DEFAULT FALSE,
                created_at          TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chapter_assignments (
                id            SERIAL PRIMARY KEY,
                auction_id    INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
                chapter       TEXT NOT NULL,
                role          TEXT NOT NULL,
                assignee_id   TEXT,
                assignee_name TEXT,
                status        TEXT DEFAULT 'available',
                claimed_at    TIMESTAMPTZ,
                deadline_at   TIMESTAMPTZ,
                done_at       TIMESTAMPTZ,
                UNIQUE(auction_id, chapter, role)
            )
        """)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def fmt_rate(base: int, urgent: bool) -> str:
    val = base + (URGENT_BONUS if urgent else 0)
    return f"{val // 1000}k"


def user_has_role(member: discord.Member, role_name: str) -> bool:
    return any(r.name.upper() == role_name.upper() for r in member.roles)


async def count_active(pool: asyncpg.Pool, guild_id: str, user_id: str) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT COUNT(*) AS n
            FROM chapter_assignments ca
            JOIN auctions a ON a.id = ca.auction_id
            WHERE a.guild_id = $1
              AND ca.assignee_id = $2
              AND ca.status = 'claimed'
        """, guild_id, user_id)
        return row["n"]


async def build_embed(pool: asyncpg.Pool, auction_id: int) -> discord.Embed:
    async with pool.acquire() as conn:
        auction = await conn.fetchrow("SELECT * FROM auctions WHERE id=$1", auction_id)
        rows    = await conn.fetch("""
            SELECT chapter, role, assignee_name, status, deadline_at
            FROM chapter_assignments
            WHERE auction_id=$1
            ORDER BY role, chapter::int NULLS LAST, chapter
        """, auction_id)

    urgent     = auction["urgent"]
    deadline_h = DEADLINE_HOURS["urgent"] if urgent else DEADLINE_HOURS["normal"]
    mode_str   = "🔴 **URGENT**" if urgent else "🟢 Normal"

    embed = discord.Embed(
        title=f"📋 Lelang: {auction['project_name']}",
        color=discord.Color.red() if urgent else discord.Color.blue()
    )
    embed.add_field(name="📢 Channel", value=f"<#{auction['project_channel_id']}>", inline=False)

    roles_present = [r for r in ["KTL", "ETL", "TS"] if any(row["role"] == r for row in rows)]
    rate_lines = "\n".join(
        f"**{r}**: {fmt_rate(PAYRATES[r], urgent)}" for r in roles_present
    )
    embed.add_field(name=f"💰 Payrate ({mode_str})", value=rate_lines, inline=False)
    embed.add_field(name="⏰ Deadline", value=f"{deadline_h} jam setelah claim", inline=False)

    for role in ["KTL", "ETL", "TS"]:
        role_rows = [r for r in rows if r["role"] == role]
        if not role_rows:
            continue
        lines = []
        for r in role_rows:
            if r["status"] == "available":
                lines.append(f"⏳ Ch {r['chapter']}")
            elif r["status"] == "claimed":
                dl    = r["deadline_at"]
                dl_ts = f" ⏰ <t:{int(dl.timestamp())}:R>" if dl else ""
                lines.append(f"🔒 Ch {r['chapter']} — **{r['assignee_name']}**{dl_ts}")
            else:
                lines.append(f"✅ Ch {r['chapter']} — ~~{r['assignee_name']}~~")
        embed.add_field(name=f"**{role}**", value="\n".join(lines), inline=True)

    return embed


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
    view      = AuctionView(client)

    async with client.pool.acquire() as conn:
        for item in view.children:
            if isinstance(item, ClaimButton):
                avail = await conn.fetchval("""
                    SELECT COUNT(*) FROM chapter_assignments
                    WHERE auction_id=$1 AND role=$2 AND status='available'
                """, auction_id, item.role_name)
                item.disabled = (avail == 0)

    await msg.edit(embed=new_embed, view=view)


# ─────────────────────────────────────────────
# Persistent Auction View
# ─────────────────────────────────────────────
class ClaimButton(discord.ui.Button):
    def __init__(self, role: str):
        super().__init__(
            label=f"Claim {role}",
            custom_id=f"claim_{role}",
            style=discord.ButtonStyle.primary
        )
        self.role_name = role

    async def callback(self, interaction: discord.Interaction):
        pool   = interaction.client.pool
        member = interaction.user  # Member in guild context

        if not isinstance(member, discord.Member):
            return await interaction.response.send_message("❌ Error: tidak bisa verifikasi role kamu.", ephemeral=True)

        # Role check
        if not user_has_role(member, self.role_name):
            return await interaction.response.send_message(
                f"❌ Kamu tidak punya role **{self.role_name}** untuk klaim ini.", ephemeral=True
            )

        # Look up auction by message ID
        async with pool.acquire() as conn:
            auction = await conn.fetchrow(
                "SELECT * FROM auctions WHERE auction_message_id=$1",
                str(interaction.message.id)
            )
        if not auction:
            return await interaction.response.send_message("❌ Auction tidak ditemukan.", ephemeral=True)

        # Active chapter limit
        active = await count_active(pool, str(interaction.guild_id), str(member.id))
        if active >= MAX_ACTIVE:
            return await interaction.response.send_message(
                f"❌ Kamu sudah punya **{MAX_ACTIVE}** chapter aktif. Selesaikan dulu sebelum klaim lagi.",
                ephemeral=True
            )

        # Find next available chapter (ascending)
        async with pool.acquire() as conn:
            next_ch = await conn.fetchrow("""
                SELECT id, chapter FROM chapter_assignments
                WHERE auction_id=$1 AND role=$2 AND status='available'
                ORDER BY chapter::int NULLS LAST, chapter
                LIMIT 1
            """, auction["id"], self.role_name)

        if not next_ch:
            return await interaction.response.send_message(
                f"❌ Tidak ada chapter **{self.role_name}** yang tersedia.", ephemeral=True
            )

        # Assign
        urgent     = auction["urgent"]
        deadline_h = DEADLINE_HOURS["urgent"] if urgent else DEADLINE_HOURS["normal"]
        deadline   = datetime.now(timezone.utc) + timedelta(hours=deadline_h)

        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE chapter_assignments
                SET assignee_id=$1, assignee_name=$2, status='claimed',
                    claimed_at=NOW(), deadline_at=$3
                WHERE id=$4
            """, str(member.id), member.display_name, deadline, next_ch["id"])

        await interaction.response.defer()
        await refresh_auction_message(interaction.client, auction["id"])

        # Ping user in project channel
        project_ch = interaction.guild.get_channel(int(auction["project_channel_id"]))
        if project_ch:
            await project_ch.send(
                f"🎉 {member.mention} mengambil **{self.role_name} Ch {next_ch['chapter']}**!\n"
                f"⏰ Deadline: <t:{int(deadline.timestamp())}:F> (<t:{int(deadline.timestamp())}:R>)"
            )


class AuctionView(discord.ui.View):
    def __init__(self, bot_instance: ScanBot):
        super().__init__(timeout=None)
        for role in ["KTL", "ETL", "TS"]:
            self.add_item(ClaimButton(role))


# ─────────────────────────────────────────────
# /auction
# ─────────────────────────────────────────────
@bot.tree.command(name="auction", description="Buat lelang chapter baru (jalankan di channel project)")
@app_commands.describe(
    ktl="Chapter KTL, pisah koma (contoh: 50,51,52)",
    etl="Chapter ETL, pisah koma (contoh: 50,51,52)",
    ts="Chapter TS, pisah koma (contoh: 50,51,52)",
    urgent="Mode urgent: +2k payrate, deadline 3 jam"
)
async def auction_cmd(
    interaction: discord.Interaction,
    ktl: Optional[str] = None,
    etl: Optional[str] = None,
    ts:  Optional[str] = None,
    urgent: bool = False,
):
    if not ktl and not etl and not ts:
        return await interaction.response.send_message(
            "❌ Masukkan minimal satu daftar chapter (ktl/etl/ts).", ephemeral=True
        )

    auction_ch = discord.utils.find(
        lambda c: c.name == AUCTION_CHANNEL, interaction.guild.text_channels
    )
    if not auction_ch:
        return await interaction.response.send_message(
            f"❌ Channel **#{AUCTION_CHANNEL}** tidak ditemukan di server ini.", ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    pool         = interaction.client.pool
    project_name = interaction.channel.name
    chapters     = {
        "KTL": [c.strip() for c in (ktl or "").split(",") if c.strip()],
        "ETL": [c.strip() for c in (etl or "").split(",") if c.strip()],
        "TS":  [c.strip() for c in (ts  or "").split(",") if c.strip()],
    }

    async with pool.acquire() as conn:
        auction_id = await conn.fetchval("""
            INSERT INTO auctions (guild_id, project_channel_id, project_name, urgent)
            VALUES ($1, $2, $3, $4) RETURNING id
        """, str(interaction.guild_id), str(interaction.channel_id), project_name, urgent)

        rows = [
            (auction_id, ch, role)
            for role, chs in chapters.items()
            for ch in chs
        ]
        if rows:
            await conn.executemany(
                "INSERT INTO chapter_assignments (auction_id, chapter, role) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
                rows
            )

    embed = await build_embed(pool, auction_id)
    view  = AuctionView(bot)

    staff_role  = discord.utils.find(lambda r: r.name.lower() == "staff", interaction.guild.roles)
    mention_str = staff_role.mention if staff_role else "@staff"

    msg = await auction_ch.send(content=mention_str, embed=embed, view=view)

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE auctions SET auction_message_id=$1, auction_channel_id=$2 WHERE id=$3
        """, str(msg.id), str(auction_ch.id), auction_id)

    await interaction.followup.send(
        f"✅ Auction berhasil dibuat di {auction_ch.mention}!", ephemeral=True
    )


# ─────────────────────────────────────────────
# /ktldone  /etldone  /tsdone
# ─────────────────────────────────────────────
async def mark_done(interaction: discord.Interaction, role: str, chapter: str):
    pool       = interaction.client.pool
    channel_id = str(interaction.channel_id)

    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT ca.id, ca.assignee_id, ca.assignee_name, ca.auction_id,
                   a.project_channel_id, a.auction_channel_id
            FROM chapter_assignments ca
            JOIN auctions a ON a.id = ca.auction_id
            WHERE a.project_channel_id = $1
              AND ca.chapter            = $2
              AND ca.role               = $3
              AND ca.status             = 'claimed'
        """, channel_id, chapter, role)

    if not row:
        return await interaction.response.send_message(
            f"❌ Tidak ada **{role} Ch {chapter}** yang sedang dikerjakan di channel ini.",
            ephemeral=True
        )
    if row["assignee_id"] != str(interaction.user.id):
        return await interaction.response.send_message(
            f"❌ Chapter ini bukan milikmu (dikerjakan oleh **{row['assignee_name']}**).",
            ephemeral=True
        )

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE chapter_assignments SET status='done', done_at=NOW() WHERE id=$1",
            row["id"]
        )

    await interaction.response.send_message(
        f"✅ **{role} Ch {chapter}** selesai! Dikerjakan oleh {interaction.user.mention}."
    )

    await refresh_auction_message(interaction.client, row["auction_id"])

    # Chain notification
    next_role = {"KTL": "ETL", "ETL": "TS"}.get(role)
    if next_role:
        async with pool.acquire() as conn:
            next_row = await conn.fetchrow("""
                SELECT assignee_id, assignee_name, status
                FROM chapter_assignments
                WHERE auction_id=$1 AND chapter=$2 AND role=$3
            """, row["auction_id"], chapter, next_role)

        if next_row:
            if next_row["assignee_id"]:
                await interaction.channel.send(
                    f"📢 <@{next_row['assignee_id']}> **{role} Ch {chapter}** sudah selesai! "
                    f"Giliran kamu untuk **{next_role} Ch {chapter}**."
                )
            else:
                auction_ch_id = row["auction_channel_id"]
                ch_mention    = f"<#{auction_ch_id}>" if auction_ch_id else f"#{AUCTION_CHANNEL}"
                await interaction.channel.send(
                    f"⚠️ **{next_role} Ch {chapter}** belum ada yang ambil! "
                    f"Segera claim di {ch_mention}."
                )


@bot.tree.command(name="ktldone", description="Tandai KTL chapter selesai")
@app_commands.describe(chapter="Nomor chapter yang selesai")
async def ktldone(interaction: discord.Interaction, chapter: str):
    await mark_done(interaction, "KTL", chapter)


@bot.tree.command(name="etldone", description="Tandai ETL chapter selesai")
@app_commands.describe(chapter="Nomor chapter yang selesai")
async def etldone(interaction: discord.Interaction, chapter: str):
    await mark_done(interaction, "ETL", chapter)


@bot.tree.command(name="tsdone", description="Tandai TS chapter selesai")
@app_commands.describe(chapter="Nomor chapter yang selesai")
async def tsdone(interaction: discord.Interaction, chapter: str):
    await mark_done(interaction, "TS", chapter)


# ─────────────────────────────────────────────
# /progress
# ─────────────────────────────────────────────
@bot.tree.command(name="progress", description="Lihat progress chapter di project ini")
async def progress_cmd(interaction: discord.Interaction):
    pool       = interaction.client.pool
    channel_id = str(interaction.channel_id)

    async with pool.acquire() as conn:
        auction = await conn.fetchrow("""
            SELECT id, project_name FROM auctions
            WHERE project_channel_id=$1
            ORDER BY created_at DESC
            LIMIT 1
        """, channel_id)

    if not auction:
        return await interaction.response.send_message(
            "❌ Tidak ada auction aktif di channel ini.", ephemeral=True
        )

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT chapter, role, assignee_name, status
            FROM chapter_assignments
            WHERE auction_id=$1
            ORDER BY chapter::int NULLS LAST, chapter, role
        """, auction["id"])

    embed = discord.Embed(
        title=f"📊 Progress: {auction['project_name']}",
        color=discord.Color.green()
    )

    for role in ["KTL", "ETL", "TS"]:
        role_rows = [r for r in rows if r["role"] == role]
        if not role_rows:
            continue

        lines = []
        for r in role_rows:
            if r["status"] == "available":
                lines.append(f"⏳ Ch {r['chapter']}")
            elif r["status"] == "claimed":
                lines.append(f"🔒 Ch {r['chapter']} — {r['assignee_name']}")
            else:
                lines.append(f"✅ Ch {r['chapter']} — {r['assignee_name']}")

        done  = sum(1 for r in role_rows if r["status"] == "done")
        total = len(role_rows)
        embed.add_field(
            name=f"{role}  ({done}/{total} selesai)",
            value="\n".join(lines) or "—",
            inline=True
        )

    # Summary of fully completed chapters (all 3 roles done)
    all_chapters = list({r["chapter"] for r in rows})
    completed    = []
    for ch in sorted(all_chapters, key=lambda x: (int(x) if x.isdigit() else float("inf"), x)):
        ch_rows   = [r for r in rows if r["chapter"] == ch]
        if ch_rows and all(r["status"] == "done" for r in ch_rows):
            completed.append(f"✅ Ch {ch}")

    if completed:
        embed.add_field(name="🎉 Chapter Selesai Semua", value="\n".join(completed), inline=False)

    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────────
# Deadline reminder (every 30 min)
# ─────────────────────────────────────────────
@tasks.loop(minutes=30)
async def deadline_check():
    if not bot.pool:
        return
    now = datetime.now(timezone.utc)
    async with bot.pool.acquire() as conn:
        overdue = await conn.fetch("""
            SELECT ca.id, ca.assignee_id, ca.chapter, ca.role, a.project_channel_id, a.guild_id
            FROM chapter_assignments ca
            JOIN auctions a ON a.id = ca.auction_id
            WHERE ca.status = 'claimed' AND ca.deadline_at < $1
        """, now)

    for row in overdue:
        guild = bot.get_guild(int(row["guild_id"]))
        if not guild:
            continue
        ch = guild.get_channel(int(row["project_channel_id"]))
        if ch:
            await ch.send(
                f"⚠️ <@{row['assignee_id']}> deadline **{row['role']} Ch {row['chapter']}** sudah lewat! "
                f"Segera selesaikan atau hubungi PJ."
            )
            async with bot.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE chapter_assignments SET deadline_at=NULL WHERE id=$1", row["id"]
                )


bot.run(TOKEN)
