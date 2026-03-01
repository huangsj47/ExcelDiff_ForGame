const express = require('express');
const bodyParser = require('body-parser');
const path = require('path');
const sqlite3 = require('sqlite3').verbose();
const fs = require('fs');

const app = express();
const PORT = process.env.PORT || 3000;

// 中间件配置
app.set('view engine', 'ejs');
app.set('views', path.join(__dirname, 'views'));
app.use(express.static(path.join(__dirname, 'public')));
app.use(bodyParser.urlencoded({ extended: true }));
app.use(bodyParser.json());

// 初始化数据库
const dbPath = path.join(__dirname, 'database.db');
const db = new sqlite3.Database(dbPath);

// 创建数据库表
db.serialize(() => {
    // 项目表
    db.run(`CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        department TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )`);

    // 仓库表
    db.run(`CREATE TABLE IF NOT EXISTS repositories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER,
        name TEXT NOT NULL,
        type TEXT NOT NULL, -- 'svn' or 'git'
        category TEXT,
        url TEXT NOT NULL,
        server_url TEXT,
        root_directory TEXT,
        username TEXT,
        password TEXT,
        token TEXT,
        branch TEXT,
        resource_type TEXT, -- 'table', 'res', 'code'
        current_version TEXT,
        path_regex TEXT,
        log_regex TEXT,
        log_filter_regex TEXT,
        commit_filter TEXT,
        important_tables TEXT,
        unconfirmed_history BOOLEAN DEFAULT 0,
        delete_table_alert BOOLEAN DEFAULT 0,
        weekly_version_setting TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (project_id) REFERENCES projects (id)
    )`);

    // 提交记录表
    db.run(`CREATE TABLE IF NOT EXISTS commits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        repository_id INTEGER,
        commit_id TEXT NOT NULL,
        path TEXT,
        version TEXT,
        operation TEXT,
        author TEXT,
        commit_time DATETIME,
        message TEXT,
        status TEXT DEFAULT 'pending', -- 'pending', 'confirmed', 'rejected'
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (repository_id) REFERENCES repositories (id)
    )`);
});

// 路由
const projectRoutes = require('./routes/projects');
const repositoryRoutes = require('./routes/repositories');
const commitRoutes = require('./routes/commits');

app.use('/projects', projectRoutes);
app.use('/repositories', repositoryRoutes);
app.use('/commits', commitRoutes);

// 主页路由
app.get('/', (req, res) => {
    db.all('SELECT * FROM projects ORDER BY created_at DESC', (err, projects) => {
        if (err) {
            console.error(err);
            return res.status(500).send('数据库错误');
        }
        res.render('index', { projects });
    });
});

// 启动服务器
app.listen(PORT, () => {
    console.log(`服务器运行在 http://localhost:${PORT}`);
});

module.exports = app;
