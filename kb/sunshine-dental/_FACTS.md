# Sunshine Dental: fact sheet (source of truth for the knowledge base)

Fictional practice. Every doc in this folder must agree with this sheet; when
a fact changes, change it here first, then in the docs. Files starting with
`_` are not ingested (scripts/ingest.py).

Practice 4 in Postgres. Hours and providers here must match what the agent's
scheduling tools use (app/scheduling DEFAULT_BUSINESS_HOURS, scripts/seed_fhir.py).

## Identity
- Sunshine Dental, 2400 W Kennedy Blvd, Suite 210, Tampa, FL 33609
- Phone (555) 000-9999 (answered 24/7 by the practice's voice assistant; staff 9–5)
- Email frontdesk@sunshinedental.example; website sunshinedental.example
- Patient portal: portal.sunshinedental.example
- General family dentistry. Founded 2014.

## Team
- Dr. Mei Chen, DDS: general dentist and owner. Exams, fillings, crowns, front-tooth and premolar root canals, simple and surgical extractions, implant crowns, clear aligners, night guards, whitening. Speaks English and Mandarin.
- Sam Rivera, RDH: dental hygienist. Cleanings, deep cleanings, periodontal maintenance, fluoride, sealants, whitening trays.
- Priya Nair: office manager. Billing, insurance, payment plans, records requests.
- Lucia Gómez: patient coordinator. Speaks Spanish, in office Tuesdays and Thursdays.
- Referral partners: Bayshore Oral Surgery (impacted wisdom teeth, implant placement, IV sedation); Tampa Endodontics (molar root canals); Little Teeth Pediatric Dentistry (children under 3).

## Hours
- Monday–Friday 9:00 AM–5:00 PM. Closed Saturday and Sunday. No lunch closure.
- Last appointment of the day starts at 4:30 PM.
- Holidays closed: New Year's Day, Memorial Day, Independence Day, Labor Day, Thanksgiving and the Friday after, Christmas Eve, Christmas Day.
- Same-day emergency slots are held every weekday at 11:00 AM and 3:30 PM.

## Location, parking, access
- Free parking in the garage behind the building, entrance on S Albany Ave. Take a ticket; we validate up to 3 hours at the front desk.
- Two accessible parking spaces on garage level 1, next to the elevator. Metered street parking on Kennedy Blvd.
- Suite 210 is on the 2nd floor; elevator from the lobby. Whole office is wheelchair accessible; one treatment room fits a wheelchair transfer.
- HART bus stop at Kennedy Blvd & Albany Ave, about 50 feet from the entrance.
- Service animals welcome.

## Languages
- English; Spanish (Lucia Gómez, Tue/Thu, and a phone interpreter service any day at no cost); Mandarin (Dr. Chen).

## New patients
- Book a "new patient visit": comprehensive exam, full x-rays, and a cleaning if gums are healthy. 60–90 minutes.
- Arrive 15 minutes early. Bring photo ID, insurance card, list of medications.
- Forms sent by text and email 48 hours before the visit (portal link): medical history, HIPAA acknowledgment, financial policy, treatment consent. If not done online, arrive 30 minutes early to fill them in.
- Previous x-rays less than 12 months old: ask the old office to send them; we may not need new ones.
- Under 18 must come with a parent or legal guardian at the first visit.
- We see children from age 3; under 3 referred to Little Teeth Pediatric Dentistry.

## Reminders
- Text and email reminders 2 days before and 2 hours before. Reply C to confirm, R to ask for a reschedule call.

## Cancellations, no-shows, late arrivals
- Please give at least 24 hours' notice to cancel or reschedule. Monday appointments: by Friday 5:00 PM.
- The first late cancellation or no-show is forgiven. After that: $50 fee per missed appointment or late cancellation (under 24 h).
- Two no-shows within 12 months: a $50 deposit is required to book, applied to the visit.
- Arriving more than 15 minutes late may mean rescheduling, so the next patient isn't delayed.
- Weather closures (hurricanes, tropical storms): we follow Hillsborough County school closures and call everyone affected; no fees.

## Insurance
- In-network (PPO): Delta Dental PPO, Delta Dental Premier, Cigna Dental PPO, MetLife PDP Plus, Aetna Dental PPO, Guardian DentalGuard Preferred, United Concordia.
- Out-of-network PPO plans: we still file the claim for you; you pay the difference between our fee and what the plan pays.
- Not accepted: DHMO/HMO plans (for example Cigna Dental Care DHMO, Aetna DMO), Florida Medicaid dental plans (DentaQuest, MCNA, Liberty), CHIP / Florida Healthy Kids dental.
- Medicare: original Medicare doesn't cover routine dental. Medicare Advantage plans with a dental PPO through Delta Dental, Aetna or Cigna are treated like those PPOs; other Medicare Advantage dental plans: we check case by case.
- We verify benefits 2 business days before your visit and give you an estimate. The estimate isn't a guarantee: the plan decides what it pays.
- Most PPOs cover two cleanings and exams a year at 100%; we can't promise coverage for any specific plan.
- Secondary insurance: we bill secondary plans too.

## Payment
- Your estimated share is due at the time of service.
- Accepted: Visa, Mastercard, American Express, Discover, HSA/FSA cards, Apple Pay, Google Pay, cash, personal checks.
- Statements by email; balance due within 30 days. Credit balances refunded within 30 days on request.
- Financing: CareCredit (6–12 month promotional plans for qualifying patients) and Sunbit. Apply at the front desk or online, decision in minutes.
- In-house payment plan for treatment over $1,000: one third down, the rest in 3 interest-free monthly payments, card on file.

## Membership plan (no insurance)
- Sunshine Smile Plan: $349/year adults, $279/year children under 14.
- Includes 2 exams, 2 regular cleanings, all needed x-rays, 1 emergency exam per year, and 20% off other treatment. No waiting periods, no annual maximum, no pre-existing condition exclusions.
- Not insurance; can't be combined with insurance. Starts the day you sign up.

## Services, CDT codes, self-pay price ranges (before insurance)
Preventive and diagnostic:
- D0150 comprehensive exam (new patient) $95–$125
- D0120 periodic exam $55–$75
- D0140 limited / emergency exam $75–$95
- D0274 four bitewing x-rays $65–$85
- D0210 full-mouth x-rays $140–$180
- D0330 panoramic x-ray $120–$150
- D1110 adult cleaning $105–$135
- D1120 child cleaning $75–$95
- D1208 fluoride varnish $35–$45
- D1351 sealant, per tooth $45–$60
Gum disease:
- D4341 deep cleaning (scaling and root planing), 4+ teeth per quadrant $225–$285 per quadrant
- D4342 deep cleaning, 1–3 teeth per quadrant $160–$200 per quadrant
- D4910 periodontal maintenance $140–$175 (every 3–4 months after deep cleaning)
Fillings and crowns:
- D2391 tooth-colored filling, 1 surface, back tooth $175–$225
- D2392 tooth-colored filling, 2 surfaces, back tooth $220–$280
- D2330 tooth-colored filling, 1 surface, front tooth $160–$210
- No silver (amalgam) fillings placed.
- D2740 porcelain/ceramic crown $1,150–$1,450 (two visits about 2 weeks apart; temporary crown in between)
- D2950 core buildup $250–$325
Root canals:
- D3310 front tooth $850–$1,050
- D3320 premolar $950–$1,200
- D3330 molar: referred to Tampa Endodontics (their fee, typically $1,200–$1,500)
- A crown is usually needed after a back-tooth root canal (separate fee).
Extractions:
- D7140 simple extraction $175–$250
- D7210 surgical extraction $295–$395
- Impacted wisdom teeth (D7240): referred to Bayshore Oral Surgery
Implants:
- Implant placement: Bayshore Oral Surgery. Dr. Chen restores it: D6057 abutment + D6065 implant crown $2,100–$2,600 together, 3–6 months after placement.
Cosmetic:
- In-office whitening (one 90-minute visit) $450–$550
- Take-home whitening trays $250–$325
- Free 20-minute cosmetic consultation
- D8090 clear aligners (comprehensive) $3,800–$5,500, mild to moderate crowding/spacing; free aligner consultation; typical treatment 6–18 months.
Other:
- D9944 night guard (hard, full arch) $425–$550
- D9230 nitrous oxide ("laughing gas") $75–$95 per visit
- D9248 oral conscious sedation $250–$350 per visit
- IV sedation: not offered in-house; Bayshore Oral Surgery.

## Sedation and anxiety
- Comfort menu: headphones, blankets, breaks on request (raise a hand).
- Nitrous oxide: you can drive yourself home after.
- Oral conscious sedation: needs an adult driver to bring you and take you home, and a pre-sedation review with Dr. Chen at a prior visit; you'll get written instructions.

## Emergencies
- During hours: call; same-day emergency slots at 11:00 AM and 3:30 PM, D0140 limited exam.
- After hours / weekends: the voice assistant takes the details; Dr. Chen is on call for patients of record and returns urgent calls within 1 hour between 7:00 AM and 10:00 PM.
- Go to the ER or call 911 for: swelling that affects breathing or swallowing, swelling spreading to the eye or neck, uncontrolled bleeding, a jaw injury or major facial trauma, high fever with facial swelling.
- Knocked-out adult tooth: handle by the crown, keep it in milk (or gently back in the socket), and come in or call right away; within 60 minutes matters most.
- Emergencies for non-patients: we see them in the same-day slots when available.

## Kids and families
- From age 3. Child cleaning D1120, fluoride D1208, sealants D1351.
- Parents may stay with children in the treatment room.
- Family block booking: up to 3 family members back to back.

## Medical notes (office policy, not medical advice)
- Tell us about pregnancy, heart conditions, joint replacements, blood thinners, diabetes, and medication changes; update at every visit.
- Pregnancy: routine cleanings are fine; tell us; x-rays only when needed, with shielding.
- Some heart conditions and joint replacements need antibiotics before treatment: your physician or Dr. Chen decides; tell us before the visit.
- Allergies (latex, local anesthetic) noted in your chart; the office is latex-free.

## Records and privacy
- Copies of records and x-rays: request form at the front desk or portal; free when sent to another dentist; ready within 5 business days.
- HIPAA notice of privacy practices given at the first visit; available on request.
- We never share records without written authorization, except as the law allows (e.g. treatment, payment).

## Second opinions and treatment plans
- Free second-opinion visit (limited exam fee waived) if you bring a treatment plan and x-rays from another office less than 6 months old.
- Treatment plans with estimates are printed and emailed; valid for 90 days.
