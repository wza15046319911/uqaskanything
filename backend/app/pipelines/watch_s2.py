"""watch_s2.py — watch a semester's course profiles going live and load them incrementally (default S2 2026 / 2026:2)

Each run (idempotent, only handles newly appearing courses):
  1. collect_ids gathers the full set of currently published offering ids
  2. diff against the offering_id already in the DB (whole table) -> new this round
  3. for new ids: scraper fetches details -> build_db upsert -> embed fills NULL
  4. at the end of each run (including no-new / error) send one summary email to WATCH_MAIL_TO

If any sub-step fails: email the error + non-zero exit, leave it to the launchd log, do not swallow the error.
Profiles go live in batches; call once a day is enough, load each batch as it appears, until all are out.

Email credentials come from environment variables (or backend/.env, reusing llm's .env loader):
  WATCH_SMTP_USER / WATCH_SMTP_PASS / WATCH_MAIL_TO
  optional WATCH_SMTP_HOST (default smtp.gmail.com) / WATCH_SMTP_PORT (default 465)
If not configured, skip sending and print a note, without affecting the loading.

Usage:
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
