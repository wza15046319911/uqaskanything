"""watch_s2.py — 监听某学期 course profile 上线并增量入库(默认 S2 2026 / 2026:2)

每次运行(幂等,只处理新冒出来的课):
  1. collect_ids 采当前已发布的 offering id 全集
  2. 与 DB 已有 offering_id(全表)求差集 -> 本轮新增
  3. 对新 id:scraper 抓详情 -> build_db upsert -> embed 补 NULL
  4. 每次运行结束(含无新增/出错)给 WATCH_MAIL_TO 发一封汇总邮件

任一子步失败:邮件报错 + 非零退出,交给 launchd 日志,不吞错。
profile 分批上线,每天调一次即可,出一批入一批,直到全出齐。

邮件凭据走环境变量(或 backend/.env,复用 llm 的 .env 加载器):
  WATCH_SMTP_USER / WATCH_SMTP_PASS / WATCH_MAIL_TO
  可选 WATCH_SMTP_HOST(默认 smtp.gmail.com) / WATCH_SMTP_PORT(默认 465)
未配置则跳过发送并打印提示,不影响入库。

用法:
    python -m app.pipelines.watch_s2
    python -m app.pipelines.watch_s2 --semester 2026:2 --dry-run
"""
from __future__ import annotations
import os
import ssl
import sys
import smtplib
import argparse
import subprocess
from pathlib import Path
from email.message import EmailMessage

import psycopg

from app.core.config import DSN, DATA_DIR
from app.services.llm import _load_dotenv


def run(cmd: list[str]) -> None:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True)


def existing_ids() -> set[str]:
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT offering_id FROM courses")
        return {r[0] for r in cur.fetchall()}


def notify(subject: str, body: str) -> None:
    user = os.environ.get("WATCH_SMTP_USER")
    pw = os.environ.get("WATCH_SMTP_PASS")
    to = os.environ.get("WATCH_MAIL_TO", user or "")
    if not (user and pw and to):
        print("[watch] 邮件未配置(WATCH_SMTP_USER/PASS/MAIL_TO),跳过发送。", flush=True)
        return
    host = os.environ.get("WATCH_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("WATCH_SMTP_PORT", "465"))
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as s:
        s.login(user, pw)
        s.send_message(msg)
    print(f"[watch] 已发送邮件 -> {to}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--semester", default="2026:2", help="如 2026:2 / 2026:3(summer)")
    ap.add_argument("--location", default="St Lucia", help="校区精确匹配,空串=不限")
    ap.add_argument("--mode", default="In Person", help="授课模式精确匹配,空串=不限")
    ap.add_argument("--delay", type=float, default=1.0, help="抓详情请求间隔秒")
    ap.add_argument("--dry-run", action="store_true", help="只采集+算差集,不抓不入库")
    args = ap.parse_args()

    _load_dotenv()
    py = sys.executable
    work = DATA_DIR / "watch"
    work.mkdir(exist_ok=True)
    tag = args.semester.replace(":", "_")
    all_ids_file = work / f"ids_all_{tag}.txt"
    new_ids_file = work / f"ids_new_{tag}.txt"
    new_jsonl = work / f"new_{tag}.jsonl"

    lines = [f"学期 {args.semester}"]
    new: list[str] = []
    try:
        run([py, "-m", "app.scrapers.collect_ids",
             "--semester", args.semester, "--location", args.location,
             "--mode", args.mode, "--out", str(all_ids_file)])
        current = {ln.strip() for ln in all_ids_file.read_text().splitlines() if ln.strip()}

        have = existing_ids()
        new = sorted(current - have)
        head = f"已发布 {len(current)} | DB 已有(全表) {len(have)} | 本轮新增 {len(new)}"
        lines.append(head)
        print(f"\n[watch] {head}", flush=True)

        if not new:
            lines.append("无新发布,结束。")
        elif args.dry_run:
            lines.append(f"dry-run,新 id 前 20: {new[:20]}")
        else:
            new_ids_file.write_text("\n".join(new) + "\n")
            run([py, "-m", "app.scrapers.scraper",
                 "--file", str(new_ids_file), "--out", str(new_jsonl), "--delay", str(args.delay)])
            scraped = sum(1 for ln in new_jsonl.read_text().splitlines() if ln.strip())
            run([py, "-m", "app.pipelines.build_db", "--in", str(new_jsonl)])
            run([py, "-m", "app.pipelines.embed"])
            lines.append(f"新增 {len(new)} 门:成功抓取入库 {scraped} 门,"
                         f"失败 {len(new) - scraped} 门(下轮自动重试)。")

        notify(f"[UQ-RAG watch] {args.semester} 新增 {len(new)}", "\n".join(lines))
        print("\n[watch] 完成。", flush=True)
    except Exception as e:
        lines.append(f"运行出错:{type(e).__name__}: {e}")
        notify(f"[UQ-RAG watch] {args.semester} 失败", "\n".join(lines))
        raise


if __name__ == "__main__":
    main()
