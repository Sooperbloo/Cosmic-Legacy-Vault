import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import aiohttp
from typing import List

GUILD_ID = 1433635828105744418
STAFF_ROLE_ID = 1433637723603865661
MANAGER_ROLE_ID = 1433750015972605992
FORUM_CHANNEL_ID = 1503164907187867839
API_URL = "https://cryo-api-production.up.railway.app/api/items"
VAULT_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "vault.json")
FORUM_POSTS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "forum_posts.json")
CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")


def load_vault():
    with open(VAULT_FILE, "r") as f:
        return json.load(f)


def save_vault(data):
    with open(VAULT_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_forum_posts():
    if not os.path.exists(FORUM_POSTS_FILE):
        return {}
    with open(FORUM_POSTS_FILE, "r") as f:
        return json.load(f)


def save_forum_posts(data):
    with open(FORUM_POSTS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {"next_ticket_id": 70}
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_next_ticket_id() -> int:
    config = load_config()
    ticket_id = config.get("next_ticket_id", 70)
    config["next_ticket_id"] = ticket_id + 1
    save_config(config)
    return ticket_id


def is_staff(interaction: discord.Interaction) -> bool:
    role_ids = [r.id for r in interaction.user.roles]
    return STAFF_ROLE_ID in role_ids or MANAGER_ROLE_ID in role_ids


async def fetch_api_items():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception:
        pass
    return []


async def item_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    items = await fetch_api_items()
    names = [i["name"] for i in items]
    filtered = [n for n in names if current.lower() in n.lower()]
    return [app_commands.Choice(name=n, value=n) for n in filtered[:25]]


async def vault_item_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    vault = load_vault()
    items = [i["name"] for i in vault["items"] if i["quantity"] > 0]
    filtered = [i for i in items if current.lower() in i.lower()]
    return [app_commands.Choice(name=i, value=i) for i in filtered[:25]]


async def update_forum_posts(bot: discord.Client):
    """Update or create forum posts for each crate."""
    vault = load_vault()
    api_items = await fetch_api_items()
    api_map = {i["name"].lower(): i for i in api_items}

    # Group vault items by crate
    crate_items = {}
    for item in vault["items"]:
        if item["quantity"] <= 0:
            continue
        api_item = api_map.get(item["name"].lower())
        crate = api_item["crate"] if api_item else "Unknown"
        if crate not in crate_items:
            crate_items[crate] = []
        crate_items[crate].append(item)

    forum_posts = load_forum_posts()
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    forum = guild.get_channel(FORUM_CHANNEL_ID)
    if not forum or not isinstance(forum, discord.ForumChannel):
        return

    for crate, items in crate_items.items():
        # Group by item name, collecting all donors
        grouped = {}
        for item in items:
            name = item["name"]
            if name not in grouped:
                grouped[name] = []
            grouped[name].append({
                "qty": item["quantity"],
                "donor": item.get("donated_by", "Unknown")
            })

        lines = []
        for name in sorted(grouped.keys()):
            entries = grouped[name]
            total = sum(e["qty"] for e in entries)
            lines.append(f"- {name} x{total}")
            for e in entries:
                lines.append(f"  - x{e['qty']} donated by **{e['donor']}**")

        description = "\n".join(lines) if lines else "*No items available.*"

        embed = discord.Embed(
            title=f"📦 {crate}",
            description=description,
            color=discord.Color.purple()
        )
        embed.set_footer(text="Last updated automatically")

        if crate in forum_posts:
            # Update existing post
            try:
                thread = guild.get_thread(int(forum_posts[crate]))
                if thread:
                    async for msg in thread.history(limit=1, oldest_first=True):
                        await msg.edit(embed=embed)
                        break
                    continue
            except Exception:
                pass

        # Create new post
        try:
            thread, msg = await forum.create_thread(
                name=crate,
                embed=embed
            )
            forum_posts[crate] = str(thread.id)
            save_forum_posts(forum_posts)
        except Exception as e:
            print(f"Error creating forum post for {crate}: {e}")


class VaultCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="vault_add", description="Add an item to the vault inventory")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.autocomplete(item=item_autocomplete)
    @app_commands.describe(item="Item name", quantity="Quantity to add", donated_by="Who donated this? (leave blank if you are the donor)", ticket="Ticket number (leave blank to auto-assign)")
    async def vault_add(self, interaction: discord.Interaction, item: str, quantity: int, donated_by: str = None, ticket: int = None):
        if not is_staff(interaction):
            await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)
            return
        if quantity < 1:
            await interaction.response.send_message("Quantity must be at least 1.", ephemeral=True)
            return

        donor = donated_by or interaction.user.display_name
        ticket_id = ticket if ticket is not None else get_next_ticket_id()

        vault = load_vault()
        existing = next((i for i in vault["items"] if i["name"].lower() == item.lower()), None)
        if existing:
            existing["quantity"] += quantity
            if "donated_by" not in existing:
                existing["donated_by"] = donor
            if "ticket" not in existing:
                existing["ticket"] = ticket_id
        else:
            vault["items"].append({"name": item, "quantity": quantity, "donated_by": donor, "ticket": ticket_id})
        save_vault(vault)

        await update_forum_posts(self.bot)

        embed = discord.Embed(
            title="✅ Vault Updated",
            description=f"Added **{quantity}x {item}** to the vault.\nDonated by: **{donor}**\nTicket: **#{ticket_id}**",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="vault_remove", description="Remove an item from the vault inventory")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.autocomplete(item=vault_item_autocomplete)
    @app_commands.describe(item="Item name", quantity="Quantity to remove")
    async def vault_remove(self, interaction: discord.Interaction, item: str, quantity: int):
        if not is_staff(interaction):
            await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)
            return

        vault = load_vault()
        existing = next((i for i in vault["items"] if i["name"].lower() == item.lower()), None)
        if not existing:
            await interaction.response.send_message(f"**{item}** is not in the vault.", ephemeral=True)
            return

        existing["quantity"] = max(0, existing["quantity"] - quantity)
        save_vault(vault)

        await update_forum_posts(self.bot)

        embed = discord.Embed(
            title="✅ Vault Updated",
            description=f"Removed **{quantity}x {item}** from the vault.",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="vault", description="View the current vault inventory")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def vault_view(self, interaction: discord.Interaction):
        vault = load_vault()
        items = [i for i in vault["items"] if i["quantity"] > 0]

        if not items:
            await interaction.response.send_message("The vault is currently empty.", ephemeral=True)
            return

        api_items = await fetch_api_items()
        api_map = {i["name"].lower(): i for i in api_items}

        embed = discord.Embed(title="🏦 CryoVault Inventory", color=discord.Color.purple())
        for item in sorted(items, key=lambda x: x["name"]):
            api_item = api_map.get(item["name"].lower())
            crate = api_item["crate"] if api_item else "Unknown"
            donor = item.get("donated_by", "Unknown")
            embed.add_field(
                name=f"{item['name']} (x{item['quantity']})",
                value=f"Crate: {crate}\nDonated by: {donor}",
                inline=True
            )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="vault_refresh", description="Manually refresh the forum vault posts")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def vault_refresh(self, interaction: discord.Interaction):
        if not is_staff(interaction):
            await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await update_forum_posts(self.bot)
        await interaction.followup.send("Forum posts refreshed!", ephemeral=True)


async def setup(bot):
    await bot.add_cog(VaultCog(bot))
