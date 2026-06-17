# 部署文档 — 香港轻量服务器(免备案)+ 硅基流动 bge-m3

面向中国用户访问。本文档只描述操作步骤,不改动仓库代码(其中"改 embedding"一步给出
要替换的代码,由你按需粘贴)。

---

## 0. 选型结论

| 项 | 方案 | 理由 |
|---|---|---|
| 服务器 | 阿里云/腾讯云 **香港轻量应用服务器** | 免 ICP 备案,可直接绑域名走 80/443,境内访问延迟 30–80ms |
| 答案/规划 LLM | **DeepSeek API**(你已有 key) | 境内服务,不被墙,服务器零算力 |
| Embedding | **硅基流动 `BAAI/bge-m3`** | 境内 API,同模型同 1024 维,与现有库向量完全兼容;服务器无需 Ollama/GPU |
| 数据库 | Postgres + pgvector(Docker) | 本地 `pg_dump` 搬过去,带现成 embedding,服务器不重算 |
| 前端 | Vite 构建 `dist/` → nginx 静态托管 | 纯静态,nginx 反代 `/api` 到后端 |

**整套无任何被墙依赖**,这是该项目适合中国部署的根本原因。

架构:

```
中国用户 ──HTTPS──> nginx(:443)
                     ├── /          静态文件 frontend/dist
                     └── /api/*  ──> uvicorn FastAPI(:8077)
                                       ├── DeepSeek API   (答案/规划)
                                       ├── 硅基流动 API    (query embedding)
                                       └── Postgres+pgvector(:5433, Docker)
```

---

## 1. 准备清单

- [ ] 香港轻量服务器一台:**2 核 2G、Ubuntu 22.04**(够用;若坚持自托管 Ollama 才需 4G)
- [ ] 域名一个(可选但强烈建议,用于 HTTPS;香港机免备案,域名解析到服务器公网 IP 即可)
- [ ] DeepSeek API key(已有:`sk-9e76...`)
- [ ] 硅基流动 API key:注册 https://siliconflow.cn → 控制台创建密钥(`sk-...`)
- [ ] 本地能跑 `pg_dump`(你开发用的 Postgres 在 `localhost:5433`)

---

## 2. 第一步:本地导出数据库(带 embedding)

在你的开发机执行。把整个 `uq_courses` 库(含已算好的向量和 HNSW 索引定义)导出成一个文件:

```bash
pg_dump "postgresql://postgres:uqrag@localhost:5433/uq_courses" \
  -Fc -f uq_courses.dump
```

- `-Fc` 自定义压缩格式,体积小、恢复快。导出文件预计几十 MB。
- 这样服务器**直接 restore 现成向量**,无需在服务器上跑 `embed.py`、无需 Ollama。

把 `uq_courses.dump` 传到服务器(在服务器执行,或用 scp):

```bash
scp uq_courses.dump root@<服务器公网IP>:/root/
```

---

## 3. 第二步:服务器基础环境

SSH 登录服务器后:

```bash
apt update && apt -y upgrade
apt -y install nginx git python3-venv python3-pip
# Docker(只用来跑 pgvector,最省心)
curl -fsSL https://get.docker.com | sh
```

**防火墙**:在云厂商控制台的"安全组/防火墙"放行 **80、443**;**不要**对公网放行 5432/5433/8077
(Postgres 和后端只在本机访问)。

---

## 4. 第三步:Postgres + pgvector(Docker)+ 导入数据

用官方 `pgvector` 镜像(自带 `vector` 扩展),映射到本机 5433,和代码默认 DSN 一致:

```bash
docker run -d --name uqpg \
  -e POSTGRES_PASSWORD=uqrag \
  -e POSTGRES_DB=uq_courses \
  -p 127.0.0.1:5433:5432 \
  -v uqpg_data:/var/lib/postgresql/data \
  --restart unless-stopped \
  pgvector/pgvector:pg16
```

> 注意 `-p 127.0.0.1:5433:5432`:**只绑本机**,公网访问不到,安全。

等十几秒数据库起来后,导入 dump:

```bash
# 把 dump 拷进容器再恢复
docker cp /root/uq_courses.dump uqpg:/tmp/uq_courses.dump
docker exec -it uqpg pg_restore -U postgres -d uq_courses --no-owner /tmp/uq_courses.dump
```

验证(应能看到 `courses` 表行数,且 embedding 非空):

```bash
docker exec -it uqpg psql -U postgres -d uq_courses \
  -c "SELECT count(*) total, count(embedding) embedded FROM courses;"
```

`total` 和 `embedded` 应该相等(或接近)。若 `embedded` 为 0,说明 dump 没带向量,需回到第二步重导。

---

## 5. 第四步:把 embedding 换成硅基流动

线上问答只用到 [backend/app/services/retrieval.py](backend/app/services/retrieval.py) 里的 `_embed`(`qa.run → retrieval`)。
`query.py` 是旧 CLI、`embed.py` 是建库脚本,都不在 API 链路,**生产无需改它们**。

把 [retrieval.py:84-91](backend/app/services/retrieval.py#L84-L91) 的 `_embed` 替换为(OpenAI 兼容协议,
读环境变量,不写死 key):

```python
EMBED_BASE = os.environ.get("EMBED_BASE", "https://api.siliconflow.cn/v1")
EMBED_API_KEY = os.environ.get("EMBED_API_KEY", "")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")


def _embed(text: str) -> str:
    """取 bge-m3 向量并转成 pgvector 字面量(硅基流动 OpenAI 兼容接口)。"""
    r = requests.post(
        f"{EMBED_BASE}/embeddings",
        headers={"Authorization": f"Bearer {EMBED_API_KEY}"},
        json={"model": EMBED_MODEL, "input": text[:8000], "encoding_format": "float"},
        timeout=60,
    )
    r.raise_for_status()
    v = r.json()["data"][0]["embedding"]
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"
```

要点:
- 旧的 `EMBED_MODEL = "bge-m3"`(retrieval.py:27)可删掉,已由上面的 env 版本覆盖。
- bge-m3 输出 **1024 维**,与库里现有向量同维同模型,**检索结果与本地一致**,不用重算库。
- `input` 截断 8000 与原 `embed.py` 保持一致,防超长。
- 失败直接抛错(`raise_for_status`),符合"不吞错"的项目约定,前端会收到 500 + 原因。

> 若你也想在服务器跑 `query.py` 这个 CLI,同样改它的 `embed`;但 Web 服务用不到。

---

## 6. 第五步:部署后端(uvicorn + systemd)

```bash
cd /root
git clone <你的仓库地址> uq_course_rag    # 或 scp 整个 backend 目录上来
cd uq_course_rag/backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

写生产 `.env`(在 `backend/.env`,注意 **不进 git**):

```ini
# LLM:开启 DeepSeek(线上要 true,你本地是 false)
LLM_ENABLED=true
DEEPSEEK_API_KEY=sk-your-deepseek-api-key

# Embedding:硅基流动
EMBED_API_KEY=sk-你的硅基流动key
# 下面两个有默认值,一般不用写
# EMBED_BASE=https://api.siliconflow.cn/v1
# EMBED_MODEL=BAAI/bge-m3

# 数据库(和 Docker 的 5433 对应,config.py 默认值就是这个,可不写)
# DATABASE_URL=postgresql://postgres:uqrag@localhost:5433/uq_courses
```

> ⚠️ 你当前 `.env` 是 `LLM_ENABLED=false`,线上**必须改 true**,否则会去找本地 Ollama qwen 而服务器没装,问答会失败。

`config.py` 默认 DSN 已是 `localhost:5433`,与上面 Docker 映射一致,可不设 `DATABASE_URL`。

注意 [retrieval.py](backend/app/services/retrieval.py) 默认不自动加载 `.env`(只有 [llm.py](backend/app/services/llm.py) 会),
所以 `EMBED_API_KEY` 要确保进了进程环境。最稳妥:在 systemd 用 `EnvironmentFile` 注入。

建 systemd 服务 `/etc/systemd/system/uqrag.service`:

```ini
[Unit]
Description=UQ Course RAG API
After=network.target docker.service

[Service]
WorkingDirectory=/root/uq_course_rag/backend
EnvironmentFile=/root/uq_course_rag/backend/.env
ExecStart=/root/uq_course_rag/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8077
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

> `--host 127.0.0.1`:后端只在本机听,公网只能通过 nginx 进来。

启动:

```bash
systemctl daemon-reload
systemctl enable --now uqrag
systemctl status uqrag           # 看是否 running
curl -s 127.0.0.1:8077/docs >/dev/null && echo "backend OK"
```

---

## 7. 第六步:构建前端 + nginx

前端调的是相对路径 `/api/*`([ask.ts:36](frontend/src/api/ask.ts#L36)、[sim.ts](frontend/src/api/sim.ts)),
所以同源部署、nginx 反代即可,无需配 CORS、无需改前端代码。

**本地构建**(或在服务器装 node 后构建):

```bash
cd frontend
npm ci
npm run build        # 产出 frontend/dist
```

把 `frontend/dist` 传到服务器 `/var/www/uqrag`:

```bash
scp -r frontend/dist/* root@<服务器IP>:/var/www/uqrag/
```

nginx 配置 `/etc/nginx/sites-available/uqrag`:

```nginx
server {
    listen 80;
    server_name your-domain.com;          # 没域名先填公网 IP

    root /var/www/uqrag;
    index index.html;

    # SPA 路由兜底:刷新非首页也回 index.html
    location / {
        try_files $uri $uri/ /index.html;
    }

    # 反代后端
    location /api/ {
        proxy_pass http://127.0.0.1:8077;
        proxy_set_header Host $host;
        proxy_read_timeout 120s;          # 问答走 DeepSeek,留足超时
    }
}
```

启用:

```bash
ln -s /etc/nginx/sites-available/uqrag /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

此时用 `http://<公网IP>/` 应该能打开页面并正常问答。

---

## 8. 第七步:HTTPS(有域名时)

域名解析 A 记录指向服务器公网 IP 后:

```bash
apt -y install certbot python3-certbot-nginx
certbot --nginx -d your-domain.com
```

certbot 会自动改 nginx 配上 443 和自动续期。完成后用 `https://your-domain.com` 访问。

> 没域名也能跑(纯 IP + http),但浏览器无锁、且部分场景不安全;长期建议上域名 + HTTPS。

---

## 9. 验证清单

逐项确认:

```bash
# 1. 数据库有向量
docker exec -it uqpg psql -U postgres -d uq_courses -c \
  "SELECT count(*) total, count(embedding) embedded FROM courses;"

# 2. 后端活着
curl -s 127.0.0.1:8077/docs >/dev/null && echo backend-ok

# 3. embedding 通(从服务器直接打硅基流动)
curl -s https://api.siliconflow.cn/v1/embeddings \
  -H "Authorization: Bearer $EMBED_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"BAAI/bge-m3","input":"hello"}' | head -c 200

# 4. 端到端问答
curl -s -X POST 127.0.0.1:8077/api/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"有哪些机器学习相关的课"}' | head -c 500

# 5. 前端
curl -s http://localhost/ | grep -q "<div id" && echo frontend-ok
```

第 4 步返回正常 JSON(含 courses / answer)即代表 DeepSeek + 硅基流动 + pgvector 三方全通。

---

## 10. 排错速查

| 现象 | 原因 / 处理 |
|---|---|
| 问答 500,日志提示连不上 11434 | `LLM_ENABLED` 没设 true,跑去找本地 Ollama。改 `.env` 后 `systemctl restart uqrag` |
| 问答 500,embedding 报 401 | `EMBED_API_KEY` 没注入进程。确认 systemd `EnvironmentFile` 生效,`systemctl show uqrag -p Environment` |
| `embedded` 列为 0 | dump 没带向量。重做第二步 `pg_dump`,确认本地库已 `embed.py` 算过 |
| 前端能开但 `/api` 404 | nginx `location /api/` 没生效或后端没起。`nginx -t`、`systemctl status uqrag` |
| 刷新子页面 404 | 缺 SPA 兜底,确认 `try_files ... /index.html` |
| 境内访问慢/偶断 | 香港机正常波动;若严重,考虑换境内机 + 备案 |

查日志:`journalctl -u uqrag -f`(后端)、`tail -f /var/log/nginx/error.log`(nginx)。

---

## 11. 成本与维护

- **服务器**:香港轻量 2C2G 约 ¥24–70/月(常有促销)。
- **DeepSeek**:按量,问答每次几厘到几分,学生量级月几元。
- **硅基流动 embedding**:bge-m3 极便宜(¥/百万 token 级别),query 短,几乎可忽略。
- **更新数据**:本地重新跑 scrape→build_db→embed.py,再 `pg_dump` 覆盖导入服务器即可;前端改了重新 `npm run build` + scp。

---

## 12. 安全注意

- `.env`(含两个 key)**不要进 git**,确认在 `.gitignore` 里。
- Postgres 容器已 `127.0.0.1` 绑定,后端 `--host 127.0.0.1`,公网只暴露 80/443。
- 安全组只放行 80/443/22,**关闭** 5432/5433/8077 的公网入站。
- 定期 `apt upgrade`、certbot 自动续期检查。

---

## 附:如果之后改用"境内服务器 + 备案"

唯一区别是:① 域名要先 ICP 备案(2–3 周,云厂商代办),备案通过前不能绑域名走 80/443;
② 其余步骤完全相同。备案期间可先用香港机过渡,备案下来再迁库(同样 `pg_dump`/`restore`)。
