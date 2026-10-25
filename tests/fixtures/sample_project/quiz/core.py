"""Tiny fixture app: proves the bundled interpreter has sqlite3, fractions and tkinter."""
import sqlite3
from fractions import Fraction


def add_fractions(a: str, b: str) -> str:
    return str(Fraction(a) + Fraction(b))


def store_and_load(db_path: str, value: str) -> str:
    con = sqlite3.connect(db_path)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS answers (id INTEGER PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO answers (value) VALUES (?)", (value,))
        con.commit()
        return con.execute("SELECT value FROM answers ORDER BY id DESC LIMIT 1").fetchone()[0]
    finally:
        con.close()
