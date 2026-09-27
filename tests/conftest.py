"""Общее для тестов: код лежит в src/, база — во временном каталоге.

Без этого тесты писали бы в боевую /data/bot.db.
"""
import os, sys, tempfile
from pathlib import Path

os.environ.setdefault("BOT_DATA", tempfile.mkdtemp())
os.environ.setdefault("TG_BOT_TOKEN", "test")
os.environ.setdefault("OPENROUTER_API_KEY", "test")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
