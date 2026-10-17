"""Make agent/ importable without Docker. db.py only reads DATABASE_URL at import;
no test opens a real connection."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))
os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")
