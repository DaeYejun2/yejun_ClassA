# -*- coding: utf-8 -*-
"""
cleaned_cases.csv -> OpenAI 임베딩(text-embedding-3-small) -> PostgreSQL(pgvector) 적재
"""
import csv
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
import psycopg2
from psycopg2.extras import execute_values

load_dotenv(Path(__file__).parent / ".env")

CSV_PATH = Path(__file__).parent / "cleaned_cases.csv"
EMBED_MODEL = "text-embedding-3-small"
BATCH_SIZE = 50

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )


def embed_batch(texts: list[str]) -> list[list[float]]:
    resp = client.embeddings.create(model=EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]


def main():
    with open(CSV_PATH, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    print(f"{len(rows)}건 로드. 임베딩 생성 시작 (batch={BATCH_SIZE})...")

    conn = get_conn()
    cur = conn.cursor()

    total = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        texts = [r["case_text"] for r in batch]

        t0 = time.time()
        embeddings = embed_batch(texts)
        elapsed = time.time() - t0

        values = []
        for r, emb in zip(batch, embeddings):
            values.append(
                (
                    r["case_id"],
                    r["권역"],
                    r["유형"],
                    r["제목"],
                    r["등록일"],
                    r["민원내용"],
                    r["쟁점"],
                    r["처리결과"],
                    r["소비자유의사항"],
                    r["case_text"],
                    r["상세URL"],
                    emb,
                )
            )

        execute_values(
            cur,
            """
            INSERT INTO fss_dispute_cases
                (case_id, domain, case_type, title, reg_date,
                 complaint, issue, resolution, consumer_note,
                 case_text, source_url, embedding)
            VALUES %s
            ON CONFLICT (case_id) DO UPDATE SET
                embedding = EXCLUDED.embedding
            """,
            values,
        )
        conn.commit()
        total += len(batch)
        print(f"  {total}/{len(rows)}건 적재 완료 ({elapsed:.1f}s/batch)")

    cur.execute("SELECT count(*) FROM fss_dispute_cases")
    print("최종 적재 건수:", cur.fetchone()[0])

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
