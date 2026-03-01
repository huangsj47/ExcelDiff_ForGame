const express = require('express');
const router = express.Router();
const sqlite3 = require('sqlite3').verbose();
const path = require('path');
const fs = require('fs');
const axios = require('axios');

const dbPath = path.join(__dirname, '../database.db');
const db = new sqlite3.Database(dbPath);

// 检查本地仓库是否存在的函数
function checkLocalRepository(projectCode, repositoryName, repositoryId) {
    const localPath = path.join('repos', `${projectCode}_${repositoryName}_${repositoryId}`);
    return fs.existsSync(localPath);
}

// 获取项目的所有仓库
router.get('/project/:projectId', (req, res) => {
    const projectId = req.params.projectId;
    
    db.all('SELECT * FROM repositories WHERE project_id = ? ORDER BY created_at DESC', 
        [projectId], (err, repositories) => {
        if (err) {
            console.error(err);
            return res.status(500).json({ error: '数据库错误' });
        }
        res.json(repositories);
    });
});

// 仓库配置页面
router.get('/project/:projectId/config', (req, res) => {
    const projectId = req.params.projectId;
    
    db.get('SELECT * FROM projects WHERE id = ?', [projectId], (err, project) => {
        if (err) {
            console.error(err);
            return res.status(500).send('数据库错误');
        }
        if (!project) {
            return res.status(404).send('项目不存在');
        }
        
        db.all('SELECT * FROM repositories WHERE project_id = ?', [projectId], (err, repositories) => {
            if (err) {
                console.error(err);
                return res.status(500).send('数据库错误');
            }
            res.render('repository-config', { project, repositories });
        });
    });
});

// 新增Git仓库页面
router.get('/project/:projectId/add-git', (req, res) => {
    const projectId = req.params.projectId;
    
    db.get('SELECT * FROM projects WHERE id = ?', [projectId], (err, project) => {
        if (err) {
            console.error(err);
            return res.status(500).send('数据库错误');
        }
        if (!project) {
            return res.status(404).send('项目不存在');
        }
        res.render('add-git-repository', { project });
    });
});

// 新增SVN仓库页面
router.get('/project/:projectId/add-svn', (req, res) => {
    const projectId = req.params.projectId;
    
    db.get('SELECT * FROM projects WHERE id = ?', [projectId], (err, project) => {
        if (err) {
            console.error(err);
            return res.status(500).send('数据库错误');
        }
        if (!project) {
            return res.status(404).send('项目不存在');
        }
        res.render('add-svn-repository', { project });
    });
});

// 创建Git仓库
router.post('/git', (req, res) => {
    const {
        project_id, name, category, url, server_url, token, 
        current_date, branch, resource_type, path_regex, 
        log_regex, log_filter_regex, commit_filter, 
        important_tables, unconfirmed_history, delete_table_alert, 
        weekly_version_setting
    } = req.body;

    if (!name || !url || !server_url || !token || !branch || !resource_type) {
        return res.status(400).json({ error: '必填字段不能为空' });
    }

    // 获取项目信息
    db.get('SELECT * FROM projects WHERE id = ?', [project_id], (err, project) => {
        if (err) {
            console.error(err);
            return res.status(500).json({ error: '获取项目信息失败' });
        }
        if (!project) {
            return res.status(404).json({ error: '项目不存在' });
        }

        // 检查是否存在相同配置的仓库
        const checkSql = `SELECT * FROM repositories WHERE project_id = ? AND name = ? AND type = 'git'`;
        db.get(checkSql, [project_id, name], (err, existingRepo) => {
            if (err) {
                console.error(err);
                return res.status(500).json({ error: '检查现有仓库失败' });
            }

            if (existingRepo) {
                // 检查本地仓库是否存在
                const localExists = checkLocalRepository(project.code, name, existingRepo.id);
                
                if (localExists && existingRepo.url === url && existingRepo.branch === branch) {
                    // 配置一致，复用现有仓库并触发更新
                    console.log(`复用现有Git仓库: ${name}, 触发更新操作`);
                    
                    // 调用Python后端触发仓库更新
                    axios.post(`http://localhost:8002/api/repositories/${existingRepo.id}/reuse-and-update`, {
                        action: 'pull_and_cache'
                    }).catch(updateErr => {
                        console.error('触发仓库更新失败:', updateErr);
                    });
                    
                    return res.json({ 
                        id: existingRepo.id, 
                        message: '检测到相同配置的本地仓库，已复用并触发更新',
                        reused: true 
                    });
                }
            }

            // 创建新仓库
            const sql = `INSERT INTO repositories (
                project_id, name, type, category, url, server_url, token, 
                branch, resource_type, path_regex, log_regex, log_filter_regex, 
                commit_filter, important_tables, unconfirmed_history, 
                delete_table_alert, weekly_version_setting
            ) VALUES (?, ?, 'git', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`;

            db.run(sql, [
                project_id, name, category, url, server_url, token, 
                branch, resource_type, path_regex, log_regex, log_filter_regex, 
                commit_filter, important_tables, unconfirmed_history ? 1 : 0, 
                delete_table_alert ? 1 : 0, weekly_version_setting
            ], function(err) {
                if (err) {
                    console.error(err);
                    return res.status(500).json({ error: '创建Git仓库失败' });
                }
                res.json({ id: this.lastID, message: '新Git仓库创建成功' });
            });
        });
    });
});

// 创建SVN仓库
router.post('/svn', (req, res) => {
    const {
        project_id, name, category, url, root_directory, username, 
        password, current_version, resource_type, path_regex, 
        log_regex, log_filter_regex, commit_filter, important_tables, 
        unconfirmed_history, delete_table_alert, weekly_version_setting
    } = req.body;

    if (!name || !url || !root_directory || !username || !password || !current_version || !resource_type) {
        return res.status(400).json({ error: '必填字段不能为空' });
    }

    // 获取项目信息
    db.get('SELECT * FROM projects WHERE id = ?', [project_id], (err, project) => {
        if (err) {
            console.error(err);
            return res.status(500).json({ error: '获取项目信息失败' });
        }
        if (!project) {
            return res.status(404).json({ error: '项目不存在' });
        }

        // 检查是否存在相同配置的仓库
        const checkSql = `SELECT * FROM repositories WHERE project_id = ? AND name = ? AND type = 'svn'`;
        db.get(checkSql, [project_id, name], (err, existingRepo) => {
            if (err) {
                console.error(err);
                return res.status(500).json({ error: '检查现有仓库失败' });
            }

            if (existingRepo) {
                // 检查本地仓库是否存在
                const localExists = checkLocalRepository(project.code, name, existingRepo.id);
                
                if (localExists && existingRepo.url === url && existingRepo.root_directory === root_directory) {
                    // 配置一致，复用现有仓库并触发更新
                    console.log(`复用现有SVN仓库: ${name}, 触发更新操作`);
                    
                    // 调用Python后端触发仓库更新
                    axios.post(`http://localhost:8002/api/repositories/${existingRepo.id}/reuse-and-update`, {
                        action: 'update_and_cache'
                    }).catch(updateErr => {
                        console.error('触发仓库更新失败:', updateErr);
                    });
                    
                    return res.json({ 
                        id: existingRepo.id, 
                        message: '检测到相同配置的本地仓库，已复用并触发更新',
                        reused: true 
                    });
                }
            }

            // 创建新仓库
            const sql = `INSERT INTO repositories (
                project_id, name, type, category, url, root_directory, username, 
                password, current_version, resource_type, path_regex, log_regex, 
                log_filter_regex, commit_filter, important_tables, 
                unconfirmed_history, delete_table_alert, weekly_version_setting
            ) VALUES (?, ?, 'svn', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`;

            db.run(sql, [
                project_id, name, category, url, root_directory, username, 
                password, current_version, resource_type, path_regex, log_regex, 
                log_filter_regex, commit_filter, important_tables, 
                unconfirmed_history ? 1 : 0, delete_table_alert ? 1 : 0, 
                weekly_version_setting
            ], function(err) {
                if (err) {
                    console.error(err);
                    return res.status(500).json({ error: '创建SVN仓库失败' });
                }
                res.json({ id: this.lastID, message: 'SVN仓库创建成功' });
            });
        });
    });
});

// 获取单个仓库详情
router.get('/:id', (req, res) => {
    const repositoryId = req.params.id;
    
    db.get('SELECT * FROM repositories WHERE id = ?', [repositoryId], (err, repository) => {
        if (err) {
            console.error(err);
            return res.status(500).json({ error: '数据库错误' });
        }
        if (!repository) {
            return res.status(404).json({ error: '仓库不存在' });
        }
        res.json(repository);
    });
});

// 删除仓库
router.delete('/:id', (req, res) => {
    const repositoryId = req.params.id;
    
    db.run('DELETE FROM repositories WHERE id = ?', [repositoryId], function(err) {
        if (err) {
            console.error(err);
            return res.status(500).json({ error: '删除仓库失败' });
        }
        if (this.changes === 0) {
            return res.status(404).json({ error: '仓库不存在' });
        }
        res.json({ message: '仓库删除成功' });
    });
});

module.exports = router;








