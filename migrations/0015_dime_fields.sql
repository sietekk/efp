-- 0015_dime_fields: what the DIME v4 recipients file carries that identity can use (maintenance 03).
--
-- ref_dime.party now holds the mapped party (the FEC vocabulary: DEM, REP, IND; NULL for any other
-- code) and party_code keeps the raw DIME code. The file also carries committees (recipient.type
-- 'comm'): the loader keeps candidates only and counts the rest in ref_sources.notes_json.
-- nickname is read when a DIME build has an `nname` column (v4.0 does not); gender is stored and
-- not used by the prior; won / primary_won are the general and primary outcome (1 won, 0 lost);
-- ico_status is incumbent / challenger / open seat; fecyear is the FEC election year.

ALTER TABLE ref_dime ADD COLUMN party_code TEXT;
ALTER TABLE ref_dime ADD COLUMN suffix TEXT;
ALTER TABLE ref_dime ADD COLUMN gender TEXT;
ALTER TABLE ref_dime ADD COLUMN won INTEGER;
ALTER TABLE ref_dime ADD COLUMN primary_won INTEGER;
ALTER TABLE ref_dime ADD COLUMN ico_status TEXT;
ALTER TABLE ref_dime ADD COLUMN fecyear INTEGER;
ALTER TABLE ref_dime ADD COLUMN nickname TEXT;
CREATE INDEX idx_ref_dime_rid ON ref_dime(load_id, bonica_rid);
