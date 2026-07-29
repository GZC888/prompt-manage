# Prompt 管理器 (Prompt Manage)

语言: 简体中文 | [English](README.en.md)

<p align="center">
  <img src="logo.png" alt="Prompt Manager Logo" width="140" />
</p>

一个**轻量、自托管**的个人 Prompt（提示词）管理系统：版本控制、搜索、标签、来源、收藏、归档、图片、导入导出、中英文界面、深浅色主题。技术栈保持轻量 —— **Python + Flask + SQLite + Jinja + 原生 JS/CSS**，无 React/Vue/Next.js，无外部 CDN 依赖，开箱即用，适合长期部署在个人 VPS。

> 镜像地址：`ghcr.io/gzc888/prompt-manage`

---

## ✨ 功能概览

- **提示词管理**：名称、来源、标签、备注、颜色、封面图片（Base64，单张 ≤5MB，jpg/jpeg/png/webp）。
- **语义化版本控制**：补丁 / 次 / 主版本递增，历史版本、词级 & 行级 Diff、一键回滚（创建新版本，不覆盖历史）、版本数量自动清理。
- **搜索与筛选**：名称/来源/备注/标签/当前内容全文搜索，标签 & 来源多维筛选，排序。
- **收藏 / 置顶 / 归档**：侧边栏快速过滤 All / Favorites / Pinned / Locked / Archived；记录最近使用时间与复制次数。
- **命令面板**：`Ctrl/Cmd + K` 快速搜索提示词、新建、打开设置/收藏、切换主题。
- **安全访问控制**：三种模式 `off` / `global` / `per`（详见下文），CSRF 防护，登录限流，安全响应头 + CSP。
- **数据管理**：JSON / CSV 导入导出，导入前自动备份，导入失败自动回滚不破坏现有数据。
- **体验**：深/浅色主题（含跟随系统），中英文切换，响应式移动端适配，**无首页布局闪烁（FOUC）**。

---

## 🚀 Docker Compose 部署（推荐）

1. 准备一个目录，放入 [`docker-compose.yml`](docker-compose.yml) 和 [`.env.example`](.env.example)。
2. 选择要运行的镜像。生产请使用发布 tag、`sha-*` tag 或 digest；`latest` 只适合临时验证：

   ```bash
   export PROMPT_MANAGE_IMAGE=ghcr.io/gzc888/prompt-manage:sha-REPLACE_ME
   # Replace sha-REPLACE_ME with the exact release/sha tag or image digest.
   ```

3. 为每个安装生成两个不同的随机值。`BOOTSTRAP_TOKEN` 只用于第一次认领全新生产数据库：

   ```bash
   umask 077
   printf 'PROMPT_MANAGE_IMAGE=%s\n' "$PROMPT_MANAGE_IMAGE" > .env
   printf 'HOST_BIND=127.0.0.1\n' >> .env
   printf 'SECRET_KEY=%s\n' "$(openssl rand -hex 32)" >> .env
   printf 'BOOTSTRAP_TOKEN=%s\n' "$(openssl rand -hex 32)" >> .env
   # 直接通过 HTTP 访问时保持 false；HTTPS 反代请改为 true
   printf 'SESSION_COOKIE_SECURE=false\n' >> .env
   ```

4. 启动：

   ```bash
   docker compose up -d
   ```

5. 全新生产库第一次启动时，除 `/healthz`、静态资源和 `/setup` 外的路由会保持关闭。若按上例绑定本机，打开 `http://127.0.0.1:3501/setup`；通过反代访问时打开反代域名的 `/setup`。输入 `BOOTSTRAP_TOKEN`，设置至少 8 位的访问密码并选择认证模式（公网请选择 `global`）。初始化成功后令牌不会写入数据库；从 `.env` 删除 `BOOTSTRAP_TOKEN` 并重启容器。
6. 健康检查：`http://127.0.0.1:3501/healthz` 应返回包含 `status=ok`、`build_sha` 和 `initialized` 的 JSON。`/setup` 只能完成一次，旧数据库升级不会重新触发初始化。

Compose 默认只将端口绑定到 `127.0.0.1`，并使用 `pull_policy: missing`；更换 tag/digest 后请显式运行 `docker compose pull`。数据持久化在 Compose 的逻辑卷 `prompt-data`（容器内 `/app/data`）；Docker 通常会给实际卷名加项目名前缀，可用 `docker inspect prompt-manage --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Name}}{{end}}{{end}}'` 查询。`BOOTSTRAP_TOKEN` 在 Compose 中允许为空，以便初始化完成后删除；对全新生产库，应用会在令牌为空时保持 `/setup` 返回 503，而不会开放业务路由。Compose 固定 `APP_ENV=production`；开发/测试请使用源码或单独的 override。生产环境不要把 `.env` 或初始化令牌提交到仓库。若确需直接从其他机器访问，请把 `HOST_BIND=0.0.0.0` 作为有防火墙保护的临时选择，并保持 `TRUST_PROXY_HEADERS=false`。

### 从源码本地构建镜像（可选）

```bash
export BOOTSTRAP_TOKEN="$(openssl rand -hex 32)"
docker build -t prompt-manage:local .
docker run -d --name prompt-manage -p 127.0.0.1:3501:3501 \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -e BOOTSTRAP_TOKEN="$BOOTSTRAP_TOKEN" \
  -e SESSION_COOKIE_SECURE=false \
  -v prompt-data:/app/data \
  prompt-manage:local
# 打开 http://127.0.0.1:3501/setup，输入上面的 BOOTSTRAP_TOKEN
```

---

## 🛳️ Dokploy 部署

1. 在 Dokploy 新建一个 **Compose** 应用，粘贴本仓库的 `docker-compose.yml`（或指向仓库）。
2. 将 `PROMPT_MANAGE_IMAGE` 设为不可变 tag/digest，并设置不同的 **`SECRET_KEY`** 和 **`BOOTSTRAP_TOKEN`**（均可用 `openssl rand -hex 32` 生成）。HTTPS 反代下同时设置 `SESSION_COOKIE_SECURE=true` 和 `TRUST_PROXY_HEADERS=true`。
3. 部署。Compose 使用 `pull_policy: missing`，不会在每次重启时悄悄改变正在运行的镜像。升级时显式更换发布 tag、`sha-*` tag 或 digest，并执行 `docker compose pull` 后再重建。
4. 将 Dokploy 的域名/反代指向容器的 `3501` 端口，完成 `/setup` 后删除 `BOOTSTRAP_TOKEN` 并重新部署。

> 提示：Dokploy 的卷映射请指向 `/app/data`，确保数据库与备份持久化。

### HTTPS、代理头与 HSTS

- `SESSION_COOKIE_SECURE=true` 和 HSTS **不会提供 TLS**；公网部署必须先在 Dokploy、Nginx、Caddy 或 Cloudflare Origin 前配置有效 HTTPS，并把 HTTP 重定向到 HTTPS。
- `TRUST_PROXY_HEADERS=true` 时，应用只信任最靠近它的 **1 层**代理提供的 `X-Forwarded-For` 和 `X-Forwarded-Proto`。该代理必须覆盖而不是追加不可信的入站值，且容器的 `3501` 端口不得绕过代理直接暴露。多层代理应在最靠近应用的一层先规范化这些头。
- 生产默认启用 `ENABLE_HSTS=true`，但仅当应用将请求识别为 HTTPS 时才发送 `Strict-Transport-Security: max-age=<HSTS_MAX_AGE>`。`HSTS_INCLUDE_SUBDOMAINS` 默认 `false`；只有确认当前响应主机名下的所有子域都支持 HTTPS 后，才显式设为 `true` 追加 `includeSubDomains`。故障恢复时可临时设 `HSTS_MAX_AGE=0` 清除策略。
- 使用 Cloudflare 时建议 SSL/TLS 模式设为 **Full (strict)**，并通过规则对本应用域名关闭 **Rocket Loader**。Rocket Loader 会改变脚本执行时序，可能干扰首屏布局脚本和交互脚本；同时不要缓存登录、设置和其他动态 HTML 响应。

---

## 📦 GHCR 镜像与自动发布

GitHub Actions（[`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml)）在以下情况自动构建并推送到 GHCR：

- push 到 `main` → `:latest` 与 `:sha-xxxxxxx`
- 打 tag `v*`（如 `v1.2.3`）→ `:1.2.3`、`:1.2`
- 手动触发（`workflow_dispatch`）

权限最小化（`contents: read`，`packages: write`），使用官方 `setup-buildx` / `login` / `metadata` / `build-push` actions。

验证镜像：

```bash
set -eu
docker pull "$PROMPT_MANAGE_IMAGE"
docker rm -f prompt-manage-verify >/dev/null 2>&1 || true
docker run -d --name prompt-manage-verify -p 127.0.0.1:3501:3501 \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -e BOOTSTRAP_TOKEN="$(openssl rand -hex 32)" \
  -e SESSION_COOKIE_SECURE=false \
  "$PROMPT_MANAGE_IMAGE"
attempt=1
until curl -fsS http://127.0.0.1:3501/healthz 2>/dev/null; do
  if [ "$attempt" -ge 30 ]; then
    docker logs prompt-manage-verify || true
    docker rm -f prompt-manage-verify >/dev/null 2>&1 || true
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 1
done
docker rm -f prompt-manage-verify >/dev/null
```

这一步只验证容器存活；全新生产库仍须按上面的 `/setup` 流程完成安全初始化。

> 首次推送后，GHCR 包默认私有。若要 `docker pull` 公开镜像，请在 GitHub 的 Package 设置中将其设为 Public，或先 `docker login ghcr.io`。

---

## ⚙️ 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PROMPT_MANAGE_IMAGE` | （无） | **Compose 必填**。生产使用发布/`sha-*` tag 或 digest。 |
| `HOST_BIND` | `127.0.0.1` | Compose 发布端口的监听地址；仅在防火墙或私网保护下使用 `0.0.0.0`。 |
| `HOST_PORT` | `3501` | Compose 发布到主机的端口。 |
| `APP_ENV` | `production` | `production` / `development` / `testing`。Compose 固定为 `production`；其他值仅用于源码运行或自定义 override。 |
| `APP_PORT` | `3501` | 监听端口。 |
| `DB_PATH` | `/app/data/data.sqlite3` | SQLite 数据库路径；Docker 部署必须是 `/app/data` 下的绝对路径，并把该目录整体持久化。 |
| `SECRET_KEY` | （无） | **生产必填**。缺失/弱值会拒绝启动。 |
| `BOOTSTRAP_TOKEN` | （无） | 全新生产库 `/setup` 的一次性认领令牌，**首次初始化必填**；为空时 `/setup` 返回 503。不写入数据库，初始化后删除并重启。 |
| `SESSION_COOKIE_SECURE` | 生产 `true` | HTTPS 下应为 `true`。 |
| `SESSION_COOKIE_SAMESITE` | `Lax` | `Lax` / `Strict` / `None`。 |
| `TRUST_PROXY_HEADERS` | `false` | 仅在应用始终位于会覆盖转发头的单层可信反向代理后方时设为 `true`；启用后禁止直连应用端口。 |
| `PERMANENT_SESSION_DAYS` | `3650` | 登录保持天数（默认约 10 年，适合个人长期使用）。 |
| `AUTH_LOGIN_MAX_ATTEMPTS` | `10` | 限流窗口内允许的失败次数。 |
| `AUTH_LOGIN_WINDOW_SECONDS` | `900` | 限流统计窗口（秒）。 |
| `AUTH_LOCK_SECONDS` | `900` | 触发限流后的锁定时长（秒）。 |
| `GLOBAL_LOGIN_MAX_ATTEMPTS` | `1000` | 全站累计失败次数上限，用于防分布式暴力破解。 |
| `GLOBAL_LOGIN_WINDOW_SECONDS` | `3600` | 全站累计失败次数统计窗口（秒）。 |
| `MAX_IMPORT_SIZE_MB` | `10` | 导入文件大小上限。 |
| `MAX_IMAGE_SIZE_MB` | `5` | 单张图片上限。 |
| `IMPORT_BACKUP_RETENTION` | `20` | 导入前备份最多保留的份数。 |
| `ENABLE_SECURITY_HEADERS` | `true` | 是否输出安全响应头与 CSP。 |
| `ENABLE_HSTS` | 生产 `true` | 仅在请求被识别为 HTTPS 时输出 HSTS；本身不会启用 HTTPS。 |
| `HSTS_MAX_AGE` | `31536000` | HSTS 有效期（秒）。 |
| `HSTS_INCLUDE_SUBDOMAINS` | `false` | 是否追加 `includeSubDomains`；仅在所有相关子域均支持 HTTPS 时启用。 |
| `GUNICORN_WORKERS` | `2` | Gunicorn 工作进程数；SQLite 写入较多时不要盲目调高。 |
| `GUNICORN_THREADS` | `4` | 每个工作进程的线程数。 |
| `GUNICORN_TIMEOUT` | `60` | 请求超时（秒）。 |
| `GUNICORN_GRACEFUL_TIMEOUT` | `30` | 优雅停止等待时间（秒）。 |
| `GUNICORN_LOG_LEVEL` | `info` | Gunicorn 日志级别。 |
| `BUILD_SHA` | `dev`（构建时） | `/healthz` 返回的构建标识；发布镜像由 CI 在构建时注入，运行环境不要覆盖。 |
| `FLASK_DEBUG` | `false` | 仅本地 `python app.py` 时生效；生产请勿开启。 |

完整示例见 [`.env.example`](.env.example)。`SECRET_KEY` 生成：

```bash
openssl rand -hex 32
```

> **请勿**把真实 `SECRET_KEY` 或 `BOOTSTRAP_TOKEN` 写进代码、URL、工单或提交到仓库。`.env`、数据库、备份均已在 `.dockerignore`/不打入镜像。

---

## 🔐 认证模式

### 首次安全初始化

全新生产数据库默认处于未认领状态，应用会在服务器端封闭业务路由，不会先以 `off` 模式公开内容。服务端必须先配置至少 32 个字符的随机 `BOOTSTRAP_TOKEN`，否则 `/setup` 返回 503 且初始化保持禁用；已知占位符和短令牌会让应用拒绝启动。配置后访问 `/setup`，使用该令牌设置第一个访问密码和认证模式；令牌使用常量时间比较、不会写入 SQLite，并在初始化完成后失效。为了减少秘密暴露面，完成后仍应从部署环境删除该变量并重启。已有数据库升级时会保留原认证状态，不会被强制重新初始化。

`BOOTSTRAP_TOKEN` 为空不是可用的初始化模式。若令牌丢失，生成新令牌、更新环境并重启，而不要临时开放应用端口。

在 **设置 → 访问密码** 中选择：

| 模式 | 行为 | 适用场景 |
| --- | --- | --- |
| `off` | 不需要密码，完全开放。 | 仅本机/内网，无敏感内容。 |
| `global` | 访问任意页面都需要登录（一次登录看全部）。 | **公网部署强烈推荐。** |
| `per` | 站点可浏览，但勾选了"需要密码"的提示词被单独锁定，需逐条解锁；其内容/标签/来源/备注不会出现在列表、搜索、标签 API 或导出中。 | 想展示大部分内容、仅隐藏个别提示词。 |

**为什么公网建议 `global`**：`off` 完全无保护；`per` 仅保护被标记的条目，其余内容公开可读。公网 VPS 上，`global` 用单一密码门禁整站，最简单也最稳妥。

- 设置密码至少 **8 位**，**不限制最大长度**（鼓励使用较长的 passphrase）。旧版 4–8 位短密码仍可登录，建议在设置页更新为更强的密码。
- 修改密码或切换模式需先验证当前密码；首次设置密码后会自动登录为管理员。
- `/settings` 与 `/export` 在已设置密码时需要登录才能访问。
- `per` 模式的提示词解锁密码与管理员密码相同；知道该密码的人拥有全站管理权限，不要把它作为只读分享凭据。

---

## 🗄️ 数据持久化、备份与恢复

- 所有数据保存在 `DB_PATH`（默认 `/app/data/data.sqlite3`），SQLite 开启 WAL，因此运行中还可能有同名的 `-wal` 和 `-shm` 文件。**务必将整个 `/app/data` 挂载到持久化卷。**
- **逻辑导出 ≠ 完整物理快照**：设置页的普通 JSON/CSV 导出提示词、版本和常规可迁移设置，不包含认证凭据；`per` 模式默认还会排除受保护条目。只有已登录管理员显式请求 `include_locked=1&include_auth=1` 的全量恢复导出才包含认证模式与**密码哈希**，必须像秘密一样加密、限制访问。逻辑导出不包含登录限流记录、认证 session、已解锁会话、迁移表等全部 SQLite 状态；恢复后必须重新登录。
- **导入前自动备份**：每次导入会先把全部提示词和可恢复设置（包括认证密码哈希）写入 `dirname(DB_PATH)/backups/pre-import-*.json`；默认 `DB_PATH` 下即 `/app/data/backups/pre-import-*.json`。普通导入会恢复语言/清理阈值，但默认不覆盖当前认证凭据，避免把管理员锁在门外。该文件是防误操作的回滚点，不是异地备份，不能替代物理快照。
- **运行中安全备份（推荐）**：使用 SQLite Online Backup API，让 SQLite 在一致性快照中处理 WAL；不要在服务运行时只复制 `data.sqlite3`：

  ```bash
  set -eu
  umask 077
  backup="prompt-data-$(date +%Y%m%d-%H%M%S).sqlite3"
  backup_path="$(docker exec -e BACKUP_NAME="$backup" prompt-manage \
    python -c 'import os; print(os.path.join(os.path.dirname(os.environ["DB_PATH"]), "backups", os.environ["BACKUP_NAME"]))')"
  docker exec -e BACKUP_FILE="$backup_path" prompt-manage \
    python -c 'import os,sqlite3; os.umask(0o077); src=sqlite3.connect(os.environ["DB_PATH"]); dst=sqlite3.connect(os.environ["BACKUP_FILE"]); src.backup(dst); dst.close(); src.close()'
  if docker cp "prompt-manage:$backup_path" "./$backup" && chmod 600 "./$backup"; then
    docker exec prompt-manage rm -f -- "$backup_path" || \
      echo "快照已复制，但卷内临时文件清理失败：$backup_path" >&2
  else
    echo "复制失败；卷内快照仍保留在 $backup_path。确认重试副本可用后再手动删除。" >&2
    exit 1
  fi
  ```

  只有 `docker cp` 和本地权限设置都成功后，命令才会删除卷内临时快照。若复制或清理失败，先重试并校验本地副本，再按错误信息中的路径执行 `docker exec prompt-manage rm -f -- <路径>`；不要先删除唯一可用副本。

- **离线物理备份**：升级或迁移前先停止应用，再把整个 `/app/data`（包括可能存在的 `-wal`/`-shm`）打包到应用卷之外；完成后再启动：

  ```bash
  set -eu
  umask 077
  install -d -m 700 ./backups
  docker compose stop prompt-manage
  trap 'docker compose start prompt-manage' EXIT
  docker compose run --rm --no-deps -v "$PWD/backups:/backup" --entrypoint sh prompt-manage \
    -c 'umask 077; tar -czf "/backup/prompt-data-$(date +%Y%m%d-%H%M%S).tar.gz" -C /app/data .'
  docker compose start prompt-manage
  trap - EXIT
  ```

物理快照和离线归档包含认证哈希与全部受保护内容，必须像最高敏感级别的秘密一样加密、限制权限（示例使用 `umask 077`），不要放在 Web 可访问目录；恢复前应执行 `PRAGMA integrity_check`，并轮换/清理旧副本。

**恢复演练（每次升级前、至少每月一次）**：不要直接在生产卷上试验。在同一个 shell 中依次运行下面的命令；先用唯一名称创建新空卷，从两种来源中选择一种恢复，再启动测试容器。文件变量必须填写包含扩展名的完整相对文件名。

```bash
set -eu
restore_suffix="$(date +%Y%m%d%H%M%S)-$$"
restore_volume="prompt-restore-test-$restore_suffix"
restore_container="prompt-restore-test-$restore_suffix"
docker volume create "$restore_volume"
```

从在线 `.sqlite3` 快照恢复（快照位于当前目录）：

```bash
snapshot="prompt-data-YYYYMMDD-HHMMSS.sqlite3"
docker run --rm -v "$restore_volume:/data" -v "$PWD:/restore:ro" alpine:3.20 \
  sh -c 'cp "/restore/$1" /data/data.sqlite3' sh "$snapshot"
```

或者从离线卷归档恢复（归档位于 `./backups`，必须解压到空卷，不能覆盖混合旧文件）：

```bash
archive="backups/prompt-data-YYYYMMDD-HHMMSS.tar.gz"
docker run --rm -v "$restore_volume:/data" -v "$PWD:/restore:ro" alpine:3.20 \
  sh -c 'tar -xzf "/restore/$1" -C /data' sh "$archive"
```

用同一镜像启动隔离实例。SQLite 快照方案保持下面的默认路径；离线归档若原部署使用了自定义 `DB_PATH`，把 `restore_db_path` 改成原值：

```bash
restore_db_path=/app/data/data.sqlite3
docker run -d --name "$restore_container" -p 127.0.0.1:3502:3501 \
  -e SECRET_KEY="$(openssl rand -hex 32)" -e SESSION_COOKIE_SECURE=false \
  -e DB_PATH="$restore_db_path" \
  -v "$restore_volume:/app/data" "$PROMPT_MANAGE_IMAGE"
attempt=1
until curl -fsS http://127.0.0.1:3502/healthz 2>/dev/null; do
  if [ "$attempt" -ge 30 ]; then
    docker logs "$restore_container" || true
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 1
done
docker exec "$restore_container" python -c 'import os,sqlite3,sys; c=sqlite3.connect(os.environ["DB_PATH"]); r=c.execute("PRAGMA integrity_check").fetchall(); print(r); c.close(); sys.exit(0 if r == [("ok",)] else 1)'
```

确认健康检查返回 `initialized=true`，且完整性检查输出仅为 `[('ok',)]`，再实际检查登录、关键提示词、版本历史和导出。真正恢复 `.sqlite3` 快照时先停止所有访问，移走旧数据库及其 `-wal`/`-shm` 文件，再把快照放到 `DB_PATH` 后启动；恢复离线归档时，将它解压到新的空卷并让服务挂载该卷，同时沿用原 `DB_PATH`，不要与旧卷文件混合覆盖。两种方式都要观察迁移日志和健康检查，随后重新验证认证模式、HTTPS Cookie 和最近备份。至少保留一份经过演练的副本在独立主机/对象存储。

检查完成后清理临时资源：

```bash
docker rm -f "$restore_container"
docker volume rm "$restore_volume"
```

---

## ⬆️ 升级

```bash
# 1) 先备份 /app/data（见上）
# 2) 拉取最新镜像并重建容器
docker compose pull
docker compose up -d
```

数据库结构通过内置迁移在启动时自动升级（记录在 `schema_migrations` 表，未知版本或迁移名称不匹配会 fail-fast，迁移失败会终止启动而非静默忽略）。为增加约束，迁移可能在事务中创建新表、复制数据、删除旧表并重命名新表；因此不要依赖“永不 drop/rename”的假设，升级前必须完成可恢复的物理备份和恢复演练。Compose 不会自动替换已有镜像；升级时修改 `PROMPT_MANAGE_IMAGE` 为新的发布 tag、`sha-*` tag 或 digest，执行 `docker compose pull`，再重建容器。回滚到旧镜像时必须同时恢复与旧代码兼容的数据库快照，不要直接让旧代码读取新 schema。

### 部署验收清单

1. `docker compose ps` 显示容器为 `healthy`，日志中没有密钥校验、卷权限、bootstrap 或迁移失败。
2. `curl -fsS https://<域名>/healthz` 返回 `status=ok`，并确认 `build_sha` 是本次期望的提交/镜像、`initialized` 状态正确，而不是仅凭 tag 判断升级成功。
3. 全新安装完成 `/setup` 后删除 `BOOTSTRAP_TOKEN` 并重启；再次访问 `/setup` 不应能重做初始化。确认能够登录、创建一个测试提示词并导出后再删除测试数据。
4. `curl -sSI https://<域名>/` 检查 CSP 等安全头；启用 HSTS 时确认存在正确的 `Strict-Transport-Security`。同时确认 HTTP 会跳转到 HTTPS、Secure Cookie 能正常保持登录。
5. `TRUST_PROXY_HEADERS=true` 时，从公网确认只能经可信代理访问，`3501` 端口不可直连；核对限流日志中的客户端地址符合代理拓扑。
6. 完成一次隔离卷恢复演练并记录所用备份、恢复耗时与校验结果。只有健康检查通过不代表内容、认证和备份均可恢复。

---

## 🧑‍💻 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SECRET_KEY=dev-only-not-secret      # 开发可省略（会用带警告的开发密钥）
export DB_PATH=./data/data.sqlite3
export APP_ENV=development
export FLASK_DEBUG=true                     # 可选：开启调试/自动重载

python app.py                              # http://127.0.0.1:3501
```

> 生产请使用 Gunicorn：`gunicorn -c gunicorn.conf.py wsgi:app`（Docker 镜像已默认如此）。

---

## ✅ 运行测试

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

测试使用临时 SQLite 数据库（`tmp_path`），不会污染真实 `/app/data/data.sqlite3`。覆盖：健康检查、生产缺失 `SECRET_KEY` 启动失败、长期 session、登出清理、旧 SHA256 密码登录并迁移、密码无最大长度、登录限流、CSRF、受保护提示词的访问/编辑/删除/置顶/回滚拦截、搜索/标签/导出不泄漏、导入非法 JSON 不破坏数据 + 自动备份等。

---

## ❓ 常见问题

- **打不开/容器不断重启？** 多半是生产模式缺少 `SECRET_KEY`。查看日志 `docker logs prompt-manage`，设置强密钥后重启。
- **全新部署只能打开 `/setup`，或 `/setup` 返回 503？** 这是生产环境的安全初始化状态；503 表示服务端没有非空 `BOOTSTRAP_TOKEN`。设置随机令牌并重启后完成认领，不要通过 URL 或日志传递令牌；完成后删除该环境变量并再次重启。
- **HTTPS 下登录后立即掉线？** 反代为 HTTPS 时请设 `SESSION_COOKIE_SECURE=true`；仅当应用端口只接受可信代理流量时再设 `TRUST_PROXY_HEADERS=true`。纯 HTTP 访问则都设为 `false`。
- **HTTPS 没有 HSTS？** `ENABLE_HSTS=true` 仍要求应用把请求识别为 HTTPS。检查 TLS 终止层是否发送/覆盖 `X-Forwarded-Proto=https`，以及 `TRUST_PROXY_HEADERS` 是否与实际代理边界一致。子域策略需另设 `HSTS_INCLUDE_SUBDOMAINS=true`。
- **Cloudflare 后界面闪烁或按钮失效？** 对应用域名关闭 Rocket Loader，并清除 Cloudflare 缓存；不要缓存动态 HTML。
- **能否运行中复制 `data.sqlite3`？** 不要。WAL 中可能还有未合并事务，请使用上面的 SQLite Online Backup API，或停机后复制整个数据目录。
- **`docker pull` GHCR 403？** 镜像可能为私有，去 GitHub Package 设为 Public，或先 `docker login ghcr.io`。
- **忘记密码？** 先停止公网入口并完成物理备份，再在仅本机可访问的维护窗口清空认证设置；该操作会立即让站点变为公开，必须在重新设置 `global` 密码后才能恢复公网入口：
  ```bash
  docker exec -it prompt-manage python -c "import app; c=app.get_db(); c.execute('BEGIN IMMEDIATE'); app.set_setting(c,'auth_mode','off'); app.set_setting(c,'auth_password_hash',''); app.set_setting(c,'auth_revision',str(int(app.get_setting(c,'auth_revision','1'))+1)); c.execute('DELETE FROM auth_sessions'); c.execute('DELETE FROM prompt_unlocks'); c.commit(); c.close()"
  ```
- **想收紧 CSP？** 默认 CSP 兼容少量内联脚本/样式（FOUC 早期脚本、颜色外圈）。后续可迁移为 nonce 收紧 `script-src`。

---

## 🧩 技术说明

- 单文件应用 `app.py` + `i18n.py`（翻译）+ `wsgi.py`/`gunicorn.conf.py`（生产入口）。
- 前端：`templates/`（Jinja，含 `_icons.html` 内联 SVG 图标宏、`_prompt_card.html`、`_sidebar.html` 等 partial）+ `static/css/style.css` + `static/js/{main,index,detail,settings,versions}.js`。
- 首屏布局状态（主题 / 网格或列表 / 侧边栏）由 `<head>` 中**先于样式表执行**的极小脚本写入 `<html>` data 属性，CSS 据此决定初始布局，彻底消除登录后首页的 grid/list、侧边栏闪烁。

许可证：见 [LICENSE](LICENSE)。
