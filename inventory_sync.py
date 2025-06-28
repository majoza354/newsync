#!/usr/bin/env python3
"""
Inventory synchronizer for BrickLink & BrickOwl with a minimal text UI.

This tool reads inventory from both services and compares it against the last
saved state.  On startup it presents a small curses based interface showing
high level statistics and a preview of the proposed synchronisation.
"""

import curses
import json
import logging
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests_oauthlib import OAuth1

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
STATE_FILE = Path("inventory_state.json")
SYNC_EVERY_MIN = 15
CONFLICT_STRATEGY = "higher"  # or "lower"

load_dotenv()

BL_CONSUMER_KEY = os.getenv("BL_CONSUMER_KEY")
BL_CONSUMER_SECRET = os.getenv("BL_CONSUMER_SECRET")
BL_TOKEN = os.getenv("BL_TOKEN")
BL_TOKEN_SECRET = os.getenv("BL_TOKEN_SECRET")
BO_API_KEY = os.getenv("BO_API_KEY")

BL_API_BASE = "https://api.bricklink.com/api/store/v1"
BO_API_BASE = "https://www.brickowl.com/api/v1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ---------------------------------------------------------------------------
# API helper classes
# ---------------------------------------------------------------------------
class BrickLinkAPI:
    def __init__(self):
        if not all([BL_CONSUMER_KEY, BL_CONSUMER_SECRET, BL_TOKEN, BL_TOKEN_SECRET]):
            raise RuntimeError("BrickLink credentials missing.")
        self.auth = OAuth1(
            BL_CONSUMER_KEY,
            BL_CONSUMER_SECRET,
            BL_TOKEN,
            BL_TOKEN_SECRET,
            signature_type="auth_header",
        )

    def get_inventory(self):
        url = f"{BL_API_BASE}/inventories"
        r = requests.get(url, auth=self.auth)
        r.raise_for_status()
        data = r.json()
        inv = {}
        for itm in data["data"]:
            key = (itm["item"]["no"], itm["color_id"])
            inv[key] = {
                "inventory_id": itm["inventory_id"],
                "qty": itm["quantity"],
            }
        return inv

    def update_quantity(self, inventory_id, qty):
        url = f"{BL_API_BASE}/inventories/{inventory_id}"
        r = requests.put(url, json={"quantity": qty}, auth=self.auth)
        r.raise_for_status()
        logging.info("BrickLink: inventory %s -> %s", inventory_id, qty)


class BrickOwlAPI:
    def __init__(self):
        if not BO_API_KEY:
            raise RuntimeError("BrickOwl API key missing.")
        self.headers = {"X-API-KEY": BO_API_KEY}

    def get_inventory(self):
        url = f"{BO_API_BASE}/inventory"
        r = requests.get(url, headers=self.headers)
        r.raise_for_status()
        data = r.json()
        inv = {}
        for itm in data["items"]:
            key = (itm["part_num"], itm["colour_id"])
            inv[key] = {
                "item_id": itm["item_id"],
                "qty": itm["quantity"],
            }
        return inv

    def update_quantity(self, item_id, qty):
        url = f"{BO_API_BASE}/inventory/update"
        r = requests.post(
            url, json={"item_id": item_id, "quantity": qty}, headers=self.headers
        )
        r.raise_for_status()
        logging.info("BrickOwl: item %s -> %s", item_id, qty)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        with STATE_FILE.open() as f:
            return json.load(f)
    return {}


def save_state(state):
    with STATE_FILE.open("w") as f:
        json.dump(state, f)


# ---------------------------------------------------------------------------
# Synchronisation logic
# ---------------------------------------------------------------------------
def compute_sync(bl_inv, bo_inv, prev_state):
    """Return new state and list of actions required to sync."""
    new_state = {}
    actions = []
    all_keys = set(bl_inv) | set(bo_inv) | {tuple(k.split(":")) for k in prev_state}

    for key in all_keys:
        bl_qty = bl_inv.get(key, {}).get("qty", 0)
        bo_qty = bo_inv.get(key, {}).get("qty", 0)
        prev_qty = prev_state.get(":".join(map(str, key)), 0)

        new_state[":".join(map(str, key))] = max(bl_qty, bo_qty)

        if bl_qty == bo_qty:
            continue

        bl_changed = bl_qty != prev_qty
        bo_changed = bo_qty != prev_qty

        if bl_changed and not bo_changed:
            actions.append(("BO", key, bl_qty))
        elif bo_changed and not bl_changed:
            actions.append(("BL", key, bo_qty))
        else:
            chosen = max(bl_qty, bo_qty) if CONFLICT_STRATEGY == "higher" else min(bl_qty, bo_qty)
            if bl_qty != chosen:
                actions.append(("BL", key, chosen))
            if bo_qty != chosen:
                actions.append(("BO", key, chosen))

    return new_state, actions


def apply_actions(actions, bl_inv, bo_inv, bl_api, bo_api):
    for target, key, qty in actions:
        if target == "BL" and key in bl_inv:
            bl_api.update_quantity(bl_inv[key]["inventory_id"], qty)
        elif target == "BO" and key in bo_inv:
            bo_api.update_quantity(bo_inv[key]["item_id"], qty)


# ---------------------------------------------------------------------------
# Text UI
# ---------------------------------------------------------------------------

def draw_interface(stdscr, bl_inv, bo_inv, prev_state, actions):
    stdscr.clear()
    lines = [
        "Brick Inventory Sync",
        "",
        f"BrickLink items: {len(bl_inv)}",
        f"BrickOwl items:  {len(bo_inv)}",
        f"Items in last state: {len(prev_state)}",
        "",
        "Proposed changes:",
    ]
    for target, key, qty in actions[:10]:
        lines.append(f" - set {key} on {target} to {qty}")
    if len(actions) > 10:
        lines.append(f" ... and {len(actions)-10} more")
    lines.append("")
    lines.append("Press q to quit.")

    for idx, line in enumerate(lines):
        stdscr.addstr(idx, 0, line)
    stdscr.refresh()


def main_curses(stdscr):
    curses.curs_set(0)
    bl_api = BrickLinkAPI()
    bo_api = BrickOwlAPI()

    bl_inv = bl_api.get_inventory()
    bo_inv = bo_api.get_inventory()
    prev_state = load_state()

    new_state, actions = compute_sync(bl_inv, bo_inv, prev_state)
    draw_interface(stdscr, bl_inv, bo_inv, prev_state, actions)

    next_sync = time.time() + SYNC_EVERY_MIN * 60
    while True:
        if time.time() >= next_sync:
            bl_inv = bl_api.get_inventory()
            bo_inv = bo_api.get_inventory()
            prev_state = load_state()
            new_state, actions = compute_sync(bl_inv, bo_inv, prev_state)
            save_state(new_state)
            draw_interface(stdscr, bl_inv, bo_inv, prev_state, actions)
            next_sync = time.time() + SYNC_EVERY_MIN * 60

        ch = stdscr.getch()
        if ch == ord("q"):
            break
        time.sleep(0.1)

    save_state(new_state)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    curses.wrapper(main_curses)
