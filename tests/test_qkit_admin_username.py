#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from qkit_auth.models import QkitPlatformRole
from qkit_auth import services as qservices


def _patch_user_creation_path(monkeypatch):
    monkeypatch.setattr(qservices, "_get_user_by_username", lambda username: None)
    monkeypatch.setattr(qservices, "apply_pre_assignments", lambda user: 0)
    monkeypatch.setattr(qservices.db.session, "add", lambda obj: None)
    monkeypatch.setattr(qservices.db.session, "flush", lambda: None)


def test_qkit_user_promoted_by_qkit_admin_username(monkeypatch):
    monkeypatch.setenv("QKIT_ADMIN_USERNAME", "qkit_admin")
    monkeypatch.setenv("ADMIN_USERNAME", "local_admin")
    _patch_user_creation_path(monkeypatch)

    user, err = qservices.ensure_qkit_user(
        username="qkit_admin",
        display_name="Qkit Admin",
        email="qkit_admin@corp.netease.com",
        source="test",
    )

    assert err is None
    assert user is not None
    assert user.role == QkitPlatformRole.PLATFORM_ADMIN.value


def test_qkit_user_not_promoted_by_local_admin_username(monkeypatch):
    monkeypatch.delenv("QKIT_ADMIN_USERNAME", raising=False)
    monkeypatch.setenv("ADMIN_USERNAME", "legacy_local_admin")
    _patch_user_creation_path(monkeypatch)

    user, err = qservices.ensure_qkit_user(
        username="legacy_local_admin",
        display_name="Legacy Local Admin",
        email="legacy_local_admin@corp.netease.com",
        source="test",
    )

    assert err is None
    assert user is not None
    assert user.role == QkitPlatformRole.NORMAL.value
