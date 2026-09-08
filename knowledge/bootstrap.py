"""Initialize a local administrator interactively; never print credentials."""

import getpass
from argon2 import PasswordHasher
from sqlalchemy import select
from .db import SessionLocal
from .models import KnowledgeAdmin as Admin


def main():
    username = input("Administrator username [admin]: ").strip() or "admin"
    password = getpass.getpass("Password (at least 12 characters): ")
    if len(password) < 12 or password != getpass.getpass("Repeat password: "):
        raise SystemExit("Password too short or confirmation does not match.")
    with SessionLocal() as db:
        if db.scalar(select(Admin).where(Admin.username == username)):
            raise SystemExit("Administrator already exists; no changes made.")
        db.add(Admin(username=username, password_hash=PasswordHasher().hash(password)))
        db.commit()
    print("Administrator created. Start the API and sign in.")


if __name__ == "__main__":
    main()
