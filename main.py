# normalize_tree_fixed.py
import os
import re
import asyncio
import unicodedata
import logging
from typing import Optional

from dotenv import load_dotenv
import discord
from discord import app_commands
from discord.ext import commands
from keep_alive import keep_alive
load_dotenv()


from discord.ext import tasks


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("normalize_tree_fixed")

# ---------- Improved normalization utilities ----------
END_LETTER_RE = re.compile(r"LETTER.*?([A-Z])$")   # anchored to end of Unicode name

def char_to_ascii(ch: str) -> Optional[str]:
    """Map a single character to an ASCII letter if possible."""
    if ord(ch) < 128:
        return ch

    # 1) Try NFKC
    nfkc = unicodedata.normalize("NFKC", ch)
    if len(nfkc) == 1 and ord(nfkc) < 128:
        return nfkc

    # 2) Try NFKD + strip combining marks
    nfkd = unicodedata.normalize("NFKD", ch)
    stripped = "".join(c for c in nfkd if unicodedata.category(c)[0] != "M")
    if stripped and all(ord(c) < 128 for c in stripped):
        return stripped.lower() if stripped.isalpha() else stripped

    # 3) Fallback via Unicode names
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return None

    m = END_LETTER_RE.search(name)
    if m:
        base = m.group(1)
        low_flag = "SMALL" in name and "CAPITAL" not in name
        cap_flag = "CAPITAL" in name and "SMALL" not in name
        if low_flag:
            return base.lower()
        if cap_flag:
            return base.upper()
        return base.lower()

    return None

def remove_combining(ch: str) -> str:
    decomposed = unicodedata.normalize("NFKD", ch)
    return "".join(c for c in decomposed if unicodedata.category(c)[0] != "M")

def normalize_channel_name(original: str) -> str:
    out = []
    for ch in original:
        if ord(ch) < 128:
            out.append(ch)
            continue

        mapped = char_to_ascii(ch)
        if mapped:
            out.append(mapped)
            continue

        rc = remove_combining(ch)
        if rc and all(ord(c) < 128 for c in rc):
            out.append(rc)
            continue

        out.append(ch)

    result = "".join(out)

    # --- NEW RULE: Replace all 〢 with | ---
    result = result.replace("│","〢")

    # collapse multiple spaces
    result = re.sub(r"\s{2,}", " ", result).strip()

    return result

# ---------- BOT SETUP ----------
intents = discord.Intents.all()
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)
@tasks.loop(minutes=5)
async def refresh_presence():
    activity = discord.Activity(type=discord.ActivityType.watching, name=f"{len(bot.guilds)} servers")
    await bot.change_presence(status=discord.Status.online, activity=activity)

@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
        logger.info("Synced application commands")
    except Exception as e:
        logger.warning("Couldn't sync application commands: %s", e)
    logger.info(f"Logged in as {bot.user}")
    if not refresh_presence.is_running():
        refresh_presence.start()
        
# ---------- CONFIRMATION UI ----------
class ConfirmRenameView(discord.ui.View):
    def __init__(self, author_id: int, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("You can't confirm this — you didn't run the command.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # NOTE: interaction is first, button second
        self.confirmed = True
        # acknowledge and edit the original ephemeral message (or followup) 
        # If this was an ephemeral followup, edit_message is valid for the original message.
        await interaction.response.edit_message(content="Confirmed — starting rename...", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()

# ---------- SLASH COMMAND ----------
@bot.tree.command(name="normalize", description="Normalize channel names to ASCII.")
@app_commands.choices(mode=[
    app_commands.Choice(name="preview", value="preview"),
    app_commands.Choice(name="now", value="now"),
])
async def normalize(interaction: discord.Interaction, mode: Optional[app_commands.Choice[str]] = None):

    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message(
            "You need **Manage Channels** permission.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    channels = list(guild.channels)

    to_change = []
    for ch in channels:
        new = normalize_channel_name(ch.name)
        if ch.name != new:
            to_change.append((ch, ch.name, new))

    if not to_change:
        await interaction.followup.send("No channels need normalization.", ephemeral=True)
        return

    # Preview
    if mode and mode.value == "preview":
        lines = ["Channels that would be renamed:"]
        for ch, old, new in to_change:
            lines.append(f"- {ch.type.name:6} `{old}` → `{new}`")
        await interaction.followup.send("```\n" + "\n".join(lines) + "\n```", ephemeral=True)
        return

    # Require explicit mode:now
    if not mode or mode.value != "now":
        await interaction.followup.send(
            "To run renames, use `/normalize mode:now`.\nUse `/normalize mode:preview` to preview.",
            ephemeral=True
        )
        return

    # Confirmation buttons
    view = ConfirmRenameView(interaction.user.id)
    await interaction.followup.send(
        f"Found **{len(to_change)}** channels.\nConfirm to proceed:",
        ephemeral=True, view=view
    )
    await view.wait()

    if not view.confirmed:
        await interaction.followup.send("Cancelled.", ephemeral=True)
        return

    # Perform renaming
    renamed, failed = [], []
    for ch, old, new in to_change:
        try:
            await ch.edit(name=new, reason="Channel normalization")
            renamed.append((old, new))
            await asyncio.sleep(1)
        except Exception as e:
            failed.append((old, new, str(e)))

    summary = [f"Done. Success: {len(renamed)} | Failed: {len(failed)}"]
    for old, new in renamed[:20]:
        summary.append(f"`{old}` → `{new}`")
    if failed:
        summary.append("\nFailed:")
        for old, new, err in failed:
            summary.append(f"`{old}` → `{new}` ({err})")

    await interaction.followup.send("```\n" + "\n".join(summary) + "\n```", ephemeral=True)

if __name__ == "__main__":
    # start keep-alive server in background thread so host (like Replit) doesn't shut down
    keep_alive()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN missing in .env")
    bot.run(token)