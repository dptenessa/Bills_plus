"""Config for the expense pilot."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_URL = os.getenv("DATABASE_URL", "sqlite:///./data/expense_pilot.db")
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", ROOT / "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = ROOT / "expense_config.json"
DEFAULT_CONFIG = {
    "categories": ["Utilities", "Office supplies", "Maintenance", "Services", "Travel", "Other"],
    "cost_centers": ["General"],
}
SESSION_HOURS = int(os.getenv("SESSION_HOURS", "12"))

def load_expense_config():
    try:
        supplied=json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError,ValueError):
        supplied={}
    configured={}
    for key,defaults in DEFAULT_CONFIG.items():
        value=supplied.get(key,defaults) if isinstance(supplied,dict) else defaults
        cleaned=[str(item).strip() for item in value if str(item).strip()] if isinstance(value,list) else defaults[:]
        configured[key]=list(dict.fromkeys(cleaned)) or defaults[:]
    return configured


def load_payer_config():
    try:
        supplied=json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        payer=supplied.get('payer',{}) if isinstance(supplied,dict) else {}
    except (OSError,ValueError):
        payer={}
    return {key:str(payer.get(key,'')).strip() for key in ('name','address','postal_city')}


# Build the loaded settings after both loader functions are defined.
EXPENSE_CONFIG = load_expense_config()
PAYER_CONFIG = load_payer_config()

