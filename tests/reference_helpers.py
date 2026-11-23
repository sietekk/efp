"""Synthetic reference files in each dataset's real format, and a loader context.

Nothing here is real data; names are placeholders, figures are invented except the two values the
selftest recomputes (SMITH's proportion and Mary's 1880 count), which are written into the files on
purpose so the known-value check has something to find."""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from efp.config import Config
from efp.reference import datasets
from efp.runner import make_context
from efp.types import Context
from tests.capturehelpers import make_capture

CENSUS_CSV = (
    "name,rank,count,prop100k,cum_prop100k,pctwhite,pctblack,pctapi,pctaian,pct2prace,pcthispanic\n"
    "SMITH,1,2442977,828.19,828.19,70.9,23.11,0.5,0.89,2.19,2.4\n"
    "JOHNSON,2,1932812,655.24,1483.43,58.97,34.63,0.54,0.94,2.56,2.36\n"
    "ZZZRARE,162253,100,0.03,60000,(S),(S),(S),(S),(S),(S)\n"
    "ALL OTHER NAMES,0,29312001,9936.98,100000,66.7,9.9,2.8,0.7,2.5,17.4\n"
)
SSA_NATIONAL = {
    "yob1880.txt": "Mary,F,7065\nAnna,F,2604\nJohn,M,9655\n",
    "yob1881.txt": "Mary,F,6919\nJohn,M,8769\n",
    "NationalReadMe.pdf": "not a name file",
}
SSA_STATE = {
    "CT.TXT": "CT,F,1910,Mary,300\nCT,M,1910,John,250\n",
    "AK.TXT": "AK,F,1910,Mary,14\n",
    "StateReadMe.pdf": "x",
}
POPULATION_CSV = (
    "SUMLEV,REGION,DIVISION,STATE,NAME,SEX,AGE,ESTBASE2020_CIV,POPEST2020_CIV,POPEST2023_CIV\n"
    "040,1,1,09,Connecticut,0,0,1000,1000,1100\n"
    "040,1,1,09,Connecticut,0,3,1000,1000,1100\n"
    "040,1,1,09,Connecticut,0,4,1000,900,800\n"
    "040,1,1,09,Connecticut,0,5,500,500,600\n"
    "040,1,1,09,Connecticut,0,85,40,40,50\n"
    "040,1,1,09,Connecticut,0,999,9999,9999,9999\n"
    "040,1,1,09,Connecticut,1,0,500,500,550\n"
    "010,0,0,00,United States,0,0,5000,5000,5000\n"
)
FEC_CN = (
    "H0CT01001|SMITH, JOHN Q|DEM|2024|CT|H|01|I|C|C001|1 MAIN ST||HARTFORD|CT|06101\n"
    "S0CT00002|DOE, JANE|REP|2022|CT|S|00|C|C|C002|2 MAIN ST||HARTFORD|CT|06101\n"
    "P00000001|EXAMPLE, ALEX|IND|2024|US|P|00|O|N|C003|||||\n"
)
CONGRESS_YAML = """\
- id: {bioguide: S000001, fec: [H0CT01001, S0CT00099], opensecrets: N0000001, icpsr: 11111, govtrack: 400001}
  name: {first: John, last: Smith, middle: Q., official_full: John Q. Smith}
  bio: {birthday: '1960-05-01', gender: M}
  terms:
  - {type: rep, start: '2005-01-04', end: '2007-01-03', state: CT, party: Democrat, district: 1}
  - {type: sen, start: '2007-01-04', end: '2013-01-03', state: CT, party: Democrat}
- id: {bioguide: D000002}
  name: {first: Jane, last: Doe}
  terms:
  - {type: rep, start: '2019-01-03', end: '2021-01-03', state: NY, party: Republican, district: 3}
"""
OPENSTATES = {
    "people-main/data/ct/legislature/john-smith-1.yml": (
        "id: ocd-person/aaa\nname: John Smith\ngiven_name: John\nfamily_name: Smith\n"
        "party:\n- name: Democratic\nroles:\n- type: lower\n  district: '12'\n"
        "  start_date: '2015-01-01'\n  end_date: '2019-01-01'\n"
    ),
    "people-main/data/ny/retired/jane-doe-2.yml": (
        "id: ocd-person/bbb\nname: Jane Doe\nparty:\n- name: Republican\nroles:\n- type: upper\n"
        "  district: '3'\n  start_date: '2003-01-01'\n  end_date: '2011-01-01'\n"
    ),
    "people-main/data/ct/executive/governor.yml": "id: ocd-person/ccc\nname: Not A Legislator\n",
    "people-main/README.md": "readme",
}
HARVARD_CSV = (
    "ledb_candid,full_name,firstname,lastname,fips,geo_name,state_abb,office_consolidated,year,"
    "pid_est,ballotpedia_url\n"
    "1,gil example,gil,example,06037,los angeles,CA,Prosecutor,1992,D,NA\n"
    "2,pat sample,pat,sample,17031,cook,IL,Sheriff,2018,R,NA\n"
    "3,,,,17031,cook,IL,Sheriff,2018,R,NA\n"
)
FTM_CSV = (
    "candidate_id,candidate_name,state,office,party,election_year,status\n"
    'F1,"DOE, JANE",NY,State Senate,Republican,2018,Won\n'
    'F2,"SAMPLE, PAT",IL,Governor,Democratic,2014,Lost\n'
)
WMN_JSON = json.dumps(
    {"license": ["CC BY-SA 4.0"], "authors": ["x"], "categories": ["social"],
     "sites": [{"name": "ExampleSite", "uri_check": "https://example.test/{account}"}]}
)  # fmt: skip


def zip_bytes(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def dime_sqlite(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        'CREATE TABLE recipients ("bonica.rid" TEXT, cycle INTEGER, name TEXT, lname TEXT, '
        "fname TEXT, mname TEXT, party TEXT, state TEXT, seat TEXT, district TEXT, "
        '"recipient.cfscore" REAL)'
    )
    conn.executemany(
        "INSERT INTO recipients VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "cand1",
                2012,
                "smith, john q",
                "smith",
                "john",
                "q",
                "100",
                "CT",
                "federal:house",
                "CT01",
                -0.7,
            ),
            (
                "cand1",
                2014,
                "smith, john q",
                "smith",
                "john",
                "q",
                "100",
                "CT",
                "federal:house",
                "CT01",
                -0.6,
            ),
            (
                "cand2",
                2016,
                "doe, jane",
                "doe",
                "jane",
                "",
                "200",
                "NY",
                "state:upper",
                "NY03",
                0.8,
            ),
        ],
    )
    conn.commit()
    conn.close()
    return path


def write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


def place(config: Config, dataset: str, filename: str, data: bytes | str) -> Path:
    """A downloaded file as the loaders find it."""
    return write(config.paths.bulk / dataset / filename, data)


def make_ctx(config: Config, conn: sqlite3.Connection) -> tuple[Context, Any]:
    cap = make_capture(
        config,
        conn,
        hosts=(
            "www2.census.gov", "www.ssa.gov", "www.fec.gov", "bucket.example.test",
            "api.open.fec.gov", "api.congress.gov", "v3.openstates.org", "ballotpedia.org",
            "unitedstates.github.io", "example.test",
        ),
    )  # fmt: skip
    return make_context(config, conn).model_copy(update={"capture": cap}), cap


def load_all(ctx: Context) -> dict[str, list[Any]]:
    return {name: ds.load(ctx) for name, ds in datasets().items()}
