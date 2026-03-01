#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
增量缓存系统实现
解决手动获取数据时重复生成缓存的问题
"""

from datetime import datetime, timezone
from sqlalchemy import text
from services.model_loader import get_runtime_models

class IncrementalCacheManager:
    """增量缓存管理器"""
    
    def __init__(self):
        self.diff_logic_version = "1.7.0"  # 从app.py获取当前版本
    
    def add_repository_sync_fields(self):
        """为Repository表添加同步跟踪字段"""
        db = None
        try:
            app, db = get_runtime_models("app", "db")
            
            with app.app_context():
                # 检查字段是否已存在
                inspector = db.inspect(db.engine)
                columns = [col['name'] for col in inspector.get_columns('repository')]
                
                fields_to_add = []
                if 'last_sync_commit_id' not in columns:
                    fields_to_add.append('last_sync_commit_id VARCHAR(100)')
                if 'last_sync_time' not in columns:
                    fields_to_add.append('last_sync_time DATETIME')
                if 'cache_version' not in columns:
                    fields_to_add.append('cache_version VARCHAR(20)')
                if 'sync_mode' not in columns:
                    fields_to_add.append('sync_mode VARCHAR(20) DEFAULT "full"')
                
                if fields_to_add:
                    for field in fields_to_add:
                        sql = f'ALTER TABLE repository ADD COLUMN {field}'
                        db.session.execute(text(sql))
                    
                    db.session.commit()
                    print(f"✅ 成功添加Repository同步跟踪字段: {', '.join(fields_to_add)}")
                else:
                    print("✅ Repository同步跟踪字段已存在")
                    
        except Exception as e:
            print(f"❌ 添加Repository同步跟踪字段失败: {e}")
            if db is not None:
                db.session.rollback()
    
    def get_new_commits_since_last_sync(self, repository, service):
        """获取自上次同步以来的新提交"""
        try:
            # 获取仓库的最新提交ID
            all_commits = service.get_commits(limit=1000)
            if not all_commits:
                return []
            
            latest_commit_id = all_commits[0]['commit_id']
            
            # 检查是否需要增量更新
            if (repository.last_sync_commit_id and 
                repository.cache_version == self.diff_logic_version and
                repository.last_sync_commit_id == latest_commit_id):
                print(f"📋 仓库 {repository.name} 无新提交，跳过同步")
                return []
            
            # 如果是首次同步或版本不匹配，返回所有提交
            if (not repository.last_sync_commit_id or 
                repository.cache_version != self.diff_logic_version):
                print(f"🔄 仓库 {repository.name} 首次同步或版本不匹配，执行全量同步")
                return all_commits
            
            # 找到上次同步的提交位置
            last_sync_index = None
            for i, commit in enumerate(all_commits):
                if commit['commit_id'] == repository.last_sync_commit_id:
                    last_sync_index = i
                    break
            
            if last_sync_index is None:
                print(f"⚠️ 未找到上次同步提交 {repository.last_sync_commit_id}，执行全量同步")
                return all_commits
            
            # 返回新提交（从0到last_sync_index，不包含last_sync_index）
            new_commits = all_commits[:last_sync_index]
            print(f"📊 仓库 {repository.name} 发现 {len(new_commits)} 个新提交")
            return new_commits
            
        except Exception as e:
            print(f"❌ 获取新提交失败: {e}")
            return all_commits  # 失败时回退到全量同步
    
    def get_existing_cached_files(self, repository_id):
        """获取已有缓存的文件列表"""
        try:
            DiffCache, = get_runtime_models("DiffCache")
            
            cached_files = set()
            
            # 从DiffCache表获取已缓存的文件
            cached_diffs = DiffCache.query.filter_by(
                repository_id=repository_id,
                diff_logic_version=self.diff_logic_version,
                diff_version=self.diff_logic_version
            ).all()
            
            for cache in cached_diffs:
                cached_files.add(f"{cache.commit_id}:{cache.file_path}")
            
            print(f"📁 仓库 {repository_id} 已有 {len(cached_files)} 个文件缓存")
            return cached_files
            
        except Exception as e:
            print(f"❌ 获取已有缓存失败: {e}")
            return set()
    
    def incremental_sync_repository(self, repository_id):
        """增量同步仓库"""
        db = None
        try:
            (
                Repository,
                Commit,
                db,
                excel_cache_service,
                get_git_service,
                get_svn_service,
                add_excel_diff_task,
            ) = get_runtime_models(
                "Repository",
                "Commit",
                "db",
                "excel_cache_service",
                "get_git_service",
                "get_svn_service",
                "add_excel_diff_task",
            )
            
            repository = Repository.query.get_or_404(repository_id)
            print(f"🔄 开始增量同步仓库: {repository.name}")
            
            # 创建服务实例（使用缓存）
            if repository.type == 'git':
                service = get_git_service(repository)
            elif repository.type == 'svn':
                service = get_svn_service(repository)
            else:
                raise ValueError(f"不支持的仓库类型: {repository.type}")
            
            # 先更新仓库代码
            success, message = (service.clone_or_update_repository() 
                              if repository.type == 'git' 
                              else service.checkout_or_update_repository())
            
            if not success:
                return False, f"仓库更新失败: {message}"
            
            # 获取新提交
            new_commits = self.get_new_commits_since_last_sync(repository, service)
            
            if not new_commits:
                return True, "无新提交，同步完成"
            
            # 获取已有缓存
            existing_cached_files = self.get_existing_cached_files(repository_id)
            
            # 处理新提交
            new_commit_count = 0
            new_excel_tasks = 0
            
            for commit_data in new_commits:
                # 检查提交是否已存在
                existing_commit = Commit.query.filter_by(
                    repository_id=repository_id,
                    commit_id=commit_data['commit_id'],
                    path=commit_data['path']
                ).first()
                
                if not existing_commit:
                    # 插入新提交记录
                    new_commit = Commit(
                        repository_id=repository_id,
                        commit_id=commit_data['commit_id'],
                        path=commit_data['path'],
                        version=commit_data['version'],
                        operation=commit_data['operation'],
                        author=commit_data['author'],
                        commit_time=commit_data['commit_time'],
                        message=commit_data['message']
                    )
                    db.session.add(new_commit)
                    new_commit_count += 1
                
                # 检查是否为Excel文件且未缓存
                if excel_cache_service.is_excel_file(commit_data['path']):
                    cache_key = f"{commit_data['commit_id']}:{commit_data['path']}"
                    
                    if cache_key not in existing_cached_files:
                        # 添加Excel缓存任务
                        add_excel_diff_task(
                            repository_id, 
                            commit_data['commit_id'], 
                            commit_data['path'], 
                            priority=10
                        )
                        new_excel_tasks += 1
            
            # 更新仓库同步状态
            repository.last_sync_commit_id = new_commits[0]['commit_id']
            repository.last_sync_time = datetime.now(timezone.utc)
            repository.cache_version = self.diff_logic_version
            repository.sync_mode = 'incremental'
            
            db.session.commit()
            
            message = f"增量同步完成: 新增 {new_commit_count} 个提交，{new_excel_tasks} 个Excel缓存任务"
            print(f"✅ {message}")
            
            return True, message
            
        except Exception as e:
            print(f"❌ 增量同步失败: {e}")
            if db is not None:
                db.session.rollback()
            return False, f"增量同步失败: {str(e)}"
    
    def force_full_sync(self, repository_id):
        """强制全量同步（清空重建）"""
        db = None
        try:
            Repository, Commit, DiffCache, db = get_runtime_models(
                "Repository",
                "Commit",
                "DiffCache",
                "db",
            )
            
            repository = Repository.query.get_or_404(repository_id)
            print(f"🔄 开始全量同步仓库: {repository.name}")
            
            # 清空现有数据
            Commit.query.filter_by(repository_id=repository_id).delete()
            DiffCache.query.filter_by(repository_id=repository_id).delete()
            
            # 重置同步状态
            repository.last_sync_commit_id = None
            repository.last_sync_time = None
            repository.cache_version = None
            repository.sync_mode = 'full'
            
            db.session.commit()
            
            # 执行增量同步（此时会变成全量）
            return self.incremental_sync_repository(repository_id)
            
        except Exception as e:
            print(f"❌ 全量同步失败: {e}")
            if db is not None:
                db.session.rollback()
            return False, f"全量同步失败: {str(e)}"

# 全局实例
incremental_cache_manager = IncrementalCacheManager()

if __name__ == "__main__":
    # 初始化数据库字段
    incremental_cache_manager.add_repository_sync_fields()
    
    # 测试增量同步
    # success, message = incremental_cache_manager.incremental_sync_repository(2)
    # print(f"同步结果: {success}, 消息: {message}")
