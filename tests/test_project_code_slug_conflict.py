# -*- coding: utf-8 -*-
"""建项目时就要挡住「折成同一个知识包目录」的代号。

## 为什么 `unique=True` 不够

`project.code` 上的唯一约束**大小写敏感**（SQLite 的 TEXT UNIQUE 走 BINARY 排序），
而知识包目录名走 `project_pack_slug`，它会 `lower()` 并把非法字符折成连字符。于是
`QAREV42` / `qarev42`、`QAREV-42` / `qarev_42` 都映到**同一个目录**。

Windows 的文件系统同样不区分大小写，所以那两个代号在那台机器上**本来就只能是同一个
目录** —— 不是「改个名就能躲开」的事。

`project_pack_service` 那一侧已经有归属标记兜底（访问时明确拒绝，见
`test_project_pack_ownership.py`），但那是**事后**：用户会看到一个「代号被占用」的
知识包面板，而不是「这个代号不能起」。这里守的是**事前**那一道。

## 判据只有一份

四个建项目入口都调 `services/project_code_rules.slug_conflict`：Web 建项目、
Agent 上报项目、Qkit 建项目申请的两个分支。入口各写一遍的话早晚会漏一个，
而漏掉的那个入口就是漏洞本身 —— 所以这一组用例钉的是**那个函数**加上**每个入口都调了它**。
"""
import uuid

from app import app, create_tables, db
from models import Project
from qkit_auth.models import QkitAuthProjectCreateRequest, QkitAuthUser, QkitPlatformRole, QkitRequestStatus
from qkit_auth.services import handle_create_project_request, request_create_project
from services.project_code_rules import slug_conflict


def _uid(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def _project(code: str, name: str = "") -> Project:
    """建一个**已提交**的项目。

    必须 commit 而不是只 flush：Agent 入口在冲突时会 `db.session.rollback()`，
    只 flush 的「先建的那个」会被那次回滚一起带走 —— 于是断言 `count() == before`
    变成 `0 == 1`，看着像「项目被建出来了」，其实是我自己的前提被撤了。
    """
    project = Project(code=code, name=name or code)
    db.session.add(project)
    db.session.commit()
    return project


def test_a_case_variant_code_is_a_conflict():
    with app.app_context():
        create_tables()
        first = _project(_uid("QAREV").upper(), "先建的那个")

        conflict = slug_conflict(first.code.lower())

        assert conflict is not None and conflict.id == first.id


def test_a_code_that_folds_to_the_same_slug_is_a_conflict():
    """非法字符折算之后相同也算 —— 判据是**目录名**，不是字符串。"""
    with app.app_context():
        create_tables()
        first = _project(_uid("QAREV").upper(), "先建的那个")

        assert slug_conflict(f"{first.code.lower()}-") is not None


def test_an_unrelated_code_is_not_a_conflict():
    """反方向：不相干的代号不许被误伤（这条错了就是「项目建不出来」）。"""
    with app.app_context():
        create_tables()
        _project(_uid("QAREV").upper(), "先建的那个")

        assert slug_conflict(_uid("OTHER").upper()) is None


def test_the_exact_same_code_is_left_to_the_existing_check():
    """代号**逐字相同**不算这里管 —— 那由既有的「代号已存在」判据处理。

    这条分界是要紧的：`slug_conflict` 若把它也算成冲突，调用方就会对同一个项目
    自己跟自己冲突（例如以后加「改自己的名字」时）。
    """
    with app.app_context():
        create_tables()
        code = _uid("QAREV").upper()
        first = _project(code, "先建的那个")

        assert slug_conflict(code) is None
        assert slug_conflict(code, exclude_project_id=first.id) is None


def test_a_blank_code_is_not_a_conflict():
    with app.app_context():
        create_tables()
        _project(_uid("QAREV").upper(), "先建的那个")

        assert slug_conflict("") is None
        assert slug_conflict("   ") is None


def test_the_web_entry_point_rejects_a_case_variant(monkeypatch):
    """Web 建项目那个入口：返回里要有那句话，且**不建**项目。

    这一条走的是真接口（`_has_admin_access` 对平台管理员短路放行），所以它对
    「入口有没有真的调那个判据」是有效的 —— 只钉判据函数会漏掉接线。
    """
    token = _uid("admin-token")
    monkeypatch.setenv("ADMIN_API_TOKEN", token)
    with app.app_context():
        create_tables()
        existing = _project(_uid("QAREV").upper(), "先建的那个")
        before = Project.query.count()

        with app.test_client() as client:
            resp = client.post(
                "/projects",
                data={"code": existing.code.lower(), "name": "想撞名的那个"},
                headers={"X-Admin-Token": token},
            )

        assert resp.status_code in (200, 302), resp.status_code
        assert Project.query.count() == before, "折成同名的项目被建出来了"


def test_the_agent_entry_point_reports_the_code_as_conflicting(monkeypatch):
    """Agent 上报项目那条：代号折成同名时不建，且**报出是哪个代号挡住的**。

    走既有 `conflict_project_codes` 出口（409）。这里不能只看状态码 ——
    409 也可能是「代号逐字已存在」那条老路径给的，所以断言里要认那个代号本身。
    """
    secret = "agent-secret-slug-conflict"
    monkeypatch.setenv("AGENT_SHARED_SECRET", secret)
    monkeypatch.delenv("ADMIN_API_TOKEN", raising=False)
    with app.app_context():
        create_tables()
        existing = _project(_uid("QAREV").upper(), "先建的那个")
        before = Project.query.count()
        wanted = existing.code.lower()

        with app.test_client() as client:
            resp = client.post(
                "/api/agents/register",
                json={"agent_code": _uid("node"), "agent_name": "n", "project_codes": [wanted]},
                headers={"X-Agent-Secret": secret},
            )

        assert resp.status_code == 409, resp.status_code
        assert wanted in (resp.get_json() or {}).get("conflict_project_codes", [])
        assert Project.query.count() == before, "折成同名的项目被建出来了"


def test_the_qkit_request_entry_point_refuses_a_case_variant():
    """Qkit 提交建项目申请那条：当场拒绝，且**不留一条待审批申请**。

    只断言「返回 False」是不够的：申请若已经落库，审批通过时才会失败，
    用户会以为已经排上队了。
    """
    with app.app_context():
        create_tables()
        existing = _project(_uid("QAREV").upper(), "先建的那个")
        user = QkitAuthUser(username=_uid("u"), role=QkitPlatformRole.NORMAL.value)
        db.session.add(user)
        db.session.flush()

        ok, message = request_create_project(
            user.id, existing.code.lower(), "想撞名的那个"
        )

        assert ok is False
        assert existing.code in message, message
        assert QkitAuthProjectCreateRequest.query.filter_by(
            project_code=existing.code.lower()
        ).count() == 0, "被拒的申请还是落库了"


def test_the_qkit_approval_entry_point_rechecks_a_code_that_became_a_conflict():
    """审批那条**必须再判一次**：申请提交时那个代号还没被占，审批时已经被占了。

    这是两个分支都接线了的理由本身 —— 只判提交侧的话，这条申请会一路通过，
    然后建出两个共用知识包的项目。所以这条用例的前提是**申请先于冲突存在**。
    """
    with app.app_context():
        create_tables()
        code = _uid("QAREV").upper()
        applicant = QkitAuthUser(username=_uid("u"), role=QkitPlatformRole.NORMAL.value)
        db.session.add(applicant)
        db.session.flush()

        # 先提交申请（此时还没有任何项目占着这个代号）
        ok, message = request_create_project(applicant.id, code, "后来被撞名的那个")
        assert ok, message
        request_row = QkitAuthProjectCreateRequest.query.filter_by(
            project_code=code, status=QkitRequestStatus.PENDING.value
        ).first()
        assert request_row is not None, "前提不成立：申请没进去，这条用例守的是另一件事"

        # 申请提交之后，别人用折成同名的另一个代号把位置占了
        existing = _project(code.lower(), "抢先建的那个")
        before = Project.query.count()

        ok, message = handle_create_project_request(request_row.id, "approve", applicant.id)

        assert ok is False
        assert existing.code in message, message
        assert Project.query.count() == before, "审批时还是把折成同名的项目建出来了"
