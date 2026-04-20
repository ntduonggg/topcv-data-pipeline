import click
import pandas as pd
from tqdm.auto import tqdm
from sqlalchemy import create_engine, Integer, Text, Float, Numeric

dtype = {
    "id":                   "Int64",
    "title":                "string",
    "detail_title":         "string",
    "job_url":              "string",
    "company":              "string",
    "company_name_full":    "string",
    "company_url":          "string",
    "company_url_from_job": "string",
    "salary_list":          "string",
    "detail_salary":        "string",
    "address_list":         "string",
    "detail_location":      "string",
    "exp_list":             "string",
    "detail_experience":    "string",
    "deadline":             "string",
    "tags":                 "string",
    "working_addresses":    "string",
    "working_times":        "string",
    "desc_mota":            "string",
    "desc_yeucau":          "string",
    "desc_quyenloi":        "string",
    "company_website":      "string",
    "company_size":         "string",
    "company_industry":     "string",
    "company_address":      "string",
    "company_description":  "string",
}

@click.command()
@click.option('--pg-user', default='root', help='PostgreSQL user')
@click.option('--pg-pass', default='root', help='PostgreSQL password')
@click.option('--pg-host', default='localhost', help='PostgreSQL host')
@click.option('--pg-port', default=5432, type=int, help='PostgreSQL port')
@click.option('--pg-db', default='topcv_data', help='PostgreSQL database name')
@click.option('--target-table', default='data_engineer_job', help='Target table name')

def run(pg_user, pg_pass, pg_host, pg_port, pg_db, target_table):

    chunksize = 100000

    target_table = 'data_engineer_job'

    engine = create_engine(f'postgresql+psycopg://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}')

    df_iter = pd.read_csv(
        "data-files/topcv_data_jobs.csv",
        dtype=dtype,
        iterator=True,
        chunksize=chunksize,
        encoding="utf-8-sig"
    )

    first = True
    for df_chunk in tqdm(df_iter):
        if first:
            # Create table schema (no data)
            df_chunk.head(0).to_sql(
                name=target_table,
                con=engine,
                if_exists="replace"
            )
            first = False
            print("Table created")

        # Insert chunk
        df_chunk.to_sql(
            name=target_table,
            con=engine,
            if_exists="append"
        )

        print("Inserted:", len(df_chunk))


if __name__ == '__main__':
    run()


