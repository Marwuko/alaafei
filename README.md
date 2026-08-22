# Alaafei

**Voice-first WhatsApp referral companion for CHPS-level maternal and child care in Northern Ghana.**
Built for the UNICEF StartUp Lab *AI for Nurturing Care* Hackathon by Team Alaafei — Felix Marwuko, Akaribo George Namtesime, Abonusum Naomi.

## The problem
In Northern Ghana, maternal mortality stood at 234 per 100,000 live births (2023); the Northern Region alone accounted for 10% of the country's neonatal deaths between 2019 and 2023. When a CHPS nurse refers a mother or sick child to a district facility, no system confirms the family ever arrived. Referrals silently fail to distance, transport cost, and paper records — and nobody finds out in time.

## What Alaafei does
1. **Register** — nurse logs a referral on WhatsApp in under a minute
2. **Notify** — caregiver receives voice notes in their own language (recorded by a midwife): transport reminder, danger signs
3. **Confirm** — receiving facility confirms arrival with one reply
4. **Escalate** — unconfirmed referrals alert the nurse before it's too late
5. **Prioritize** — each morning the nurse gets a ranked household visit list with plain-language reasons

## Design principles
- **Low-connectivity by design**: async store-and-forward messaging; SMS fallback; no caregiver smartphone required; no-phone households route through the nurse
- **AI assists, protocols decide**: escalation and danger-sign logic follows GHS/IMNCI-aligned rules; language models only fill vetted templates; the nurse always decides
- **Voice-first**: caregiver audio is recorded by a real midwife (our team's Naomi), not TTS
- **Idempotent by construction**: WhatsApp retry storms can never double-register a referral

## Run it
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # dev defaults work: SQLite + printed messages
python scripts/seed_demo.py
uvicorn app.main:app --reload
pytest                         # end-to-end loop + escalation tests
```
Without Meta credentials every outbound message prints to the console, so the entire loop runs locally.

## Demo commands (WhatsApp)
```
Nurse:    REFER Amina | heavy bleeding | 2332xxxxxxxx
Facility: ARRIVED 1
Nurse:    CLOSE 1
```

## Status
Bootcamp MVP — referral loop and escalation working end-to-end with tests. Next: morning priority list, district dashboard, voice clip integration.
