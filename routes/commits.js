const express = require('express');
const router = express.Router();
const sqlite3 = require('sqlite3').verbose();
const path = require('path');
const simpleGit = require('simple-git');
const svn = require('node-svn-ultimate');
const XLSX = require('xlsx');
const fs = require('fs');
const diff = require('diff');

const dbPath = path.join(__dirname, '../database.db');
const db = new sqlite3.Database(dbPath);

// 获取仓库的提交记录
router.get('/repository/:repositoryId', (req, res) => {
    const repositoryId = req.params.repositoryId;
    const { author, path: filePath, version, operation, status } = req.query;
    
    let sql = 'SELECT * FROM commits WHERE repository_id = ?';
    let params = [repositoryId];
    
    // 添加筛选条件
    if (author) {
        sql += ' AND author LIKE ?';
        params.push(`%${author}%`);
    }
    if (filePath) {
        sql += ' AND path LIKE ?';
        params.push(`%${filePath}%`);
    }
    if (version) {
        sql += ' AND version LIKE ?';
        params.push(`%${version}%`);
    }
    if (operation) {
        sql += ' AND operation = ?';
        params.push(operation);
    }
    if (status) {
        sql += ' AND status = ?';
        params.push(status);
    }
    
    sql += ' ORDER BY commit_time DESC';
    
    db.all(sql, params, (err, commits) => {
        if (err) {
            console.error(err);
            return res.status(500).json({ error: '数据库错误' });
        }
        res.json(commits);
    });
});

// 提交记录列表页面
router.get('/repository/:repositoryId/view', (req, res) => {
    const repositoryId = req.params.repositoryId;
    
    // 获取仓库信息
    db.get('SELECT r.*, p.name as project_name FROM repositories r JOIN projects p ON r.project_id = p.id WHERE r.id = ?', 
        [repositoryId], (err, repository) => {
        if (err) {
            console.error(err);
            return res.status(500).send('数据库错误');
        }
        if (!repository) {
            return res.status(404).send('仓库不存在');
        }
        
        // 获取项目下的所有仓库（用于标签页）
        db.all('SELECT * FROM repositories WHERE project_id = ?', [repository.project_id], (err, repositories) => {
            if (err) {
                console.error(err);
                return res.status(500).send('数据库错误');
            }
            
            // 获取提交记录
            db.all('SELECT * FROM commits WHERE repository_id = ? ORDER BY commit_time DESC LIMIT 50', 
                [repositoryId], (err, commits) => {
                if (err) {
                    console.error(err);
                    return res.status(500).send('数据库错误');
                }
                res.render('commit-list', { repository, repositories, commits });
            });
        });
    });
});

// diff确认页面
router.get('/:commitId/diff', (req, res) => {
    const commitId = req.params.commitId;
    
    db.get(`SELECT c.*, r.name as repository_name, r.type, r.url, r.branch, r.root_directory, 
                   r.username, r.password, r.token, p.name as project_name
            FROM commits c 
            JOIN repositories r ON c.repository_id = r.id 
            JOIN projects p ON r.project_id = p.id 
            WHERE c.id = ?`, [commitId], (err, commit) => {
        if (err) {
            console.error(err);
            return res.status(500).send('数据库错误');
        }
        if (!commit) {
            return res.status(404).send('提交记录不存在');
        }
        
        // 获取diff内容
        getDiffContent(commit, (err, diffData) => {
            if (err) {
                console.error(err);
                return res.status(500).send('获取diff内容失败');
            }
            res.render('commit-diff', { commit, diffData });
        });
    });
});

// 获取diff内容的辅助函数
function getDiffContent(commit, callback) {
    if (commit.type === 'git') {
        getGitDiff(commit, callback);
    } else if (commit.type === 'svn') {
        getSvnDiff(commit, callback);
    } else {
        callback(new Error('不支持的仓库类型'));
    }
}

// 获取Git diff
function getGitDiff(commit, callback) {
    const git = simpleGit();
    
    // 这里需要根据实际情况实现Git diff获取逻辑
    // 由于需要访问远程仓库，这里提供一个模拟实现
    const mockDiffData = {
        type: 'code',
        oldContent: '// 旧代码内容\nfunction oldFunction() {\n    return "old";\n}',
        newContent: '// 新代码内容\nfunction newFunction() {\n    return "new";\n}',
        changes: [
            { type: 'removed', line: 1, content: 'function oldFunction() {' },
            { type: 'added', line: 1, content: 'function newFunction() {' },
            { type: 'removed', line: 2, content: '    return "old";' },
            { type: 'added', line: 2, content: '    return "new";' }
        ]
    };
    
    callback(null, mockDiffData);
}

// 获取SVN diff
function getSvnDiff(commit, callback) {
    // 这里需要根据实际情况实现SVN diff获取逻辑
    // 由于需要访问远程仓库，这里提供一个模拟实现
    const mockDiffData = {
        type: 'table',
        sheetName: 'Sheet1',
        changes: [
            {
                type: 'added',
                row: 4,
                data: {
                    A: 'DefaultBonusAddInventoryId',
                    B: '1200001',
                    C: '奖励专用道具，在奖励结算时会添加到背包',
                    D: '同名道具',
                    E: '同名道具'
                }
            }
        ]
    };
    
    callback(null, mockDiffData);
}

// 确认提交
router.post('/:commitId/confirm', (req, res) => {
    const commitId = req.params.commitId;
    
    db.run('UPDATE commits SET status = "confirmed" WHERE id = ?', [commitId], function(err) {
        if (err) {
            console.error(err);
            return res.status(500).json({ error: '确认失败' });
        }
        if (this.changes === 0) {
            return res.status(404).json({ error: '提交记录不存在' });
        }
        res.json({ message: '确认成功' });
    });
});

// 拒绝提交
router.post('/:commitId/reject', (req, res) => {
    const commitId = req.params.commitId;
    
    db.run('UPDATE commits SET status = "rejected" WHERE id = ?', [commitId], function(err) {
        if (err) {
            console.error(err);
            return res.status(500).json({ error: '拒绝失败' });
        }
        if (this.changes === 0) {
            return res.status(404).json({ error: '提交记录不存在' });
        }
        res.json({ message: '拒绝成功' });
    });
});

// 同步仓库提交记录
router.post('/repository/:repositoryId/sync', (req, res) => {
    const repositoryId = req.params.repositoryId;
    
    db.get('SELECT * FROM repositories WHERE id = ?', [repositoryId], (err, repository) => {
        if (err) {
            console.error(err);
            return res.status(500).json({ error: '数据库错误' });
        }
        if (!repository) {
            return res.status(404).json({ error: '仓库不存在' });
        }
        
        // 同步提交记录
        syncCommits(repository, (err, result) => {
            if (err) {
                console.error(err);
                return res.status(500).json({ error: '同步失败' });
            }
            res.json({ message: '同步成功', count: result.count });
        });
    });
});

// 同步提交记录的辅助函数
function syncCommits(repository, callback) {
    // 这里需要根据仓库类型调用相应的API获取提交记录
    // 由于需要访问远程仓库，这里提供一个模拟实现
    
    const mockCommits = [
        {
            commit_id: 'abc123',
            path: 'MercuryDemo/全局数据.xlsx',
            version: '52489',
            operation: 'M',
            author: 'zhangsan',
            commit_time: new Date().toISOString(),
            message: '[全局数据] Sheet1新增了一行内容'
        },
        {
            commit_id: 'def456',
            path: 'MercuryDemo/技能数据.xlsx',
            version: '52488',
            operation: 'M',
            author: 'lisi',
            commit_time: new Date(Date.now() - 86400000).toISOString(),
            message: '[技能数据] 修改技能配置'
        }
    ];
    
    let insertedCount = 0;
    const insertPromises = mockCommits.map(commit => {
        return new Promise((resolve, reject) => {
            db.run(`INSERT OR IGNORE INTO commits 
                    (repository_id, commit_id, path, version, operation, author, commit_time, message) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
                [repository.id, commit.commit_id, commit.path, commit.version, 
                 commit.operation, commit.author, commit.commit_time, commit.message],
                function(err) {
                    if (err) {
                        reject(err);
                    } else {
                        if (this.changes > 0) insertedCount++;
                        resolve();
                    }
                });
        });
    });
    
    Promise.all(insertPromises)
        .then(() => callback(null, { count: insertedCount }))
        .catch(callback);
}

module.exports = router;
