import os
import datetime

VERSION = "3.0.0" # Enterprise Edition
DEFAULT_MODEL = "opencode/deepseek-v4-flash-free"
MAX_STEPS = 10
MAX_RETRIES = 3

def log(msg: str, level: str = "INFO"):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    prefix = ""
    if level == "INFO": prefix = "[·]"
    elif level == "WARN": prefix = "[!]"
    elif level == "ERROR": prefix = "[X]"
    elif level == "NAV": prefix = "[🔗]"
    elif level == "SNAP": prefix = "[📸]"
    elif level == "AI": prefix = "[🧠]"
    print(f"[{timestamp}] {prefix} {msg}")
