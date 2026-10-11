-- 143: the QA pack name is required and constrained — say so in the UI (2026-08-18).
--
-- pack_id stopped being derived (`lang-subject-grade` gave every plain upload
-- "cs-core", so a school's second QA upload silently replaced its first). It is
-- now the REPLACEMENT KEY the curator types, and the gateway rejects an upload
-- without it. The labels have to stop promising the old behaviour: `pack_help`
-- still explains a derivation that no longer exists, and `optional` sits next to
-- a field that is now mandatory.
--
-- The character rule is not cosmetic. The name travels through a URL, a filename
-- and shell commands; diacritics and spaces make each of those need quoting or
-- escaping, and a mis-escaped name creates a SECOND pack instead of replacing the
-- first — the exact failure this change exists to prevent.

UPDATE edu_ai.ui_label SET label_text = 'povinné', notes = 'mig 143: pack_id je nyní povinný'
 WHERE scope = 'upload_test' AND label_key = 'optional' AND lang = 'cs';
UPDATE edu_ai.ui_label SET label_text = 'required'
 WHERE scope = 'upload_test' AND label_key = 'optional' AND lang = 'en';
UPDATE edu_ai.ui_label SET label_text = 'erforderlich'
 WHERE scope = 'upload_test' AND label_key = 'optional' AND lang = 'de';
UPDATE edu_ai.ui_label SET label_text = 'obligatorio'
 WHERE scope = 'upload_test' AND label_key = 'optional' AND lang = 'es';

UPDATE edu_ai.ui_label SET label_text =
 'Jméno packu je <b>povinné</b> a je zároveň <b>klíčem nahrazení</b>: nahrání packu '
 'stejného jména <b>nahradí</b> jeho předchozí verzi u této školy. Povolena jsou '
 'jen <b>písmena bez diakritiky, číslice, <code>-</code> a <code>_</code></b> '
 '(max 64 znaků) — jméno putuje do URL, názvu souboru i příkazové řádky. '
 'Např. <code>prijimacky-2026</code>.'
 WHERE scope = 'upload_test' AND label_key = 'pack_help' AND lang = 'cs';

UPDATE edu_ai.ui_label SET label_text =
 'The pack name is <b>required</b> and is the <b>replacement key</b>: uploading a '
 'pack with the same name <b>replaces</b> this school''s previous version of it. '
 'Only <b>ASCII letters, digits, <code>-</code> and <code>_</code></b> are allowed '
 '(max 64 chars) — the name travels through URLs, filenames and shell commands. '
 'E.g. <code>prijimacky-2026</code>.'
 WHERE scope = 'upload_test' AND label_key = 'pack_help' AND lang = 'en';

UPDATE edu_ai.ui_label SET label_text =
 'Der Paketname ist <b>erforderlich</b> und zugleich der <b>Ersetzungsschlüssel</b>: '
 'das Hochladen eines Pakets mit demselben Namen <b>ersetzt</b> die vorherige Version '
 'dieser Schule. Erlaubt sind nur <b>ASCII-Buchstaben, Ziffern, <code>-</code> und '
 '<code>_</code></b> (max. 64 Zeichen) — der Name wird in URLs, Dateinamen und '
 'Shell-Befehlen verwendet. Z. B. <code>prijimacky-2026</code>.'
 WHERE scope = 'upload_test' AND label_key = 'pack_help' AND lang = 'de';

UPDATE edu_ai.ui_label SET label_text =
 'El nombre del paquete es <b>obligatorio</b> y es la <b>clave de reemplazo</b>: subir '
 'un paquete con el mismo nombre <b>reemplaza</b> la versión anterior de esta escuela. '
 'Solo se permiten <b>letras ASCII, dígitos, <code>-</code> y <code>_</code></b> '
 '(máx. 64 caracteres) — el nombre viaja por URLs, nombres de archivo y comandos de '
 'shell. P. ej. <code>prijimacky-2026</code>.'
 WHERE scope = 'upload_test' AND label_key = 'pack_help' AND lang = 'es';

-- validation message shown next to the field when the typed name breaks the rule
INSERT INTO edu_ai.ui_label (scope, label_key, lang, label_text, is_active, notes) VALUES
 ('upload_test','pack_id_invalid','cs','Nepovolené znaky. Použij jen písmena bez diakritiky, číslice, - a _',true,'mig 143'),
 ('upload_test','pack_id_invalid','en','Not allowed. Use ASCII letters, digits, - and _ only',true,'mig 143'),
 ('upload_test','pack_id_invalid','de','Nicht erlaubt. Nur ASCII-Buchstaben, Ziffern, - und _',true,'mig 143'),
 ('upload_test','pack_id_invalid','es','No permitido. Use solo letras ASCII, dígitos, - y _',true,'mig 143'),
 ('upload_test','pack_id_required','cs','Jméno packu je povinné.',true,'mig 143'),
 ('upload_test','pack_id_required','en','The pack name is required.',true,'mig 143'),
 ('upload_test','pack_id_required','de','Der Paketname ist erforderlich.',true,'mig 143'),
 ('upload_test','pack_id_required','es','El nombre del paquete es obligatorio.',true,'mig 143')
ON CONFLICT DO NOTHING;
