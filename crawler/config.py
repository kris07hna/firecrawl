import os
import sys
import datetime

# Ensure Windows terminal can print emojis
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

VERSION = "3.0.0" # Enterprise Edition
DEFAULT_MODEL = "opencode/deepseek-v4-flash-free"
MAX_STEPS = 10
MAX_RETRIES = 3

def log(msg: str, level: str = "INFO"):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    prefix = ""
    if level == "INFO": prefix = "[INFO]"
    elif level == "WARN": prefix = "[WARN]"
    elif level == "ERROR": prefix = "[ERROR]"
    elif level == "NAV": prefix = "[NAV]"
    elif level == "SNAP": prefix = "[SNAP]"
    elif level == "AI": prefix = "[AI]"
    print(f"[{timestamp}] {prefix} {msg}")
