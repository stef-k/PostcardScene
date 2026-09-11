"""Single local administrator and revocable login identity, independent of Flask."""

import secrets

from sqlalchemy import CheckConstraint, String, select
from sqlalchemy.orm import Mapped, mapped_column
from werkzeug.security import check_password_hash, generate_password_hash

from postcardscene.persistence import Base


class Administrator(Base):
    __tablename__ = "administrator"
    __table_args__ = (CheckConstraint("id = 1", name="single_administrator"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    session_id: Mapped[str] = mapped_column(String(64), unique=True)


def set_password(database, username, password, *, initial=False):
    """Host-authorized setup/reset; hashing happens outside the write transaction."""
    if not username or len(username) > 64 or username != username.strip():
        raise ValueError("Username must contain 1–64 characters without outer spaces.")
    if not 12 <= len(password) <= 128:
        raise ValueError("Password must contain 12–128 characters.")
    password_hash = generate_password_hash(password, method="scrypt")
    with database.transaction() as session:
        admin = session.get(Administrator, 1)
        if initial:
            if admin is not None:
                raise ValueError("Administrator already exists; use reset-password.")
            admin = Administrator(id=1, username=username)
            session.add(admin)
        elif admin is None or admin.username != username:
            raise ValueError("Administrator not found; verify the username.")
        admin.password_hash = password_hash
        admin.session_id = secrets.token_hex(32)


def authenticate(database, username, password):
    if not 1 <= len(username) <= 64 or not 1 <= len(password) <= 128:
        return None
    with database.transaction() as session:
        admin = session.scalar(select(Administrator).where(Administrator.id == 1))
        credentials = (
            (admin.password_hash, admin.session_id, admin.username) if admin else None
        )
    if (
        credentials
        and check_password_hash(credentials[0], password)
        and credentials[2] == username
    ):
        return credentials[1]
    return None


def revoke_sessions(database):
    with database.transaction() as session:
        admin = session.get(Administrator, 1)
        if admin is not None:
            admin.session_id = secrets.token_hex(32)
