"""
状态同步服务
处理周版本diff和提交记录之间的状态同步
"""
import json
from datetime import datetime, timezone
from typing import Dict, List
from sqlalchemy import and_, or_
from services.model_loader import get_runtime_models
from services.weekly_file_status import WEEKLY_FILE_STATUSES, is_valid_weekly_file_status
from utils.safe_print import log_print
from utils.timezone_utils import beijing_window_to_utc_naive


class StatusSyncService:
    """状态同步服务类"""
    
    def __init__(self, db):
        self.db = db
    
    def sync_commit_to_weekly(self, commit_id: int, new_status: str, auto_commit: bool = True) -> Dict:
        """
        提交记录状态变更时，同步到周版本diff

        注意**提交状态域比周版本文件状态域大**：提交侧还接受 'reviewed'
        （services/commit_status_api_service.py，界面「已查看」）。'reviewed'
        不属于周版本文件状态，过去会被原样写进 cache.overall_status —— 周版本页
        的筛选与 weekly_version_stats_api 都只认三态，该文件随即从筛选和统计里
        消失。所以这里显式跳过而不是落库，并说明原因。

        Args:
            commit_id: 提交记录ID
            new_status: 新状态，必须是 WEEKLY_FILE_STATUSES 之一才会同步
            auto_commit: 是否由本方法提交事务。批量入口逐条调用它时传 False，
                把事务收敛到最外层提交一次 —— 否则循环里每条各 commit，一旦后面
                的权限校验/同步失败，前面已经落库的改动回滚不掉（见
                services/commit_status_apply.py 的「事务边界」说明）。
                默认 True：既有调用方行为不变。

        Returns:
            Dict: 同步结果
        """
        if not is_valid_weekly_file_status(new_status):
            log_print(
                f"提交状态 {new_status!r} 不属于周版本文件状态（{', '.join(WEEKLY_FILE_STATUSES)}），"
                f"跳过周版本同步: commit_id={commit_id}",
                'SYNC',
            )
            return {
                'success': True,
                'message': f'状态 {new_status} 不属于周版本文件状态，跳过同步',
                'updated_count': 0,
                'skipped': True,
            }

        try:
            Commit, = get_runtime_models("Commit")
            
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
                operator_username = commit.status_changed_by if new_status in ('confirmed', 'rejected') else None
                # 检查是否为合并diff
                if self._is_merged_diff(cache):
                    # 合并diff需要特殊处理
                    updated = self._sync_merged_diff_status(cache, commit, new_status, operator_username)
                else:
                    # 单个提交的diff直接同步
                    updated = self._sync_single_diff_status(cache, new_status, operator_username)
                
                if updated:
                    updated_count += 1
            
            if auto_commit:
                self.db.session.commit()

            log_print(f"提交状态同步完成: 更新了 {updated_count} 个周版本记录", 'SYNC')
            return {'success': True, 'message': f'同步成功，更新了 {updated_count} 个周版本记录', 'updated_count': updated_count}

        except Exception as e:
            if auto_commit:
                # auto_commit=False 时事务归调用方所有：这里回滚会把调用方（批量入口）
                # 前面已经写好的提交状态一起丢掉。失败以返回值上报，由调用方决定
                # 是整体回滚还是继续。
                self.db.session.rollback()
            log_print(f"提交状态同步失败: {e}", 'ERROR', force=True)
            return {'success': False, 'message': str(e)}
    
    def sync_weekly_to_commit(self, config_id: int, file_path: str, new_status: str) -> Dict:
        """
        周版本diff状态变更时，同步到提交记录

        服务层自校验取值：接口层（weekly_version_file_status_api）虽然也判了，
        但本方法可能被直接调用（批量确认、运维脚本、后续新增入口），只靠接口层
        校验等于把脏值防线放在错误的层 —— 这里会写 Commit.status，脏值一旦落库，
        提交列表/比较页只能渲染成「其他」且无法通过界面纠正。

        Args:
            config_id: 周版本配置ID
            file_path: 文件路径
            new_status: 新状态，必须是 WEEKLY_FILE_STATUSES 之一

        Returns:
            Dict: 同步结果
        """
        if not is_valid_weekly_file_status(new_status):
            log_print(
                f"拒绝同步非法周版本状态: config_id={config_id}, file_path={file_path}, "
                f"status={new_status!r}（仅支持 {', '.join(WEEKLY_FILE_STATUSES)}）",
                'SYNC',
                force=True,
            )
            return {
                'success': False,
                'message': f'无效的周版本状态: {new_status!r}',
                'updated_count': 0,
            }

        try:
            WeeklyVersionDiffCache, = get_runtime_models("WeeklyVersionDiffCache")
            
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
            
            # 周版本操作人（由 weekly_version_file_status_api 写入 cache.status_changed_by）
            operator_username = cache.status_changed_by if new_status in ('confirmed', 'rejected') else None

            updated_count = 0
            for commit in related_commits:
                status_updated = False

                if commit.status != new_status:
                    commit.status = new_status
                    status_updated = True

                # 同步确认用户（pending 状态清空）
                if commit.status_changed_by != operator_username:
                    commit.status_changed_by = operator_username
                    status_updated = True

                if status_updated:
                    updated_count += 1
                    log_print(
                        f"更新提交状态: commit_id={commit.id}, path={commit.path}, "
                        f"status={new_status}, changed_by={operator_username}",
                        'SYNC'
                    )
            
            self.db.session.commit()
            
            log_print(f"周版本状态同步完成: 更新了 {updated_count} 个提交记录", 'SYNC')
            return {'success': True, 'message': f'同步成功，更新了 {updated_count} 个提交记录', 'updated_count': updated_count}
            
        except Exception as e:
            self.db.session.rollback()
            log_print(f"周版本状态同步失败: {e}", 'ERROR', force=True)
            return {'success': False, 'message': str(e)}
    
    def _find_related_weekly_caches(self, commit) -> List:
        """查找与提交记录相关的周版本diff缓存

        时区：`WeeklyVersionConfig.start_time/end_time` 是**北京墙钟**（用户在
        datetime-local 里填的），而 `Commit.commit_time` 是 **naive-UTC 墙钟**。
        原来直接把两列在 SQL 里比大小，会整体偏移 8 小时且不报错 —— 落在窗口
        前 8 小时的提交会匹配不到任何周版本缓存，状态同步因此静默失效。

        这里不能在 SQL 里换算：两侧都是**列**，而 SQLite 与 MySQL 的日期加减
        语法不同（`datetime(col,'+8 hours')` vs `DATE_ADD`）。所以先用
        repository_id + file_path 把候选集缩到「某个文件的若干缓存」（量很小），
        再在 Python 侧用换算后的窗口过滤。
        """
        WeeklyVersionDiffCache, WeeklyVersionConfig = get_runtime_models(
            "WeeklyVersionDiffCache",
            "WeeklyVersionConfig",
        )

        candidates = self.db.session.query(
            WeeklyVersionDiffCache, WeeklyVersionConfig
        ).join(
            WeeklyVersionConfig, WeeklyVersionDiffCache.config_id == WeeklyVersionConfig.id
        ).filter(
            and_(
                WeeklyVersionDiffCache.repository_id == commit.repository_id,
                WeeklyVersionDiffCache.file_path == commit.path,
            )
        ).all()

        commit_time = commit.commit_time
        weekly_caches = []
        for cache, config in candidates:
            start_utc, end_utc = beijing_window_to_utc_naive(config.start_time, config.end_time)
            if start_utc is None or end_utc is None:
                # 没有可用窗口的配置不参与匹配（原来这些会被 SQL 的 NULL 比较静默排除）
                continue
            if start_utc <= commit_time <= end_utc:
                weekly_caches.append(cache)

        return weekly_caches
    
    def _find_related_commits(self, cache) -> List:
        """查找与周版本diff缓存「审核集合」相关的提交记录 = **审核窗口内**该文件的提交。

        ## 为什么把 cache.base_commit_id 排除在外

        `base_commit_id` 是窗口起点**之前**的最后一个提交
        （weekly_version_logic.generate_weekly_merged_diff 里按
        `commit_time < 窗口起点 order_by desc` 选出来的），它是上一个周版本留下的
        既有状态，**不属于本周审核对象**。原先无条件把它并进这个集合，两条链路都被它
        污染：

          - `sync_weekly_to_commit`：确认本周 N 条变更时会连历史基准一起改，
            窗口前的审核结果被本周操作覆盖（一次操作更新 N+1 条）；
          - `sync_commit_to_weekly`：聚合口径里混进窗口外的提交，
            窗口内全部确认也凑不齐 confirmed。

        比较基准与本周审核集合必须分离，所以这里只按时间窗口取数。

        时区：窗口是北京墙钟、`commit_time` 是 naive-UTC 墙钟，必须先换算（理由与
        `_find_related_weekly_caches` 的说明相同）。
        """
        Commit, WeeklyVersionConfig = get_runtime_models("Commit", "WeeklyVersionConfig")

        config = self.db.session.get(WeeklyVersionConfig, cache.config_id)
        if not config:
            # 配置已不存在（缓存成了孤儿）时无法界定窗口，宁可不改任何提交，也不能
            # 退化成「谁都不看」地乱改。
            return []

        # 窗口是北京墙钟、commit_time 是 naive-UTC，必须先换算（见本文件另一处说明）
        start_utc, end_utc = beijing_window_to_utc_naive(config.start_time, config.end_time)
        if start_utc is None or end_utc is None:
            log_print(f"周版本窗口缺失，跳过审核集合查询: {cache.file_path}", 'SYNC')
            return []

        return self.db.session.query(Commit).filter(
            and_(
                Commit.repository_id == cache.repository_id,
                Commit.path == cache.file_path,
                Commit.commit_time >= start_utc,
                Commit.commit_time <= end_utc
            )
        ).order_by(Commit.commit_time.asc()).all()

    def _is_merged_diff(self, cache) -> bool:
        """判断是否为合并diff"""
        # 如果commit_count > 1，说明是合并diff
        return cache.commit_count > 1

    def _sync_single_diff_status(self, cache, new_status: str, operator_username: str = None) -> bool:
        """同步单个diff的状态。

        最后一道写库防线：即使调用方绕过了 sync_commit_to_weekly /
        sync_weekly_to_commit 的入口校验（新增调用点、以后重构），非法值也不会
        落到 cache.overall_status / confirmation_status 上。
        """
        if not is_valid_weekly_file_status(new_status):
            log_print(
                f"拒绝写入非法周版本文件状态: file_path={getattr(cache, 'file_path', None)}, "
                f"status={new_status!r}（仅支持 {', '.join(WEEKLY_FILE_STATUSES)}）",
                'SYNC',
                force=True,
            )
            return False
        if cache.overall_status != new_status or cache.status_changed_by != operator_username:
            # 更新确认状态
            confirmation_status = json.loads(cache.confirmation_status) if cache.confirmation_status else {}
            confirmation_status['dev'] = new_status

            cache.confirmation_status = json.dumps(confirmation_status)
            cache.overall_status = new_status
            cache.status_changed_by = operator_username
            cache.updated_at = datetime.now(timezone.utc)

            log_print(f"更新周版本diff状态: {cache.file_path}, status={new_status}", 'SYNC')
            return True
        return False

    def _sync_merged_diff_status(self, cache, commit, new_status: str, operator_username: str = None) -> bool:
        """同步合并diff的状态：按审核窗口内的**所有**提交重算，而不是按本次变更分支推算。

        ## 聚合语义（拒绝优先）

            窗口内存在 rejected            → rejected
            窗口内全部 confirmed           → confirmed
            其余（含不全、空窗口）          → pending

        ## 为什么必须重算

        原实现是「按 new_status 分支 + 保留旧值」：

          - 退回 pending 的分支只问「还有没有别的 confirmed」，对 rejected
            完全不看 → 窗口内还有拒绝提交，周聚合却被清成待确认；
          - 反过来，「还有别的 confirmed」就整段不更新 → 一条提交退回待确认后，
            周聚合仍停在已确认；
          - 确认分支要求「除本次外的都 confirmed 才更新」，同样只看单向，
            任何一次状态回退都不会让已确认/已拒绝的周聚合降下来。

        这些分支各自只覆盖自己那一侧，必然留下与提交状态不一致的脏聚合值。
        改为每次由窗口内提交集合重算，任何一次状态变更都会把聚合拉回正确值 ——
        逻辑是幂等的，重复同步不会产生额外写入（`_sync_single_diff_status`
        只在状态或操作者变化时返回 True）。

        `commit` 是本次被改的提交：调用方已把它的状态改成 `new_status`
        （见 commit_status_api_service / commit_operation_handlers，先落库再同步），
        这里再显式覆盖一次，保证聚合口径与本次操作一致，不依赖会话脏值的可见性。
        """
        # 审核集合 = 窗口内该文件的提交（不含窗口前的比较基准，见 _find_related_commits）
        window_commits = self._find_related_commits(cache)
        statuses = [
            new_status if c.id == commit.id else (c.status or 'pending')
            for c in window_commits
        ]

        if any(status == 'rejected' for status in statuses):
            overall_status = 'rejected'
        elif statuses and all(status == 'confirmed' for status in statuses):
            overall_status = 'confirmed'
        else:
            overall_status = 'pending'

        log_print(
            f"合并diff状态重算: {cache.file_path}, 窗口内提交数: {len(statuses)}, "
            f"提交状态: {statuses}, 聚合结果: {overall_status}",
            'SYNC',
        )

        # 回到 pending 时必须清空操作者（聚合后的 pending 不代表任何人的确认/拒绝）
        effective_operator = operator_username if overall_status in ('confirmed', 'rejected') else None
        return self._sync_single_diff_status(cache, overall_status, effective_operator)

    def clear_all_confirmation_status(self) -> Dict:
        """清空所有文件的确认状态"""
        try:
            Commit, WeeklyVersionDiffCache = get_runtime_models("Commit", "WeeklyVersionDiffCache")

            log_print("开始清空所有确认状态", 'SYNC')

            # 重置所有提交记录状态
            commit_count = self.db.session.query(Commit).filter(Commit.status != 'pending').update(
                {'status': 'pending', 'status_changed_by': None}, synchronize_session=False
            )

            # 重置所有周版本diff状态
            #
            # 过滤条件必须带上「状态已是 pending、操作者却还留着」的行：
            # weekly_version_logic.generate_weekly_merged_diff 在 latest_commit 变化时
            # 只把状态重置成 pending，status_changed_by 是留下的（见该文件同名注释），
            # 这类行只按 `overall_status != 'pending'` 过滤会被整行跳过 ——
            # 于是「清空所有确认状态」跑完，界面上仍显示着上一个确认人。
            # overall_status 为 NULL 的历史行同样要覆盖（`!= 'pending'` 在 SQL 里
            # 对 NULL 不成立，会被静默漏掉）。
            weekly_caches = self.db.session.query(WeeklyVersionDiffCache).filter(
                or_(
                    WeeklyVersionDiffCache.overall_status.is_(None),
                    WeeklyVersionDiffCache.overall_status != 'pending',
                    WeeklyVersionDiffCache.status_changed_by.isnot(None),
                )
            ).all()

            weekly_count = 0
            for cache in weekly_caches:
                cache.confirmation_status = json.dumps({"dev": "pending"})
                cache.overall_status = 'pending'
                # 与上面提交记录那条 UPDATE 保持一致：状态回到待确认，操作者必须一起清掉。
                # 只重置状态会留下「待确认 + 有确认人」的自相矛盾记录。
                cache.status_changed_by = None
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
            (
                Commit,
                WeeklyVersionDiffCache,
                WeeklyVersionConfig,
                Repository,
            ) = get_runtime_models(
                "Commit",
                "WeeklyVersionDiffCache",
                "WeeklyVersionConfig",
                "Repository",
            )

            query = self.db.session.query(WeeklyVersionDiffCache)

            # empty_reason 用于前端显示精准的空状态提示
            empty_reason = None

            if config_id:
                query = query.filter_by(config_id=config_id)
            elif repository_id:
                # 通过repository_id查找相关的config_id
                configs = self.db.session.query(WeeklyVersionConfig).filter_by(repository_id=repository_id).all()
                config_ids = [c.id for c in configs]
                if config_ids:
                    query = query.filter(WeeklyVersionDiffCache.config_id.in_(config_ids))
                else:
                    return {'success': True, 'mapping_info': [], 'empty_reason': 'no_weekly_config'}
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
                        empty_reason = 'no_weekly_config'
                        return {'success': True, 'mapping_info': [], 'empty_reason': empty_reason}
                else:
                    return {'success': True, 'mapping_info': [], 'empty_reason': 'no_repository'}

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


