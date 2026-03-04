"""Core navigation and page handlers extracted from app.py."""

from __future__ import annotations

import hmac
import os

from flask import current_app, flash, redirect, render_template, request, session, url_for

from models import AgentNode, AgentProjectBinding, Project, Repository, db
from services.model_loader import get_runtime_model
from utils.request_security import (
    _get_project_create_agent_codes,
    _has_admin_access,
    _has_project_create_access,
    _is_safe_redirect,
)


def _has_routable_endpoint(endpoint: str) -> bool:
    """Return True only when endpoint exists in url_map (not just view_functions)."""
    try:
        return any(rule.endpoint == endpoint for rule in current_app.url_map.iter_rules())
    except Exception:
        return False


def admin_login():
    auth_backend = (os.environ.get("AUTH_BACKEND") or "local").strip().lower()
    if auth_backend == "qkit":
        next_url = request.args.get("next") or request.form.get("next") or url_for("index")
        if _has_routable_endpoint("qkit_auth_bp.login"):
            return redirect(url_for("qkit_auth_bp.login", next=next_url))

        init_error = str(current_app.config.get("AUTH_INIT_ERROR") or "").strip()
        if init_error:
            flash(f"Qkit 登录模块初始化失败：{init_error}", "error")
        else:
            flash("Qkit 登录模块未初始化，请检查 AUTH_BACKEND 配置与依赖安装。", "error")
        return render_template("admin_login.html", next_url=next_url), 503

    next_url = request.args.get("next") or request.form.get("next") or url_for("index")
    if request.method == "POST":
        configured_user = os.environ.get("ADMIN_USERNAME", "admin").strip()
        configured_password = os.environ.get("ADMIN_PASSWORD", "").strip()
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        if not configured_password:
            flash("ADMIN_PASSWORD 未配置，无法登录管理员账号。", "error")
            return render_template("admin_login.html", next_url=next_url), 500

        if hmac.compare_digest(username, configured_user) and hmac.compare_digest(password, configured_password):
            session["is_admin"] = True
            session["admin_user"] = username
            session.permanent = True
            flash("管理员登录成功。", "success")
            if not _is_safe_redirect(next_url):
                next_url = url_for("index")
            return redirect(next_url)

        flash("管理员账号或密码错误。", "error")
    return render_template("admin_login.html", next_url=next_url)


def admin_logout():
    auth_backend = (os.environ.get("AUTH_BACKEND") or "local").strip().lower()
    if auth_backend == "qkit":
        if _has_routable_endpoint("auth_bp.logout"):
            return redirect(url_for("auth_bp.logout"))
        session.pop("auth_user_id", None)
        session.pop("auth_username", None)
        session.pop("auth_role", None)
        session.pop("is_admin", None)
        session.pop("admin_user", None)
        session.pop("auth_backend", None)
        session.pop("qkit_backhost", None)
        flash("Qkit 登录模块未初始化，已清理本地会话。", "warning")
        return redirect(url_for("index"))

    csrf_session_key = get_runtime_model("CSRF_SESSION_KEY")
    session.pop("is_admin", None)
    session.pop("admin_user", None)
    session.pop(csrf_session_key, None)
    flash("已退出管理员登录。", "success")
    return redirect(url_for("index"))


def test():
    return "服务器正常工作！"


def help_page():
    return render_template("help.html")


def _ensure_creator_project_admin_membership(project_id: int) -> tuple[bool, str | None]:
    """Ensure non-platform-admin creator can immediately manage/access the created project."""
    if _has_admin_access():
        return True, None

    user_id_raw = session.get("auth_user_id")
    try:
        user_id = int(user_id_raw)
    except (TypeError, ValueError):
        return False, "未获取当前登录用户，无法创建项目。"

    auth_backend = (os.environ.get("AUTH_BACKEND") or "local").strip().lower()
    if auth_backend == "qkit":
        from qkit_auth.models import (
            QkitAuthUser,
            QkitAuthUserProject,
            QkitPlatformRole,
            QkitProjectRole,
        )

        user = db.session.get(QkitAuthUser, user_id)
        if not user:
            return False, "当前登录用户不存在，无法创建项目。"
        if str(user.role or "").strip() in {"", QkitPlatformRole.NORMAL.value}:
            user.role = QkitPlatformRole.PROJECT_ADMIN.value

        membership = QkitAuthUserProject.query.filter_by(user_id=user.id, project_id=project_id).first()
        if membership:
            if membership.role != QkitProjectRole.ADMIN.value:
                membership.role = QkitProjectRole.ADMIN.value
            if hasattr(membership, "import_sync_locked"):
                membership.import_sync_locked = True
        else:
            db.session.add(
                QkitAuthUserProject(
                    user_id=user.id,
                    project_id=project_id,
                    role=QkitProjectRole.ADMIN.value,
                    approved_by=user.id,
                    imported_from_qkit=False,
                    import_sync_locked=True,
                )
            )
        return True, None

    from auth.models import AuthUser, AuthUserProject, PlatformRole, ProjectRole

    user = db.session.get(AuthUser, user_id)
    if not user:
        return False, "当前登录用户不存在，无法创建项目。"
    if str(user.role or "").strip() in {"", PlatformRole.NORMAL.value}:
        user.role = PlatformRole.PROJECT_ADMIN.value

    membership = AuthUserProject.query.filter_by(user_id=user.id, project_id=project_id).first()
    if membership:
        if membership.role != ProjectRole.ADMIN.value:
            membership.role = ProjectRole.ADMIN.value
    else:
        db.session.add(
            AuthUserProject(
                user_id=user.id,
                project_id=project_id,
                role=ProjectRole.ADMIN.value,
                approved_by=user.id,
            )
        )
    return True, None


def index():
    log_print = get_runtime_model("log_print")
    try:
        log_print("访问首页路由", "APP")
        from utils.request_security import _get_accessible_project_ids

        accessible_ids = _get_accessible_project_ids()
        if accessible_ids is None:
            projects = Project.query.order_by(Project.created_at.desc()).all()
            all_projects = []
        elif accessible_ids:
            projects = (
                Project.query.filter(Project.id.in_(accessible_ids))
                .order_by(Project.created_at.desc())
                .all()
            )
            all_projects = Project.query.order_by(Project.code).all()
        else:
            projects = []
            all_projects = Project.query.order_by(Project.code).all()

        joinable_projects = [
            project
            for project in all_projects
            if project.id not in (accessible_ids or [])
        ]

        total_projects = (len(all_projects) + len(projects)) if accessible_ids is not None else len(projects)
        deployment_mode = (os.environ.get("DEPLOYMENT_MODE") or "single").strip().lower()
        can_direct_create_project = _has_project_create_access()
        creatable_agent_codes = set(_get_project_create_agent_codes())
        agent_nodes = []
        if deployment_mode in {"platform", "agent"} and (can_direct_create_project or _has_admin_access()):
            try:
                from services.agent_management_handlers import build_agent_node_items

                all_nodes = build_agent_node_items()
                if _has_admin_access():
                    agent_nodes = all_nodes
                else:
                    agent_nodes = [
                        item for item in all_nodes
                        if str(item.get("agent_code") or "").strip() in creatable_agent_codes
                    ]
            except Exception as exc:
                log_print(f"加载Agent列表失败: {exc}", "AGENT", force=True)
        log_print(f"找到 {len(projects)} 个可见项目（总 {total_projects} 个）", "APP")
        return render_template(
            "index.html",
            projects=projects,
            joinable_projects=joinable_projects,
            is_platform_admin=_has_admin_access(),
            agent_nodes=agent_nodes,
            can_direct_create_project=can_direct_create_project,
            deployment_mode=deployment_mode,
        )
    except Exception as exc:
        log_print(f"首页路由错误: {str(exc)}", "APP", force=True)
        import traceback

        traceback.print_exc()
        return f"首页加载错误: {str(exc)}", 500


def projects():
    if request.method == "POST":
        if not _has_project_create_access():
            flash("无权限创建项目。", "error")
            return redirect(url_for("index"))

        code = request.form.get("code")
        name = request.form.get("name")
        department = request.form.get("department")
        selected_agent_code = (request.form.get("agent_code") or "").strip()
        deployment_mode = (os.environ.get("DEPLOYMENT_MODE") or "single").strip().lower()
        is_platform_admin = _has_admin_access()
        creatable_agent_codes = set(_get_project_create_agent_codes()) if not is_platform_admin else set()
        if not code or not name:
            flash("项目代号和名称不能为空", "error")
            return redirect(url_for("index"))

        existing_project = Project.query.filter_by(code=code).first()
        if existing_project:
            flash("项目代号已存在", "error")
            return redirect(url_for("index"))

        if deployment_mode in {"platform", "agent"} and not is_platform_admin:
            if not creatable_agent_codes:
                flash("当前账号未绑定可创建项目的Agent节点，请联系管理员。", "error")
                return redirect(url_for("index"))
            if selected_agent_code:
                if selected_agent_code not in creatable_agent_codes:
                    flash("只能选择您拥有创建权限的Agent节点。", "error")
                    return redirect(url_for("index"))
            else:
                if len(creatable_agent_codes) == 1:
                    selected_agent_code = next(iter(creatable_agent_codes))
                else:
                    flash("请选择一个可用的Agent节点。", "error")
                    return redirect(url_for("index"))

        project = Project(code=code, name=name, department=department)
        db.session.add(project)
        db.session.flush()

        ok, err = _ensure_creator_project_admin_membership(project.id)
        if not ok:
            db.session.rollback()
            flash(err or "创建项目失败：无法写入项目成员关系", "error")
            return redirect(url_for("index"))

        if selected_agent_code and deployment_mode in {"platform", "agent"}:
            selected_agent = AgentNode.query.filter_by(agent_code=selected_agent_code).first()
            if not selected_agent:
                db.session.rollback()
                flash("所选 Agent 节点不存在，请刷新后重试", "error")
                return redirect(url_for("index"))

            db.session.add(
                AgentProjectBinding(
                    agent_id=selected_agent.id,
                    project_id=project.id,
                    project_code=project.code,
                )
            )
            db.session.commit()
            flash(f"项目创建成功，已绑定到 Agent: {selected_agent.agent_name or selected_agent.agent_code}", "success")
            return redirect(url_for("index"))

        db.session.commit()
        flash("项目创建成功", "success")
        return redirect(url_for("index"))

    # GET 请求也重定向到首页（统一使用 index.html 模板）
    return redirect(url_for("index"))


def project_detail(project_id):
    return redirect(url_for("merged_project_view", project_id=project_id))


def project_detail_original(project_id):
    project = Project.query.get_or_404(project_id)
    repositories = Repository.query.filter_by(project_id=project_id).order_by(Repository.display_order).all()
    return render_template("project_detail.html", project=project, repositories=repositories)


def repository_config(project_id):
    project = Project.query.get_or_404(project_id)
    repositories = Repository.query.filter_by(project_id=project_id).order_by(Repository.display_order).all()
    return render_template("repository_config.html", project=project, repositories=repositories)


def add_git_repository(project_id):
    project = Project.query.get_or_404(project_id)
    return render_template("add_git_repository.html", project=project)


def add_svn_repository(project_id):
    project = Project.query.get_or_404(project_id)
    return render_template("add_svn_repository.html", project=project)
