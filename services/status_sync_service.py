"""
状态同步服务
处理周版本diff和提交记录之间的状态同步
"""
import json
from datetime import datetime, timezone
from typing import List, Dict, Optional, Set
from sqlalchemy import and_, or_
from utils.safe_print import log_print


class StatusSyncService:
    """状态同步服务类"""
    
    def __init__(self, db):
        self.db = db
    
    def sync_commit_to_weekly(self, commit_id: int, new_status: str) -> Dict:
        """
        提交记录状态变更时，同步到周版本diff
        
        Args:
            commit_id: 提交记录ID
            new_status: 新状态 ('pending', 'confirmed', 'rejected')
            
        Returns:
            Dict: 同步结果
        """
        try:
            from app import Commit, WeeklyVersionDiffCache, WeeklyVersionConfig
            
            # 获取提交记录
            commit = self.db.session.get(Commit, commit_id)
            if not commit:
                return {'success': False, 'message': '提交记录不存在'}
            
            log_print(f"同步提交状态到周版本: commit_id={commit_id}, status={new_status}", 'SYNC')
            
            # 查找相关的周版本diff缓存
            # 通过commit_id和file_path匹配
            weekly_caches = self._find_related_weekly_caches(commit)
            
            if not weekly_caches:
                log_print(f"未找到相关的周版本diff缓存: {commit.path}", 'SYNC')
                return {'success': True, 'message': '无相关周版本记录', 'updated_count': 0}
            
            updated_count = 0
            for cache in weekly_caches:
                # 检查是否为合并diff
                if self._is_merged_diff(cache):
                    # 合并diff需要特殊处理
                    updated = self._sync_merged_diff_status(cache, commit, new_status)
                else:
                    # 单个提交的diff直接同步
                    updated = self._sync_single_diff_status(cache, new_status)
                
                if updated:
                    updated_count += 1
            
            self.db.session.commit()
            
            log_print(f"提交状态同步完成: 更新了 {updated_count} 个周版本记录", 'SYNC')
            return {'success': True, 'message': f'同步成功，更新了 {updated_count} 个周版本记录', 'updated_count': updated_count}
            
        except Exception as e:
            self.db.session.rollback()
            log_print(f"提交状态同步失败: {e}", 'ERROR', force=True)
            return {'success': False, 'message': str(e)}
    
    def sync_weekly_to_commit(self, config_id: int, file_path: str, new_status: str) -> Dict:
        """
        周版本diff状态变更时，同步到提交记录
        
        Args:
            config_id: 周版本配置ID
            file_path: 文件路径
            new_status: 新状态 ('pending', 'confirmed', 'rejected')
            
        Returns:
            Dict: 同步结果
        """
        try:
            from app import Commit, WeeklyVersionDiffCache, WeeklyVersionConfig
            
            # 获取周版本diff缓存
            cache = self.db.session.query(WeeklyVersionDiffCache).filter_by(
                config_id=config_id,
                file_path=file_path
            ).first()
            
            if not cache:
                return {'success': False, 'message': '周版本记录不存在'}
            
            log_print(f"同步周版本状态到提交: config_id={config_id}, file_path={file_path}, status={new_status}", 'SYNC')
            
            # 获取相关的提交记录
            related_commits = self._find_related_commits(cache)
            
            if not related_commits:
                log_print(f"未找到相关的提交记录: {file_path}", 'SYNC')
                return {'success': True, 'message': '无相关提交记录', 'updated_count': 0}
            
            updated_count = 0
            for commit in related_commits:
                if commit.status != new_status:
                    commit.status = new_status
                    updated_count += 1
                    log_print(f"更新提交状态: commit_id={commit.id}, path={commit.path}, status={new_status}", 'SYNC')
            
            self.db.session.commit()
            
            log_print(f"周版本状态同步完成: 更新了 {updated_count} 个提交记录", 'SYNC')
            return {'success': True, 'message': f'同步成功，更新了 {updated_count} 个提交记录', 'updated_count': updated_count}
            
        except Exception as e:
            self.db.session.rollback()
            log_print(f"周版本状态同步失败: {e}", 'ERROR', force=True)
            return {'success': False, 'message': str(e)}
    
    def _find_related_weekly_caches(self, commit) -> List:
        """查找与提交记录相关的周版本diff缓存"""
        from app import WeeklyVersionDiffCache, WeeklyVersionConfig
        
        # 通过文件路径和时间范围查找相关的周版本配置
        weekly_caches = self.db.session.query(WeeklyVersionDiffCache).join(
            WeeklyVersionConfig, WeeklyVersionDiffCache.config_id == WeeklyVersionConfig.id
        ).filter(
            and_(
                WeeklyVersionDiffCache.repository_id == commit.repository_id,
                WeeklyVersionDiffCache.file_path == commit.path,
                WeeklyVersionConfig.start_time <= commit.commit_time,
                WeeklyVersionConfig.end_time >= commit.commit_time
            )
        ).all()
        
        return weekly_caches
    
    def _find_related_commits(self, cache) -> List:
        """查找与周版本diff缓存相关的提交记录"""
        from app import Commit
        
        # 获取所有相关的commit_id
        commit_ids = set()
        
        # 添加base_commit_id和latest_commit_id
        if cache.base_commit_id:
            commit_ids.add(cache.base_commit_id)
        if cache.latest_commit_id:
            commit_ids.add(cache.latest_commit_id)
        
        # 如果是合并diff，还需要获取中间的提交
        if self._is_merged_diff(cache):
            # 获取时间范围内的所有相关提交
            from app import WeeklyVersionConfig
            config = self.db.session.get(WeeklyVersionConfig, cache.config_id)
            if config:
                # 查找该文件在时间范围内的所有提交
                all_file_commits = self.db.session.query(Commit).filter(
                    and_(
                        Commit.repository_id == cache.repository_id,
                        Commit.path == cache.file_path,
                        Commit.commit_time >= config.start_time,
                        Commit.commit_time <= config.end_time
                    )
                ).order_by(Commit.commit_time.asc()).all()

                # 添加所有相关的commit_id
                for commit in all_file_commits:
                    commit_ids.add(commit.commit_id)
        
        # 查询相关的提交记录
        if commit_ids:
            commits = self.db.session.query(Commit).filter(
                and_(
                    Commit.repository_id == cache.repository_id,
                    Commit.path == cache.file_path,
                    Commit.commit_id.in_(commit_ids)
                )
            ).all()
        else:
            commits = []
        
        return commits

    def _is_merged_diff(self, cache) -> bool:
        """判断是否为合并diff"""
        # 如果commit_count > 1，说明是合并diff
        return cache.commit_count > 1

    def _sync_single_diff_status(self, cache, new_status: str) -> bool:
        """同步单个diff的状态"""
        if cache.overall_status != new_status:
            # 更新确认状态
            confirmation_status = json.loads(cache.confirmation_status) if cache.confirmation_status else {}
            confirmation_status['dev'] = new_status

            cache.confirmation_status = json.dumps(confirmation_status)
            cache.overall_status = new_status
            cache.updated_at = datetime.now(timezone.utc)

            log_print(f"更新周版本diff状态: {cache.file_path}, status={new_status}", 'SYNC')
            return True
        return False

    def _sync_merged_diff_status(self, cache, commit, new_status: str) -> bool:
        """同步合并diff的状态（复杂逻辑）"""
        # 获取所有相关的提交记录
        related_commits = self._find_related_commits(cache)

        log_print(f"合并diff状态同步: {cache.file_path}, 相关提交数: {len(related_commits)}, 新状态: {new_status}", 'SYNC')

        if new_status == 'confirmed':
            # 如果是确认状态，检查是否所有相关提交都已确认
            all_confirmed = all(c.status == 'confirmed' or c.id == commit.id for c in related_commits)
            log_print(f"所有提交都已确认: {all_confirmed}", 'SYNC')
            if all_confirmed:
                return self._sync_single_diff_status(cache, 'confirmed')
        elif new_status == 'rejected':
            # 如果是拒绝状态，直接设置为拒绝
            log_print(f"设置合并diff为拒绝状态", 'SYNC')
            return self._sync_single_diff_status(cache, 'rejected')
        elif new_status == 'pending':
            # 如果是待确认状态，检查是否有其他提交还是确认状态
            has_confirmed = any(c.status == 'confirmed' and c.id != commit.id for c in related_commits)
            log_print(f"其他提交仍有确认状态: {has_confirmed}", 'SYNC')
            if not has_confirmed:
                return self._sync_single_diff_status(cache, 'pending')

        log_print(f"合并diff状态不需要更新", 'SYNC')
        return False

    def clear_all_confirmation_status(self) -> Dict:
        """清空所有文件的确认状态"""
        try:
            from app import Commit, WeeklyVersionDiffCache

            log_print("开始清空所有确认状态", 'SYNC')

            # 重置所有提交记录状态
            commit_count = self.db.session.query(Commit).filter(Commit.status != 'pending').update(
                {'status': 'pending'}, synchronize_session=False
            )

            # 重置所有周版本diff状态
            weekly_caches = self.db.session.query(WeeklyVersionDiffCache).filter(
                WeeklyVersionDiffCache.overall_status != 'pending'
            ).all()

            weekly_count = 0
            for cache in weekly_caches:
                cache.confirmation_status = json.dumps({"dev": "pending"})
                cache.overall_status = 'pending'
                cache.updated_at = datetime.now(timezone.utc)
                weekly_count += 1

            self.db.session.commit()

            log_print(f"清空确认状态完成: 提交记录 {commit_count} 个，周版本记录 {weekly_count} 个", 'SYNC')
            return {
                'success': True,
                'message': f'清空完成，重置了 {commit_count} 个提交记录和 {weekly_count} 个周版本记录',
                'commit_count': commit_count,
                'weekly_count': weekly_count
            }

        except Exception as e:
            self.db.session.rollback()
            log_print(f"清空确认状态失败: {e}", 'ERROR', force=True)
            return {'success': False, 'message': str(e)}

    def get_sync_mapping_info(self, config_id: int = None, repository_id: int = None, project_id: int = None) -> Dict:
        """获取同步映射信息，用于调试和监控"""
        try:
            from app import Commit, WeeklyVersionDiffCache, WeeklyVersionConfig, Repository

            query = self.db.session.query(WeeklyVersionDiffCache)

            if config_id:
                query = query.filter_by(config_id=config_id)
            elif repository_id:
                # 通过repository_id查找相关的config_id
                configs = self.db.session.query(WeeklyVersionConfig).filter_by(repository_id=repository_id).all()
                config_ids = [c.id for c in configs]
                if config_ids:
                    query = query.filter(WeeklyVersionDiffCache.config_id.in_(config_ids))
                else:
                    return {'success': True, 'mapping_info': []}
            elif project_id:
                # 通过project_id查找相关的repository_id，再查找config_id
                repositories = self.db.session.query(Repository).filter_by(project_id=project_id).all()
                repository_ids = [r.id for r in repositories]
                if repository_ids:
                    configs = self.db.session.query(WeeklyVersionConfig).filter(WeeklyVersionConfig.repository_id.in_(repository_ids)).all()
                    config_ids = [c.id for c in configs]
                    if config_ids:
                        query = query.filter(WeeklyVersionDiffCache.config_id.in_(config_ids))
                    else:
                        return {'success': True, 'mapping_info': []}
                else:
                    return {'success': True, 'mapping_info': []}

            weekly_caches = query.all()

            mapping_info = []
            for cache in weekly_caches:
                related_commits = self._find_related_commits(cache)

                mapping_info.append({
                    'file_path': cache.file_path,
                    'weekly_status': cache.overall_status,
                    'commit_count': len(related_commits),
                    'commit_statuses': [c.status for c in related_commits],
                    'is_merged_diff': self._is_merged_diff(cache),
                    'base_commit_id': cache.base_commit_id,
                    'latest_commit_id': cache.latest_commit_id
                })

            return {'success': True, 'mapping_info': mapping_info}

        except Exception as e:
            log_print(f"获取同步映射信息失败: {e}", 'ERROR', force=True)
            return {'success': False, 'message': str(e)}
