import uuid
from types import SimpleNamespace

from app import app, create_tables, db
from auth.models import AuthUser
from qkit_auth.models import QkitAuthUser
from services.commit_operation_handlers import _attach_author_display


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def test_attach_author_display_maps_from_local_auth_user():
    with app.app_context():
        create_tables()
        username = _uid("local_user")
        display_name = "本地姓名映射"
        db.session.add(
            AuthUser(
                username=username,
                password_hash="hashed",
                display_name=display_name,
                email=f"{username}@corp.netease.com",
                role="normal",
                is_active=True,
            )
        )
        db.session.commit()

        commit = SimpleNamespace(author=username, author_display=None)
        _attach_author_display([commit])
        assert commit.author_display == display_name


def test_attach_author_display_falls_back_to_qkit_user_when_local_missing():
    with app.app_context():
        create_tables()
        username = _uid("qkit_user")
        display_name = "Qkit姓名映射"
        db.session.add(
            QkitAuthUser(
                username=username,
                display_name=display_name,
                email=f"{username}@corp.netease.com",
                role="normal",
                is_active=True,
                source="qkit",
            )
        )
        db.session.commit()

        commit = SimpleNamespace(author=username, author_display=None)
        _attach_author_display([commit])
        assert commit.author_display == display_name

