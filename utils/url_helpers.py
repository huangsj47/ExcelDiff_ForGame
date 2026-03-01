"""URL生成辅助函数"""
from flask import url_for

def generate_commit_diff_url(commit, use_new_format=True):
    """
    生成commit diff的URL
    
    Args:
        commit: Commit对象
        use_new_format: 是否使用新的URL格式（包含项目代号和仓库名）
    
    Returns:
        str: 生成的URL
    """
    if use_new_format and hasattr(commit, 'repository') and commit.repository:
        repository = commit.repository
        if hasattr(repository, 'project') and repository.project:
            project = repository.project
            # 使用项目代号和仓库名生成新格式URL
            return url_for('commit_diff_with_path', 
                          project_code=project.code, 
                          repository_name=repository.name, 
                          commit_id=commit.id)
    
    # 回退到原始格式
    return url_for('commit_diff', commit_id=commit.id)

def generate_excel_diff_data_url(commit, use_new_format=True):
    """
    生成Excel diff数据的URL
    
    Args:
        commit: Commit对象
        use_new_format: 是否使用新的URL格式
    
    Returns:
        str: 生成的URL
    """
    if use_new_format and hasattr(commit, 'repository') and commit.repository:
        repository = commit.repository
        if hasattr(repository, 'project') and repository.project:
            project = repository.project
            return url_for('get_excel_diff_data_with_path',
                          project_code=project.code,
                          repository_name=repository.name,
                          commit_id=commit.id)
    
    return url_for('get_excel_diff_data', commit_id=commit.id)

def generate_refresh_diff_url(commit, use_new_format=True):
    """
    生成刷新diff的URL
    
    Args:
        commit: Commit对象
        use_new_format: 是否使用新的URL格式
    
    Returns:
        str: 生成的URL
    """
    if use_new_format and hasattr(commit, 'repository') and commit.repository:
        repository = commit.repository
        if hasattr(repository, 'project') and repository.project:
            project = repository.project
            return url_for('refresh_commit_diff_with_path',
                          project_code=project.code,
                          repository_name=repository.name,
                          commit_id=commit.id)
    
    return url_for('refresh_commit_diff', commit_id=commit.id)
