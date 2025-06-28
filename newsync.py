#!/usr/bin/env python3
"""
Inventory synchronizer for BrickLink & BrickOwl
Keeps track of previous quantities to propagate only real changes.

Requirements:
    pip install requests requests_oauthlib python-dotenv schedule
"""

import json
import os
import time
import logging
import schedule
import requests
from pathlib import Path
from requests_oauthlib import OAuth1
from dotenv import load_dotenv

# ─── Config ────────────────────────────────────────────────────────────────────
STATE_FILE = Path("inventory_state.json")   # local snapshot
SYNC_EVERY_MIN = 15                         # minutes between syncs
CONFLICT_STRATEGY = "higher"                # "higher" or "lower"

load_dotenv()

BL_CONSUMER_KEY    = os.getenv("BL_CONSUMER_KEY")
BL_CONSUMER_SECRET = os.getenv("BL_CONSUMER_SECRET")
BL_TOKEN           = os.getenv("BL_TOKEN")
BL_TOKEN_SECRET    = os.getenv("BL_TOKEN_SECRET")
BO_API_KEY         = os.getenv("BO_API_KEY")

BL_API_BASE = "https://api.bricklink.com/api/store/v1"
BO_API_BASE = "https://www.brickowl.com/api/v1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ─── Helper classes ────────────────────────────────────────────────────────────
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
        logging.info("BrickLink: inventory %s → %s", inventory_id, qty)

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
        r = requests.post(url, json={"item_id": item_id, "quantity": qty}, headers=self.headers)
        r.raise_for_status()
        logging.info("BrickOwl: item %s → %s", item_id, qty)

# ─── State persistence ─────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        with STATE_FILE.open() as f:
            return json.load(f)
    return {}

def save_state(state):
    with STATE_FILE.open("w") as f:
        json.dump(state, f)

# ─── Sync logic ────────────────────────────────────────────────────────────────
def sync():
    bl_api = BrickLinkAPI()
    bo_api = BrickOwlAPI()

    bl_inv = bl_api.get_inventory()
    bo_inv = bo_api.get_inventory()
    prev_state = load_state()

    new_state = {}
    all_keys = set(bl_inv) | set(bo_inv) | set(prev_state)

    for key in all_keys:
        bl_qty = bl_inv.get(key, {}).get("qty", 0)
        bo_qty = bo_inv.get(key, {}).get("qty", 0)
        prev_qty = prev_state.get(":".join(map(str, key)), 0)

        # Build new snapshot entry
        new_state[":".join(map(str, key))] = max(bl_qty, bo_qty)

        # No difference → nothing to do
        if bl_qty == bo_qty:
            continue

        # Determine which side changed
        bl_changed = bl_qty != prev_qty
        bo_changed = bo_qty != prev_qty

        # If only one side changed, push that change
        if bl_changed and not bo_changed:
            if key in bo_inv:
                bo_api.update_quantity(bo_inv[key]["item_id"], bl_qty)
            elif bl_qty:  # new item on BL, create on BO
                bo_api.update_quantity(None, bl_qty)  # requires extra params if creating
        elif bo_changed and not bl_changed:
            if key in bl_inv:
                bl_api.update_quantity(bl_inv[key]["inventory_id"], bo_qty)
            elif bo_qty:
                # new item on BO, create on BL (needs extra params)
                pass
        else:
            # Conflict: both changed → choose strategy
            chosen = max(bl_qty, bo_qty) if CONFLICT_STRATEGY == "higher" else min(bl_qty, bo_qty)
            if bl_qty != chosen and key in bl_inv:
                bl_api.update_quantity(bl_inv[key]["inventory_id"], chosen)
            if bo_qty != chosen and key in bo_inv:
                bo_api.update_quantity(bo_inv[key]["item_id"], chosen)

    save_state(new_state)
    logging.info("Sync complete. %d items processed.", len(all_keys))

# ─── Entrypoint ────────────────────────────────────────────────────────────────
def main():
    sync()
    schedule.every(SYNC_EVERY_MIN).minutes.do(sync)
    while True:
        schedule.run_pending()
        time.sleep(5)

if __name__ == "__main__":
    main()