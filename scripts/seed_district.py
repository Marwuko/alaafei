"""Seed a plausible Savelugu district picture for the bootcamp demo.

Clearly synthetic data: names are common Dagomba names, communities are
real Savelugu-area settlements, numbers are placeholders. Wipes existing
referrals and households; leaves nurses and facilities intact.
"""
import glob
import sqlite3
from datetime import datetime, timedelta, timezone

db = glob.glob("*.db")[0]
c = sqlite3.connect(db)
now = datetime.now(timezone.utc)


def ago(h):
    return (now - timedelta(hours=h)).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")


c.execute("DELETE FROM referrals")
c.execute("DELETE FROM households")

# (patient, danger sign, caregiver, community, status, hours_ago)
ROWS = [
    ("Fuseina Alhassan", "heavy bleeding after delivery", "Iddrisu Alhassan", "Diare",      "CLOSED",     52),
    ("Memunatu Sulemana", "baby not feeding, very weak",  "Sulemana Yakubu",  "Nabogu",     "CLOSED",     44),
    ("Ayishetu Mahama",  "severe headache and swelling",  "Mahama Abdulai",   "Pong-Tamale","ARRIVED",    26),
    ("Zulaiha Iddrisu",  "child with convulsions",        "Iddrisu Mumuni",   "Tibung",     "ESCALATED",  58),
    ("Rukaya Abdulai",   "fever for three days",          "Abdulai Seidu",    "Kpalsogu",   "CAREGIVER_NOTIFIED",    9),
    ("Sanatu Yakubu",    "labour pains, first baby",      "Yakubu Adam",      "Nyoglo",     "CAREGIVER_NOTIFIED",    3),
]

for i, (patient, sign, caregiver, community, status, h) in enumerate(ROWS):
    cur = c.execute(
        "INSERT INTO households (caregiver_name, caregiver_number, community, preferred_language)"
        " VALUES (?,?,?,?)",
        (caregiver, f"23320000{i:04d}", community, "dag"),
    )
    hid = cur.lastrowid
    notified = ago(h - 0.1)
    arrived = ago(h - 4) if status in ("ARRIVED", "CLOSED") else None
    escalated = ago(h - 48) if status == "ESCALATED" else None
    cur = c.execute(
        "INSERT INTO referrals (nurse_id, facility_id, household_id, patient_name,"
        " danger_sign, status, created_at, notified_at, arrived_at, escalated_at)"
        " VALUES (1,1,?,?,?,?,?,?,?,?)",
        (hid, patient, sign, status, ago(h), notified, arrived, escalated),
    )

c.commit()
print(f"Seeded {len(ROWS)} referrals into {db}")
for row in c.execute("SELECT id, patient_name, status FROM referrals ORDER BY id"):
    print(" ", row)
