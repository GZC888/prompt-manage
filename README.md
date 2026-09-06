# Prompt 管理器 (Prompt Manage)

一个轻量的**个人提示词知识库**：Flask + SQLite + Jinja，没有前端框架，单容器即可运行。

写好的提示词值得像代码一样管理：能搜到、能复用、能看见每次改动。这个项目就做这件事，不多做别的。

- **提示词库**：名称、来源、标签、备注、强调色；网格 / 列表两种视图
- **版本历史**：每次保存都是一个版本，可对比、可回滚，超出上限自动清理旧版本
- **即时搜索**：边输入边出结果，覆盖名称、标签、来源、备注和正文
- **Markdown 阅读**：长提示词按 Markdown 渲染，也可一键看原文
- **命令面板**：`⌘K` / `Ctrl+K` 全局搜索与跳转；`/` 聚焦列表搜索框
- **访问控制**：关闭（开放）或全站访问密码两选一
- **备份**：一键导出 / 导入 JSON，导入前自动生成快照
- **中英双语**、亮色 / 暗色 / 跟随系统主题

> 本仓库是 [zhuchenyu2008/prompt-manage](https://github.com/zhuchenyu2008/prompt-manage) 的精简重构分支：
> 移除了逐条提示词密码、封面图片、收藏（已并入置顶）、复制计数和 CSV 导入导出，
> 后端拆分为 `promptmanage/` 包。升级会自动迁移数据，见下方「从旧版本升级」。

---

## 🚀 Docker Compose 部署（推荐）

```bash
mkdir -p prompt-manage && cd prompt-manage
curl -fsSL https://raw.githubusercontent.com/GZC888/prompt-manage/main/docker-compose.yml -o docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/GZC888/prompt-manage/main/.env.example -o .env

# 生成两个互不相同的随机密钥
echo "SECRET_KEY=$(openssl rand -hex 32)" >> .env
echo "BOOTSTRAP_TOKEN=$(openssl rand -hex 32)" >> .env
# 再把 .env 里的 PROMPT_MANAGE_IMAGE 换成具体版本，例如 :sha-abc1234

docker compose up -d
```

打开 `http://127.0.0.1:3501/setup`，填入 `BOOTSTRAP_TOKEN` 和你的访问密码即可完成初始化。
初始化完成后可以把 `.env` 里的 `BOOTSTRAP_TOKEN` 清空并重启——它只用于认领一个全新的数据库。

默认只监听 `127.0.0.1`。要对外提供服务，请在前面放一个负责 HTTPS 的反向代理，
不要直接把容器端口暴露到公网。

### 从源码构建

```bash
git clone https://github.com/GZC888/prompt-manage.git && cd prompt-manage
docker build -t prompt-manage:local .
PROMPT_MANAGE_IMAGE=prompt-manage:local docker compose up -d
```

### Dokploy

新建 **Docker Compose** 应用，仓库填本项目，Compose 路径填 `docker-compose.yml`，
在面板里配置 `SECRET_KEY` / `BOOTSTRAP_TOKEN` 等环境变量，并把 `/app/data` 挂到持久卷。
域名交给 Dokploy 的 Traefik 处理 HTTPS 时，把 `TRUST_PROXY_HEADERS` 设为 `true`。

> ⚠️ 持久卷请单独建一个（例如 `prompt-manage-data`），不要和其他应用共用同一个名为
> `data` 的卷——共用会让不同应用的文件混在一起，备份和迁移都会变得危险。

---

## ⚙️ 环境变量

完整清单见 [`.env.example`](.env.example)，最常用的几个：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SECRET_KEY` | 无 | **生产必填**，至少 32 位随机串。缺失或过弱会拒绝启动 |
| `BOOTSTRAP_TOKEN` | 空 | 认领全新数据库时必填；未设置时 `/setup` 返回 503 |
| `DB_PATH` | `/app/data/data.sqlite3` | 数据库路径，必须在持久卷内 |
| `SESSION_COOKIE_SECURE` | 生产为 `true` | 走 HTTPS 时保持 `true`；纯 HTTP 访问必须改成 `false`，否则无法登录 |
| `TRUST_PROXY_HEADERS` | `false` | 仅当请求一定经过可信反向代理时才开启 |
| `MAX_IMPORT_SIZE_MB` | `10` | 允许导入的 JSON 备份大小上限 |
| `AUTH_LOGIN_MAX_ATTEMPTS` | `10` | 同一 IP 在窗口期内的失败次数上限 |

---

## 🔐 访问控制

两种模式，在「设置 → 访问安全」里切换：

- **关闭**：不需要密码。适合只在本机使用，或前面已经有 Cloudflare Access、
  Authelia 之类的身份网关。
- **访问密码**：进入知识库前需要输入密码，所有页面和接口都会先要求登录。

密码用 Werkzeug（scrypt）哈希存储，旧版的 SHA-256 哈希会在下次登录成功时自动升级。
修改密码或切换模式都需要先验证当前密码，改完之后所有设备都会退出登录。
失败登录按「IP + 路由」限流，另有一层全站失败阈值用于抵挡分布式撞库。

---

## 🗄️ 数据、备份与恢复

所有数据都在 `DB_PATH` 指向的一个 SQLite 文件里，备份就是备份 `/app/data` 目录。

**导出**：设置页「导出 JSON」，得到包含全部提示词和完整版本历史的备份文件。
登录后还可以导出「包含认证信息的完整备份」（内含密码哈希，请妥善保管）。

**导入**：设置页选择 JSON 文件并确认。导入会**覆盖全部现有数据**，
但在写入之前会先把当前库完整快照到 `dirname(DB_PATH)/backups/pre-import-*.json`，
最多保留 `IMPORT_BACKUP_RETENTION` 份。

导入的备份会被完整校验后才落库：ID 必须是安全范围内的正整数、时间戳不能是未来、
版本的父子关系不能跨提示词也不能成环。任何一项不通过都整体拒绝，不会写入一半。

命令行备份 / 恢复：

```bash
# 备份
docker compose exec prompt-manage sh -c 'sqlite3 /app/data/data.sqlite3 ".backup /app/data/backup.sqlite3"' \
  && docker compose cp prompt-manage:/app/data/backup.sqlite3 ./backup.sqlite3

# 恢复
docker compose stop prompt-manage
docker compose cp ./backup.sqlite3 prompt-manage:/app/data/data.sqlite3
docker compose start prompt-manage
```

---

## ⬆️ 升级与从旧版本迁移

```bash
# 1) 先备份 /app/data（见上）
# 2) 修改 .env 里的 PROMPT_MANAGE_IMAGE，然后
docker compose pull && docker compose up -d
docker compose logs -f prompt-manage   # 看到 Applied migration 就是在迁移
```

从旧版本（含逐条密码 / 封面图 / 收藏）升级时，第 12 号迁移会自动做这些事，
**不会让任何内容比升级前更容易被看到**：

- `auth_mode=per` → `global`：原本受保护的提示词继续需要密码，其余内容也一并被保护
- `favorite=1` 的提示词全部转成「置顶」
- 若库里存在封面图，先导出到 `dirname(DB_PATH)/removed-covers-*.json` 再删除字段
- 删除 `prompt_unlocks` 表与 `require_password` / `copy_count` / `last_used_at` 字段

导入旧格式的 JSON 备份同样兼容：`favorite` 会并入 `pinned`，`auth_mode=per` 会转成 `global`。

---

## 🧑‍💻 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

export APP_ENV=development
export SECRET_KEY=dev-only-key-0123456789abcdef0123456789
export DB_PATH="$PWD/data/dev.sqlite3"
export SESSION_COOKIE_SECURE=false
python app.py                     # http://127.0.0.1:3501
```

```bash
python -m pytest -q                                        # 测试
ruff check app.py wsgi.py gunicorn.conf.py promptmanage tests   # 静态检查
```

---

## 🧩 代码结构

```
app.py                 开发入口 + 对外稳定的导入面（测试/脚本用）
wsgi.py                生产入口（gunicorn wsgi:app）
promptmanage/
  __init__.py          应用工厂、请求生命周期、安全响应头、错误页
  config.py            环境变量解析与校验（启动即失败，不带病运行）
  db.py                连接管理、settings 读写
  migrations.py        schema 迁移历史
  security.py          密码、会话、CSRF、限流、访问控制
  transfer.py          JSON 备份的导出、校验与导入
  utils.py             时间戳、标签、颜色、版本号、安全跳转
  i18n.py              中英文案表
  views/               路由：library / settings / auth / api / misc
templates/  static/    Jinja 模板与前端资源（无构建步骤）
```

技术要点：

- 单文件 SQLite + WAL；写操作统一走 `BEGIN IMMEDIATE`，遇到写锁返回 503 + `Retry-After`
- 迁移在整库写锁内一次性执行，多 worker 并发启动也不会互相踩踏
- 会话记录在服务端 `auth_sessions` 表：改密码即失效所有设备，复制 Cookie 也无法续命
- CSRF 双提交校验；默认输出 CSP、`X-Frame-Options`、`Referrer-Policy` 等安全响应头
- 前端零依赖、零构建：约 1.1k 行原生 JS，Markdown 渲染器自带且先转义再解析

---

## ❓ 常见问题

**登录一直失败，也没有报错**
多半是用 HTTP 访问但 `SESSION_COOKIE_SECURE=true`，浏览器直接丢弃了会话 Cookie。
把它设为 `false` 并重启，或者改用 HTTPS。

**忘记密码了**
停止容器，直接改库：
`sqlite3 /app/data/data.sqlite3 "UPDATE settings SET value='off' WHERE key='auth_mode';"`
然后重启，进设置页重新设置密码。

**`/setup` 返回 503**
`BOOTSTRAP_TOKEN` 没配置。设置一个随机值后重启即可；初始化完成后可以清空。

**容器起不来，日志里写 `SECRET_KEY is missing or too weak`**
生产环境必须提供至少 32 位的随机 `SECRET_KEY`，用 `openssl rand -hex 32` 生成。

---

## 📄 许可证

GPL-3.0-only，见 [LICENSE](LICENSE)。
