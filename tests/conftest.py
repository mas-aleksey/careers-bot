"""Общее для тестов: код лежит в src/, база — во временном каталоге.

Без этого тесты писали бы в боевую /data/bot.db.
"""
import os, sys, tempfile
from pathlib import Path

# Присваивание, а не setdefault: тесты гоняют и внутри контейнеров, где эти
# переменные уже есть. Унаследованный TG_BOT_TOKEN уводил setMyCommands
# в чужого бота, унаследованный BOT_DATA — в боевую /data/bot.db.
os.environ["BOT_DATA"] = tempfile.mkdtemp()
os.environ["TG_BOT_TOKEN"] = "test"
os.environ["OPENROUTER_API_KEY"] = "test"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
