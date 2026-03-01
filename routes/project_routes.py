#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目路由
"""

from flask import render_template, request, redirect, url_for, flash
from . import project_bp
from models import db, Project, Repository
from utils.safe_print import log_print


@project_bp.route('/', methods=['GET', 'POST'])
def projects():
    """项目管理路由"""
    if request.method == 'POST':
        code = request.form.get('code')
        name = request.form.get('name')
        department = request.form.get('department')
        
        if not code or not name:
            flash('项目代号和名称不能为空', 'error')
            return redirect(url_for('project.projects'))
        
        # 检查项目代号是否已存在
        existing_project = Project.query.filter_by(code=code).first()
        if existing_project:
            flash('项目代号已存在', 'error')
            return redirect(url_for('project.projects'))
        
        project = Project(code=code, name=name, department=department)
        db.session.add(project)
        db.session.commit()
        flash('项目创建成功', 'success')
        return redirect(url_for('project.projects'))
    
    projects = Project.query.order_by(Project.created_at.desc()).all()
    return render_template('projects.html', projects=projects)


@project_bp.route('/<int:project_id>')
def project_detail(project_id):
    """项目详情页面 - 重定向到项目概览"""
    return redirect(url_for('project.merged_project_view', project_id=project_id))


@project_bp.route('/<int:project_id>/detail')
def project_detail_original(project_id):
    """保留原项目详情页面作为备用"""
    project = Project.query.get_or_404(project_id)
    repositories = Repository.query.filter_by(project_id=project_id).order_by(Repository.display_order).all()
    return render_template('project_detail.html', project=project, repositories=repositories)


@project_bp.route('/<int:project_id>/merged-view')
def merged_project_view(project_id):
    """合并的项目视图：左侧周版本列表，右侧仓库列表"""
    from models import WeeklyVersionConfig
    from datetime import datetime, timezone
    from utils.timezone_utils import now_beijing
    
    project = Project.query.get_or_404(project_id)

    # 获取所有周版本配置
    configs = WeeklyVersionConfig.query.filter_by(project_id=project_id).order_by(WeeklyVersionConfig.created_at.desc()).all()

    # 获取所有仓库
    repositories = Repository.query.filter_by(project_id=project_id).order_by(Repository.display_order).all()

    # 按时间范围和名称分组周版本配置
    now = now_beijing()

    # 分组逻辑：相同版本基础名称+相同时间范围的配置归为一组
    version_groups = {}
    for config in configs:
        # 提取版本基础名称（去掉仓库后缀）
        base_name = config.name
        if ' - ' in config.name:
            base_name = config.name.split(' - ')[0]

        # 创建分组键：基础名称 + 开始时间 + 结束时间
        group_key = f"{base_name}_{config.start_time.strftime('%Y%m%d%H%M')}_{config.end_time.strftime('%Y%m%d%H%M')}"

        if group_key not in version_groups:
            version_groups[group_key] = {
                'name': base_name,
                'start_time': config.start_time,
                'end_time': config.end_time,
                'configs': [],
                'is_active': False
            }

        version_groups[group_key]['configs'].append(config)

        # 判断是否为活跃版本（当前时间在版本时间范围内）
        try:
            now_local = now.replace(tzinfo=None)
            start_time = config.start_time
            end_time = config.end_time
            if start_time.tzinfo is not None:
                start_time = start_time.replace(tzinfo=None)
            if end_time.tzinfo is not None:
                end_time = end_time.replace(tzinfo=None)

            if start_time <= now_local <= end_time:
                version_groups[group_key]['is_active'] = True
        except Exception as e:
            log_print(f"时间比较出错: {str(e)}", 'APP', force=True)

    # 分离活跃和非活跃版本
    active_versions = []
    inactive_versions = []

    for group in version_groups.values():
        if group['is_active']:
            active_versions.append(group)
        else:
            inactive_versions.append(group)

    # 按结束时间倒序排序
    active_versions.sort(key=lambda x: x['end_time'], reverse=True)
    inactive_versions.sort(key=lambda x: x['end_time'], reverse=True)

    return render_template('merged_project_view.html',
                         project=project,
                         repositories=repositories,
                         active_versions=active_versions,
                         inactive_versions=inactive_versions)
