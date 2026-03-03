"""Core navigation and page handlers extracted from app.py."""

from __future__ import annotations

import hmac
import os

from flask import flash, redirect, render_template, request, session, url_for

from models import AgentNode, AgentProjectBinding, Project, Repository, db
from services.model_loader import get_runtime_model
from utils.request_security import _has_admin_access, _is_safe_redirect


def admin_login():
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
        agent_nodes = []
        if _has_admin_access() and deployment_mode in {"platform", "agent"}:
            try:
                from services.agent_management_handlers import build_agent_node_items

                agent_nodes = build_agent_node_items()
            except Exception as exc:
                log_print(f"加载Agent列表失败: {exc}", "AGENT", force=True)
        log_print(f"找到 {len(projects)} 个可见项目（总 {total_projects} 个）", "APP")
        return render_template(
            "index.html",
            projects=projects,
            joinable_projects=joinable_projects,
            is_platform_admin=_has_admin_access(),
            agent_nodes=agent_nodes,
            deployment_mode=deployment_mode,
        )
    except Exception as exc:
        log_print(f"首页路由错误: {str(exc)}", "APP", force=True)
        import traceback

        traceback.print_exc()
        return f"首页加载错误: {str(exc)}", 500


def projects():
    if request.method == "POST":
        code = request.form.get("code")
        name = request.form.get("name")
        department = request.form.get("department")
        selected_agent_code = (request.form.get("agent_code") or "").strip()
        deployment_mode = (os.environ.get("DEPLOYMENT_MODE") or "single").strip().lower()
        if not code or not name:
            flash("项目代号和名称不能为空", "error")
            return redirect(url_for("index"))

        existing_project = Project.query.filter_by(code=code).first()
        if existing_project:
            flash("项目代号已存在", "error")
            return redirect(url_for("index"))

        project = Project(code=code, name=name, department=department)
        db.session.add(project)
        db.session.flush()

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
