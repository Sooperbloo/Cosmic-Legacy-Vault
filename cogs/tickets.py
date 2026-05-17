import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import aiohttp
import difflib
import re
from datetime import datetime, timedelta
from discord.ext import tasks
from typing import List, Optional

GUILD_ID = 1433635828105744418
STAFF_ROLE_ID = 1433637723603865661
MANAGER_ROLE_ID = 1433750015972605992
TICKET_CHANNEL_ID = 1503164208555233391
TICKET_CATEGORY_ID = 1434220836914729144
VAULT_RULES_CHANNEL_ID = 1433752518919323648
BORROWED_CHANNEL_ID = 1503412731305267341
LOGS_CHANNEL_ID = 1504949909206597642
TICKETS_LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tickets_log.json")
OVERDUE_CHANNEL_ID = 1503412816923721769
MISSING_CHANNEL_ID = 1434221212174778530
CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")


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
API_URL = "https://cryo-api-production.up.railway.app/api/items"
VAULT_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "vault.json")
BORROWS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "borrows.json")

# Store pending IGN confirmations {channel_id: {items, donor_id, message_id}}
pending_agreements = {}

# Store borrow sessions {channel_id: {borrower_id, items, list_message_id}}
borrow_sessions = {}

# Store active borrows {borrow_message_id: {items, borrower_id, ign, return_ts, ticket_channel_id}}
active_borrows = {}


def load_vault():
    with open(VAULT_FILE, "r") as f:
        return json.load(f)


def save_vault(data):
    with open(VAULT_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_borrows():
    if not os.path.exists(BORROWS_FILE):
        return {}
    with open(BORROWS_FILE, "r") as f:
        data = json.load(f)
    # Convert string keys back to int
    return {int(k): v for k, v in data.items()}


def save_borrows():
    with open(BORROWS_FILE, "w") as f:
        json.dump({str(k): v for k, v in active_borrows.items()}, f, indent=2)


# Load persisted borrows on startup
active_borrows.update(load_borrows())


def load_ticket_log():
    if not os.path.exists(TICKETS_LOG_FILE):
        return []
    with open(TICKETS_LOG_FILE, "r") as f:
        return json.load(f)


def save_ticket_log(log):
    with open(TICKETS_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)


def add_ticket_log(entry: dict):
    """Add a ticket to the log, pruning entries older than 2 weeks."""
    log = load_ticket_log()
    log.append(entry)
    cutoff = datetime.now().timestamp() - (14 * 24 * 60 * 60)
    log = [e for e in log if e.get("timestamp", 0) > cutoff]
    save_ticket_log(log)


async def post_ticket_log(bot, entry: dict):
    """Post a ticket log entry to the logs channel."""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    channel = guild.get_channel(LOGS_CHANNEL_ID)
    if not channel:
        return

    ticket_type = entry.get("type", "Unknown")
    user_id = entry.get("user_id")
    ticket_id = entry.get("ticket_id")
    ts = entry.get("timestamp", 0)
    summary = entry.get("summary", "No summary available.")

    color = discord.Color.green() if ticket_type == "Donate" else discord.Color.blurple()
    embed = discord.Embed(
        title=f"{'📦' if ticket_type == 'Donate' else '❗'} {ticket_type} Ticket #{ticket_id}",
        color=color
    )
    embed.add_field(name="User", value=f"<@{user_id}>", inline=True)
    embed.add_field(name="Date", value=f"<t:{int(ts)}:D>", inline=True)
    embed.add_field(name="Type", value=ticket_type, inline=True)
    items = entry.get("items", [])
    if items:
        items_text = "\n".join(f"• {qty}x {name}" for name, qty in items)
        embed.add_field(name="Items", value=items_text[:1024], inline=False)

    await channel.send(embed=embed, view=TicketLogDetailView(entry))


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


def fuzzy_match(query: str, items: List[str], threshold: float = 0.6) -> Optional[str]:
    query_lower = query.lower().strip()
    items_lower = [i.lower() for i in items]
    if query_lower in items_lower:
        return items[items_lower.index(query_lower)]
    matches = difflib.get_close_matches(query_lower, items_lower, n=1, cutoff=threshold)
    if matches:
        return items[items_lower.index(matches[0])]
    return None


def parse_donation_text(text: str) -> List[tuple]:
    results = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.match(r'^(.+?)\s*[xX\xd7]\s*(\d+)\s*$', line)
        if match:
            results.append((match.group(1).strip(), int(match.group(2))))
        else:
            results.append((line.strip(), 1))
    return results


def build_agreement(ign: str, items: List[tuple], collection_date: str, return_date: str, rules_channel_id: int) -> str:
    items_list = "\n".join(f"* {qty}x {name}" for name, qty in items)
    ign_display = ign if ign else "[Awaiting IGN]"
    return (
        f"This discord message serves as a binding agreement between\n\n"
        f"* **{ign_display}** and /pw Vault and The Cryo Community Vault\n\n\n"
        f"The Cryo Community Vault have agreed to loan the following items\n\n"
        f"{items_list}\n"
        f"to **{ign_display}**, on the CryoMC Server.\n\n\n"
        f"These items remain the property of /pw Vault and The Cryo Community Vault; and will need to be returned "
        f"to the hopper at the drop-off point at /pw Vault. In agreeing to this, you agree to the rules published "
        f"in the vault discord server here: <#{rules_channel_id}>\n\n"
        f"You accept full responsibility for the safekeeping of these items until you have been notified by a vault "
        f"staff member that they have been returned to the vault. Kindly ensure you have /antidrop enabled in game "
        f"whilst using vault items. Should these items not be returned when they are due; you give The Cryo Community "
        f"Vault team permission to open a ticket, and have these items taken from your inventory and returned to "
        f"/pw Vault by the CryoMC server team.\n\n\n"
        f"Collection: **{collection_date}**\n"
        f"Return: **{return_date}**\n\n\n"
        f"__Please reply with your in-game username to confirm your agreement__, and meet at the pick-up point at "
        f"/pw Vault, to collect the items from our vault staff.\n\n"
        f"If you are happy with the service you have received from the awesome Vault Staff team, please feel free "
        f"to tip them using the /pay (username) amount command in game"
    )


def build_borrow_list_embed(items: List[tuple]) -> discord.Embed:
    desc = "\n".join(f"• **{qty}x {name}**" for name, qty in items) if items else "*No items added yet.*"
    embed = discord.Embed(
        title="🛒 Your Borrow List",
        description=desc + "\n\nUse `/borrow_item` to add more items, or click **Send Agreement** when ready.",
        color=discord.Color.blurple()
    )
    return embed


class BorrowListView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📜 Send Agreement", style=discord.ButtonStyle.green, custom_id="send_agreement")
    async def send_agreement(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel_id = interaction.channel.id
        if channel_id not in borrow_sessions:
            await interaction.response.send_message("No borrow session found. Use `/borrow_item` first.", ephemeral=True)
            return

        session = borrow_sessions[channel_id]
        if interaction.user.id != session["borrower_id"]:
            await interaction.response.send_message("This isn't your borrow list!", ephemeral=True)
            return

        if not session["items"]:
            await interaction.response.send_message("Add at least one item before sending.", ephemeral=True)
            return

        # Disable the list message buttons
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

        # Post agreement with blank IGN
        items = session["items"]
        agreement_text = build_agreement("", items, "", "", VAULT_RULES_CHANNEL_ID)
        msg = await interaction.channel.send(agreement_text)

        # Small delay so agreement renders before the prompt
        import asyncio
        await asyncio.sleep(1)

        await interaction.channel.send(
            f"<@{interaction.user.id}> **Please reply with your in-game username** to confirm your agreement."
        )

        pending_agreements[channel_id] = {
            "borrower_id": session["borrower_id"],
            "items": items,
            "message_id": msg.id
        }
        del borrow_sessions[channel_id]

    @discord.ui.button(label="🗑️ Clear List", style=discord.ButtonStyle.red, custom_id="clear_borrow_list")
    async def clear_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel_id = interaction.channel.id
        if channel_id not in borrow_sessions:
            await interaction.response.send_message("No borrow session found.", ephemeral=True)
            return
        if interaction.user.id != borrow_sessions[channel_id]["borrower_id"]:
            await interaction.response.send_message("This isn't your borrow list!", ephemeral=True)
            return
        borrow_sessions[channel_id]["items"] = []
        embed = build_borrow_list_embed([])
        await interaction.response.edit_message(embed=embed, view=self)


class TicketLogDetailView(discord.ui.View):
    def __init__(self, entry: dict):
        super().__init__(timeout=None)
        self.entry = entry

    @discord.ui.button(label="📋 View Details", style=discord.ButtonStyle.grey, custom_id="ticket_log_detail")
    async def view_details(self, interaction: discord.Interaction, button: discord.ui.Button):
        e = self.entry
        ticket_type = e.get("type", "Unknown")
        items = e.get("items", [])
        outcome = e.get("outcome", "Unknown")
        ts = e.get("timestamp", 0)
        ign = e.get("ign", "N/A")

        desc = f"**Type:** {ticket_type}\n"
        desc += f"**User:** <@{e.get('user_id')}>\n"
        desc += f"**Date:** <t:{int(ts)}:F>\n"
        desc += f"**Ticket ID:** #{e.get('ticket_id')}\n"
        desc += f"**Outcome:** {outcome}\n"
        if ign != "N/A":
            desc += f"**IGN:** {ign}\n"
        if items:
            desc += "\n**Items:**\n" + "\n".join(f"• {qty}x {name}" for name, qty in items)

        embed = discord.Embed(
            title=f"Ticket #{e.get('ticket_id')} Details",
            description=desc,
            color=discord.Color.green() if ticket_type == "Donate" else discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class MarkReturnedView(discord.ui.View):
    """Posted in ticket — player clicks to say they have returned the items."""
    def __init__(self, borrower_id: int, borrow_msg_id: int):
        super().__init__(timeout=None)
        self.borrower_id = borrower_id
        self.borrow_msg_id = borrow_msg_id

    @discord.ui.button(label="📦 I Have Returned the Items", style=discord.ButtonStyle.green, custom_id="player_returned")
    async def player_returned(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.borrower_id:
            await interaction.response.send_message("Only the borrower can mark items as returned.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

        # Update the borrowed items channel message
        guild = interaction.guild
        borrowed_channel = guild.get_channel(BORROWED_CHANNEL_ID)
        if borrowed_channel and self.borrow_msg_id in active_borrows:
            try:
                borrow_msg = await borrowed_channel.fetch_message(self.borrow_msg_id)
                borrow_data = active_borrows[self.borrow_msg_id]
                desc = "\n".join(f"• **{qty}x {name}**" for name, qty in borrow_data["items"])
                embed = discord.Embed(
                    title="📦 Player Claims Return",
                    description=(
                        f"**{borrow_data['ign']}** (<@{borrow_data['borrower_id']}>) says they have returned:\n\n{desc}\n\n"
                        f"Return due: <t:{borrow_data['return_ts']}:F>"
                    ),
                    color=discord.Color.yellow()
                )
                await borrow_msg.edit(embed=embed, view=StaffReturnConfirmView(self.borrow_msg_id))
            except Exception as e:
                print(f"Error updating borrowed channel: {e}")

        await interaction.channel.send("✅ Thank you! Staff will confirm your return shortly.")


class StaffReturnConfirmView(discord.ui.View):
    """Posted in borrowed items channel — staff confirm/deny/flag missing."""
    def __init__(self, borrow_msg_id: int):
        super().__init__(timeout=None)
        self.borrow_msg_id = borrow_msg_id

    @discord.ui.button(label="✅ Confirm Return", style=discord.ButtonStyle.green, custom_id="staff_confirm_return")
    async def confirm_return(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction):
            await interaction.response.send_message("Only staff can confirm returns.", ephemeral=True)
            return
        await interaction.response.defer()

        borrow_data = active_borrows.pop(self.borrow_msg_id, None)
        save_borrows()
        if borrow_data:
            # Add items back to vault
            vault = load_vault()
            for name, qty in borrow_data["items"]:
                existing = next((i for i in vault["items"] if i["name"].lower() == name.lower() and i["donated_by"] == borrow_data["ign"]), None)
                if existing:
                    existing["quantity"] += qty
                else:
                    vault["items"].append({"name": name, "quantity": qty, "donated_by": borrow_data["ign"], "ticket": borrow_data.get("ticket_id")})
            save_vault(vault)
            from cogs.vault import update_forum_posts
            await update_forum_posts(interaction.client)

        desc = "\n".join(f"• **{qty}x {name}**" for name, qty in (borrow_data["items"] if borrow_data else []))
        embed = discord.Embed(
            title="✅ Return Confirmed",
            description=f"Items returned and vault updated.\n\n{desc}",
            color=discord.Color.green()
        )
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="❌ Deny Return", style=discord.ButtonStyle.red, custom_id="staff_deny_return")
    async def deny_return(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction):
            await interaction.response.send_message("Only staff can deny returns.", ephemeral=True)
            return
        await interaction.response.defer()

        borrow_data = active_borrows.get(self.borrow_msg_id)
        embed = discord.Embed(
            title="❌ Return Denied",
            description=f"Return denied for **{borrow_data['ign'] if borrow_data else 'Unknown'}**. Please follow up with the player.",
            color=discord.Color.red()
        )
        # Restore player button in ticket
        if borrow_data:
            try:
                guild = interaction.guild
                ticket_channel = guild.get_channel(borrow_data["ticket_channel_id"])
                if ticket_channel:
                    await ticket_channel.send(
                        f"<@{borrow_data['borrower_id']}> Your return was denied by staff. Please ensure the items are returned to /pw Vault.",
                        view=MarkReturnedView(borrow_data["borrower_id"], self.borrow_msg_id)
                    )
            except Exception as e:
                print(f"Error sending denial message: {e}")

        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="⚠️ Missing", style=discord.ButtonStyle.grey, custom_id="staff_flag_missing")
    async def flag_missing(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction):
            await interaction.response.send_message("Only staff can flag items as missing.", ephemeral=True)
            return
        await interaction.response.defer()

        borrow_data = active_borrows.pop(self.borrow_msg_id, None)
        save_borrows()
        desc = "\n".join(f"• **{qty}x {name}**" for name, qty in (borrow_data["items"] if borrow_data else []))

        # Post to manager missing channel
        guild = interaction.guild
        missing_channel = guild.get_channel(MISSING_CHANNEL_ID)
        if missing_channel and borrow_data:
            missing_embed = discord.Embed(
                title="⚠️ Missing Items",
                description=(
                    f"Items borrowed by **{borrow_data['ign']}** (<@{borrow_data['borrower_id']}>) have been reported missing:\n\n{desc}\n\n"
                    f"Flagged by <@{interaction.user.id}>"
                ),
                color=discord.Color.orange()
            )
            await missing_channel.send(embed=missing_embed)

        embed = discord.Embed(
            title="⚠️ Flagged as Missing",
            description=f"Items have been flagged as missing and reported to managers.\n\n{desc}",
            color=discord.Color.orange()
        )
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(embed=embed, view=self)


class ConfirmCollectionView(discord.ui.View):
    def __init__(self, items: List[tuple], borrower_id: int, ign: str, agreement_message_id: int):
        super().__init__(timeout=None)
        self.items = items
        self.borrower_id = borrower_id
        self.ign = ign
        self.agreement_message_id = agreement_message_id

    @discord.ui.button(label="✅ Confirm Collection", style=discord.ButtonStyle.green, custom_id="confirm_collection")
    async def confirm_collection(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction):
            await interaction.response.send_message("Only staff can confirm collection.", ephemeral=True)
            return
        await interaction.response.defer()

        # Update timestamps from NOW (when item was actually collected)
        now = datetime.now()
        return_dt = now + timedelta(days=3)
        collection = f"<t:{int(now.timestamp())}:F>"
        return_date = f"<t:{int(return_dt.timestamp())}:F>"

        # Update the agreement message with real timestamps
        try:
            msg = await interaction.channel.fetch_message(self.agreement_message_id)
            updated_text = build_agreement(self.ign, self.items, collection, return_date, VAULT_RULES_CHANNEL_ID)
            await msg.edit(content=updated_text)
        except Exception:
            pass

        # Deduct from vault across all matching entries
        vault = load_vault()
        for name, qty in self.items:
            remaining = qty
            for entry in vault["items"]:
                if entry["name"].lower() == name.lower() and remaining > 0:
                    deduct = min(entry["quantity"], remaining)
                    entry["quantity"] -= deduct
                    remaining -= deduct
        save_vault(vault)
        from cogs.vault import update_forum_posts
        await update_forum_posts(interaction.client)

        # Log the borrow
        import time
        entry = {
            "type": "Borrow",
            "user_id": self.borrower_id,
            "ticket_id": None,
            "timestamp": time.time(),
            "items": self.items,
            "outcome": "Active",
            "ign": self.ign
        }
        add_ticket_log(entry)
        await post_ticket_log(interaction.client, entry)

        # Post to borrowed items channel
        guild = interaction.guild
        borrowed_channel = guild.get_channel(BORROWED_CHANNEL_ID)
        desc = "\n".join(f"• **{qty}x {name}**" for name, qty in self.items)
        borrow_embed = discord.Embed(
            title="🔄 Active Borrow",
            description=(
                f"**{self.ign}** (<@{self.borrower_id}>) has borrowed:\n\n{desc}\n\n"
                f"Return due: <t:{int(return_dt.timestamp())}:F>"
            ),
            color=discord.Color.blurple()
        )
        borrow_msg = await borrowed_channel.send(embed=borrow_embed)

        # Store in active_borrows
        active_borrows[borrow_msg.id] = {
            "items": self.items,
            "borrower_id": self.borrower_id,
            "ign": self.ign,
            "return_ts": int(return_dt.timestamp()),
            "ticket_channel_id": interaction.channel.id
        }
        save_borrows()

        # Post return button in ticket
        await interaction.channel.send(
            f"<@{self.borrower_id}> When you have returned the items to /pw Vault, click the button below.",
            view=MarkReturnedView(self.borrower_id, borrow_msg.id)
        )

        embed = discord.Embed(
            title="✅ Items Collected",
            description=f"**{self.ign}** has collected their items. Vault inventory updated.\nReturn due: <t:{int(return_dt.timestamp())}:F>",
            color=discord.Color.green()
        )
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red, custom_id="cancel_collection")
    async def cancel_collection(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction):
            await interaction.response.send_message("Only staff can cancel.", ephemeral=True)
            return
        embed = discord.Embed(title="❌ Cancelled", description="This borrow request has been cancelled.", color=discord.Color.red())
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)



class DonateModal(discord.ui.Modal, title="Donate Items to the Vault"):
    items_input = discord.ui.TextInput(
        label="Items to Donate",
        style=discord.TextStyle.paragraph,
        placeholder="Baldur's Staff x2\nNorth Star x1\nWhispering Acorn x3",
        required=True,
        max_length=1000
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        parsed = parse_donation_text(self.items_input.value)
        if not parsed:
            await interaction.followup.send("Couldn't parse any items. Use the format `Item Name x2`.", ephemeral=True)
            return

        api_items = await fetch_api_items()
        api_names = [i["name"] for i in api_items]

        exact, fuzzy, not_found = [], [], []
        for name, qty in parsed:
            match = fuzzy_match(name, api_names, threshold=0.6)
            if match is None:
                not_found.append((name, qty))
            elif match.lower() == name.lower():
                exact.append((match, qty))
            else:
                fuzzy.append((name, qty, match))

        session = {
            "exact": exact, "fuzzy": fuzzy, "not_found": not_found,
            "pending_fuzzy_index": 0, "donor_id": interaction.user.id,
            "confirmed_items": list(exact)
        }

        if not_found:
            nf_list = "\n".join(f"• {n} x{q}" for n, q in not_found)
            await interaction.channel.send(embed=discord.Embed(
                title="❓ Items Not Found",
                description=f"The following items couldn't be matched:\n{nf_list}\n\nPlease check the spelling.",
                color=discord.Color.red()
            ))

        donor_name = interaction.user.display_name

        if fuzzy:
            session["donor_name"] = donor_name
            name, qty, match = fuzzy[0]
            embed = discord.Embed(
                title="Did you mean...?",
                description=f"You said you wanted to donate **{qty}x {name}**.\nDid you mean **{match}**?",
                color=discord.Color.yellow()
            )
            await interaction.channel.send(embed=embed, view=FuzzyConfirmView(session, interaction.channel))
            await interaction.followup.send("Please check the messages in your ticket.", ephemeral=True)
        elif exact:
            await send_dropoff_instructions(interaction.channel, exact, interaction.user.id, donor_name, ticket_id=None)
            await interaction.followup.send("Please drop off your items and click the button when done!", ephemeral=True)
        else:
            await interaction.followup.send("No valid items found to donate.", ephemeral=True)


class FuzzyConfirmView(discord.ui.View):
    def __init__(self, session: dict, channel):
        super().__init__(timeout=120)
        self.session = session
        self.channel = channel

    @discord.ui.button(label="✅ Yes", style=discord.ButtonStyle.green)
    async def yes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.session["donor_id"]:
            await interaction.response.send_message("This isn't your ticket!", ephemeral=True)
            return
        idx = self.session["pending_fuzzy_index"]
        _, qty, match = self.session["fuzzy"][idx]
        self.session["confirmed_items"].append((match, qty))
        self.session["pending_fuzzy_index"] += 1
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await self.next_or_finish()

    @discord.ui.button(label="❌ No", style=discord.ButtonStyle.red)
    async def no_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.session["donor_id"]:
            await interaction.response.send_message("This isn't your ticket!", ephemeral=True)
            return
        self.session["pending_fuzzy_index"] += 1
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await self.channel.send("Item skipped. Let staff know if you'd like to add it manually.")
        await self.next_or_finish()

    async def next_or_finish(self):
        idx = self.session["pending_fuzzy_index"]
        if idx < len(self.session["fuzzy"]):
            name, qty, match = self.session["fuzzy"][idx]
            embed = discord.Embed(
                title="Did you mean...?",
                description=f"You said you wanted to donate **{qty}x {name}**.\nDid you mean **{match}**?",
                color=discord.Color.yellow()
            )
            await self.channel.send(embed=embed, view=FuzzyConfirmView(self.session, self.channel))
        else:
            if self.session["confirmed_items"]:
                donor_name = self.session.get("donor_name", "Unknown")
                await send_dropoff_instructions(self.channel, self.session["confirmed_items"], self.session["donor_id"], donor_name)
            else:
                await self.channel.send("No items confirmed for donation.")


async def send_dropoff_instructions(channel, items: List[tuple], donor_id: int, donor_name: str, ticket_id: int = None):
    desc = "\n".join(f"• **{qty}x {name}**" for name, qty in items)
    embed = discord.Embed(
        title="📦 Drop Off Your Items",
        description=(
            f"Thank you for donating the following items:\n\n{desc}\n\n"
            "**How to drop off:**\n"
            "Type `/pw Vault` and drop the items (preferably in a shulker box for large donations) "
            "into the hoppers on the left hand side after teleporting.\n\n"
            "Once you have dropped off your items, click the button below."
        ),
        color=discord.Color.green()
    )
    await channel.send(embed=embed, view=DropOffView(items, donor_id, donor_name, ticket_id))


async def send_donation_summary(channel, items: List[tuple], donor_id: int, donor_name: str = None, ticket_id: int = None):
    desc = "\n".join(f"• **{qty}x {name}**" for name, qty in items)
    embed = discord.Embed(
        title="📦 Donation Request",
        description=f"<@{donor_id}> would like to donate:\n\n{desc}\n\nPlease confirm once items are received in the vault.",
        color=discord.Color.green()
    )
    if donor_name:
        embed.set_footer(text=f"Donated by: {donor_name}")
    await channel.send(content=f"<@&{STAFF_ROLE_ID}>", embed=embed, view=ConfirmDonationView(items, donor_id, donor_name, ticket_id))


class DropOffView(discord.ui.View):
    def __init__(self, items: List[tuple], donor_id: int, donor_name: str, ticket_id: int = None):
        super().__init__(timeout=None)
        self.items = items
        self.donor_id = donor_id
        self.donor_name = donor_name
        self.ticket_id = ticket_id

    @discord.ui.button(label="✅ Items Dropped Off", style=discord.ButtonStyle.green, custom_id="items_dropped_off")
    async def dropped_off(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.donor_id:
            await interaction.response.send_message("Only the donor can mark items as dropped off.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await send_donation_summary(interaction.channel, self.items, self.donor_id, self.donor_name, self.ticket_id)


class ConfirmDonationView(discord.ui.View):
    def __init__(self, items: List[tuple], donor_id: int, donor_name: str = None, ticket_id: int = None):
        super().__init__(timeout=None)
        self.items = items
        self.donor_id = donor_id
        self.donor_name = donor_name or "Unknown"
        self.ticket_id = ticket_id

    @discord.ui.button(label="✅ Confirm Donation", style=discord.ButtonStyle.green, custom_id="confirm_donation")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction):
            await interaction.response.send_message("Only staff can confirm donations.", ephemeral=True)
            return
        from cogs.vault import update_forum_posts
        vault = load_vault()
        for name, qty in self.items:
            existing = next((i for i in vault["items"] if i["name"].lower() == name.lower()), None)
            if existing:
                existing["quantity"] += qty
                if "donated_by" not in existing:
                    existing["donated_by"] = self.donor_name
            else:
                vault["items"].append({"name": name, "quantity": qty, "donated_by": self.donor_name, "ticket": self.ticket_id})
        save_vault(vault)
        await update_forum_posts(interaction.client)

        # Log the donation
        import time
        entry = {
            "type": "Donate",
            "user_id": self.donor_id,
            "ticket_id": self.ticket_id,
            "timestamp": time.time(),
            "items": self.items,
            "outcome": "Confirmed",
            "ign": "N/A"
        }
        add_ticket_log(entry)
        await post_ticket_log(interaction.client, entry)

        desc = "\n".join(f"• **{qty}x {name}**" for name, qty in self.items)
        embed = discord.Embed(
            title="✅ Donation Confirmed",
            description=f"Added to the vault:\n\n{desc}\n\nThank you <@{self.donor_id}>!",
            color=discord.Color.green()
        )
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.red, custom_id="reject_donation")
    async def reject_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction):
            await interaction.response.send_message("Only staff can reject donations.", ephemeral=True)
            return
        embed = discord.Embed(title="❌ Donation Rejected", description="This donation has been rejected.", color=discord.Color.red())
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)


class OpenDonateModalButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📦 Submit Donation", style=discord.ButtonStyle.green, custom_id="open_donate_modal")
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DonateModal())


class TicketTypeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🪙 Donate", style=discord.ButtonStyle.green, custom_id="ticket_donate")
    async def donate_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket(interaction, "donate")

    @discord.ui.button(label="❗ Request", style=discord.ButtonStyle.blurple, custom_id="ticket_borrow")
    async def borrow_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket(interaction, "request")


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Close Ticket", style=discord.ButtonStyle.red, custom_id="ticket_close")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction):
            await interaction.response.send_message("Only staff can close tickets.", ephemeral=True)
            return
        await interaction.response.send_message("Closing ticket...")
        await interaction.channel.delete()


async def vault_item_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    vault = load_vault()
    # Sum quantities across all entries
    totals = {}
    for i in vault["items"]:
        if i["quantity"] > 0:
            totals[i["name"]] = totals.get(i["name"], 0) + i["quantity"]
    # Subtract what's already in the borrow session
    session = borrow_sessions.get(interaction.channel_id, {})
    for name, qty in session.get("items", []):
        if name in totals:
            totals[name] = max(0, totals[name] - qty)
    # Only show items with stock remaining
    filtered = [name for name in totals if totals[name] > 0 and current.lower() in name.lower()]
    return [app_commands.Choice(name=f"{name} (x{totals[name]})", value=name) for name in filtered[:25]]


async def create_ticket(interaction: discord.Interaction, ticket_type: str):
    guild = interaction.guild
    category = guild.get_channel(TICKET_CATEGORY_ID)
    staff_role = guild.get_role(STAFF_ROLE_ID)
    manager_role = guild.get_role(MANAGER_ROLE_ID)

    existing = discord.utils.get(
        guild.text_channels,
        name=f"{ticket_type}-{interaction.user.name.lower().replace(' ', '-')}"
    )
    if existing:
        await interaction.response.send_message(f"You already have an open ticket: {existing.mention}", ephemeral=True)
        return

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        staff_role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        manager_role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }

    ticket_id = get_next_ticket_id()
    channel_name = f"{ticket_type}-{interaction.user.name.lower().replace(' ', '-')}"
    channel = await guild.create_text_channel(name=channel_name, category=category, overwrites=overwrites)

    if ticket_type == "donate":
        embed = discord.Embed(
            title=f"📦 Donation Ticket #{ticket_id}",
            description=(
                f"Welcome {interaction.user.mention}!\n\n"
                "Click the button below to submit the items you'd like to donate.\n"
                "Use the format:\n```\nItem Name x2\nAnother Item x1\n```\n"
                "A staff member will confirm your donation once the items are received."
            ),
            color=discord.Color.green()
        )
        await channel.send(content=f"{interaction.user.mention} | <@&{STAFF_ROLE_ID}>", embed=embed, view=OpenDonateModalButton())
        await channel.send(view=CloseTicketView())
    else:
        embed = discord.Embed(
            title=f"❗ Request Ticket #{ticket_id}",
            description=(
                f"Welcome {interaction.user.mention}!\n\n"
                "Use `/borrow_item` to add items to your borrow list.\n"
                "Check the vault forum channel to see what\'s available.\n\n"
                "When you\'re ready, click **Send Agreement** to proceed."
            ),
            color=discord.Color.blurple()
        )
        await channel.send(content=f"{interaction.user.mention} | <@&{STAFF_ROLE_ID}>", embed=embed, view=CloseTicketView())

    await interaction.response.send_message(f"Your ticket has been created: {channel.mention}", ephemeral=True)


class TicketsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.add_view(TicketTypeView())
        self.bot.add_view(CloseTicketView())
        self.bot.add_view(OpenDonateModalButton())
        self.bot.add_view(BorrowListView())
        self.bot.add_view(StaffReturnConfirmView(0))
        self.check_overdue.start()

    def cog_unload(self):
        self.check_overdue.cancel()

    @tasks.loop(minutes=30)
    async def check_overdue(self):
        """Update the overdue channel with borrows past their return date."""
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return
        overdue_channel = guild.get_channel(OVERDUE_CHANNEL_ID)
        if not overdue_channel:
            return

        now = datetime.now().timestamp()
        overdue = {msg_id: data for msg_id, data in active_borrows.items() if data["return_ts"] < now}

        if not overdue:
            return

        # Clear old overdue messages and repost
        try:
            await overdue_channel.purge(limit=50)
        except Exception:
            pass

        for msg_id, data in overdue.items():
            desc = "\n".join(f"• **{qty}x {name}**" for name, qty in data["items"])
            embed = discord.Embed(
                title="⏰ Overdue Return",
                description=(
                    f"**{data['ign']}** (<@{data['borrower_id']}>) has not returned:\n\n{desc}\n\n"
                    f"Was due: <t:{data['return_ts']}:F>"
                ),
                color=discord.Color.red()
            )
            await overdue_channel.send(embed=embed)

    @check_overdue.before_loop
    async def before_check_overdue(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        channel_id = message.channel.id
        if channel_id not in pending_agreements:
            return

        agreement = pending_agreements[channel_id]
        if message.author.id != agreement["borrower_id"]:
            return

        ign = message.content.strip()
        if not ign or len(ign) > 32 or " " in ign:
            await message.channel.send("Please reply with just your in-game username (no spaces).", delete_after=10)
            return

        items = agreement["items"]

        # Update the agreement message with IGN but keep timestamps blank (filled on collection)
        agreement_text = build_agreement(ign, items, "", "", VAULT_RULES_CHANNEL_ID)
        try:
            original_msg = await message.channel.fetch_message(agreement["message_id"])
            await original_msg.edit(content=agreement_text)
        except Exception:
            pass

        # Remove from pending
        del pending_agreements[channel_id]

        # Ping staff to confirm collection
        desc = "\n".join(f"• **{qty}x {name}**" for name, qty in items)
        embed = discord.Embed(
            title="❗ Ready for Collection",
            description=f"**{ign}** (<@{message.author.id}>) has agreed to the terms and is ready to collect:\n\n{desc}\n\nPress **Confirm Collection** once the items have been handed over — this will start the return timer.",
            color=discord.Color.blurple()
        )
        await message.channel.send(
            content=f"<@&{STAFF_ROLE_ID}>",
            embed=embed,
            view=ConfirmCollectionView(items, message.author.id, ign, agreement["message_id"])
        )

    @app_commands.command(name="setup_tickets", description="Post the ticket creation panel")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def setup_tickets(self, interaction: discord.Interaction):
        if not is_staff(interaction):
            await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)
            return
        embed = discord.Embed(
            title="Cryo Community Vault Ticketing System",
            description=(
                "🪙 **Donate** - This is for if you have a Cryo item(s) you would like to donate! "
                "Please fill out all the information on the form.\n\n"
                "❗ **Request** - This is for if you would like to request to use one of the many items we have in our vault!"
            ),
            color=discord.Color.dark_gray()
        )
        await interaction.channel.send(embed=embed, view=TicketTypeView())
        await interaction.response.send_message("Ticket panel posted!", ephemeral=True)

    @app_commands.command(name="borrow_item", description="Request to borrow an item from the vault")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.autocomplete(item=vault_item_autocomplete)
    @app_commands.describe(item="Item you want to borrow", quantity="How many? (default 1)")
    async def borrow_item(self, interaction: discord.Interaction, item: str, quantity: int = 1):
        if not interaction.channel.name.startswith("request-"):
            await interaction.response.send_message("This command can only be used in a request ticket.", ephemeral=True)
            return

        vault = load_vault()
        total_qty = sum(i["quantity"] for i in vault["items"] if i["name"].lower() == item.lower())
        if total_qty < quantity:
            await interaction.response.send_message(
                f"**{item}** doesn't have enough stock in the vault (requested: {quantity}, available: {total_qty}).",
                ephemeral=True
            )
            return

        channel_id = interaction.channel.id
        if channel_id not in borrow_sessions:
            borrow_sessions[channel_id] = {
                "borrower_id": interaction.user.id,
                "items": [],
                "list_message_id": None
            }

        session = borrow_sessions[channel_id]
        found = next((i for i in session["items"] if i[0].lower() == item.lower()), None)
        if found:
            session["items"][session["items"].index(found)] = (item, found[1] + quantity)
        else:
            session["items"].append((item, quantity))

        embed = build_borrow_list_embed(session["items"])

        if session["list_message_id"]:
            try:
                msg = await interaction.channel.fetch_message(session["list_message_id"])
                await msg.edit(embed=embed, view=BorrowListView())
                await interaction.response.send_message(f"Added **{quantity}x {item}** to your borrow list.", ephemeral=True)
            except Exception:
                await interaction.response.send_message(f"Added **{quantity}x {item}** to your borrow list.", ephemeral=True)
                msg = await interaction.channel.send(embed=embed, view=BorrowListView())
                session["list_message_id"] = msg.id
        else:
            await interaction.response.send_message(f"Added **{quantity}x {item}** to your borrow list.", ephemeral=True)
            msg = await interaction.channel.send(embed=embed, view=BorrowListView())
            session["list_message_id"] = msg.id


async def setup(bot):
    await bot.add_cog(TicketsCog(bot))