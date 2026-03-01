const express = require('express');
const router = express.Router();
const sqlite3 = require('sqlite3').verbose();
const path = require('path');

const dbPath = path.join(__dirname, '../database.db');
const db = new sqlite3.Database(dbPath);

// 获取所有项目
router.get('/', (req, res) => {
    db.all('SELECT * FROM projects ORDER BY created_at DESC', (err, projects) => {
        if (err) {
            console.error(err);
            return res.status(500).json({ error: '数据库错误' });
        }
        res.json(projects);
    });
});

// 创建新项目
router.post('/', (req, res) => {
    const { code, name, department } = req.body;
    
    if (!code || !name) {
        return res.status(400).json({ error: '项目代号和名称不能为空' });
    }

    db.run('INSERT INTO projects (code, name, department) VALUES (?, ?, ?)', 
        [code, name, department], function(err) {
        if (err) {
            if (err.code === 'SQLITE_CONSTRAINT') {
                return res.status(400).json({ error: '项目代号已存在' });
            }
            console.error(err);
            return res.status(500).json({ error: '创建项目失败' });
        }
        res.json({ id: this.lastID, code, name, department });
    });
});

// 获取单个项目详情
router.get('/:id', (req, res) => {
    const projectId = req.params.id;
    
    db.get('SELECT * FROM projects WHERE id = ?', [projectId], (err, project) => {
        if (err) {
            console.error(err);
            return res.status(500).json({ error: '数据库错误' });
        }
        if (!project) {
            return res.status(404).json({ error: '项目不存在' });
        }
        
        // 获取项目下的仓库
        db.all('SELECT * FROM repositories WHERE project_id = ?', [projectId], (err, repositories) => {
            if (err) {
                console.error(err);
                return res.status(500).json({ error: '数据库错误' });
            }
            res.json({ ...project, repositories });
        });
    });
});

// 项目详情页面
router.get('/:id/view', (req, res) => {
    const projectId = req.params.id;
    
    db.get('SELECT * FROM projects WHERE id = ?', [projectId], (err, project) => {
        if (err) {
            console.error(err);
            return res.status(500).send('数据库错误');
        }
        if (!project) {
            return res.status(404).send('项目不存在');
        }
        
        // 获取项目下的仓库
        db.all('SELECT * FROM repositories WHERE project_id = ?', [projectId], (err, repositories) => {
            if (err) {
                console.error(err);
                return res.status(500).send('数据库错误');
            }
            res.render('project-detail', { project, repositories });
        });
    });
});

// 删除项目
router.delete('/:id', (req, res) => {
    const projectId = req.params.id;
    
    db.run('DELETE FROM projects WHERE id = ?', [projectId], function(err) {
        if (err) {
            console.error(err);
            return res.status(500).json({ error: '删除项目失败' });
        }
        if (this.changes === 0) {
            return res.status(404).json({ error: '项目不存在' });
        }
        res.json({ message: '项目删除成功' });
    });
});

module.exports = router;
