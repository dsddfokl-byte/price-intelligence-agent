"""Application initialization helpers."""

from app.database import Database


def initialize_database(database: Database) -> None:
    database.initialize()
