"""Shared fail-closed policy for sensitive facts and adversarial instructions.

This module deliberately has no application-model imports so profile parsing,
form planning, material validation, and legacy adapters can all use the same
classification boundary.
"""

from __future__ import annotations

import re
import unicodedata

_SPACE_RE = re.compile(r"\s+")
_KEY_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_LINE_WRAP_RE = re.compile(r"\r\n?|\n")
_HYPHENATED_LINE_WRAP_RE = re.compile(
    r"(?<=[^\W_])[\-\u058a\u05be\u2010-\u2015\u2e3a\u2e3b]\s*"
    r"(?:\r\n?|\n)\s*(?=[^\W_])",
    re.UNICODE,
)
_INTRAWORD_LINE_WRAP_RE = re.compile(
    r"(?<=[^\W_])\s*(?:\r\n?|\n)\s*(?=[^\W_])",
    re.UNICODE,
)

_SENSITIVE_KEY_MARKERS = (
    "age",
    "ancestry",
    "arrest_history",
    "arrest_record",
    "attest",
    "authorization",
    "authorisation",
    "birth_date",
    "birthdate",
    "citizen",
    "clearance",
    "consent",
    "country_of_birth",
    "country_of_citizenship",
    "country_of_origin",
    "credential",
    "date_of_birth",
    "demographic",
    "disab",
    "dob",
    "employment_eligibility",
    "employment_status",
    "employment_permit",
    "eligible_to_work",
    "entitled_to_work",
    "ethnic",
    "gender",
    "government_id",
    "health_information",
    "hiv_status",
    "immigration",
    "legal_status",
    "legally_eligible",
    "licens",
    "marital",
    "medical_condition",
    "medical_history",
    "health_condition",
    "military",
    "national_origin",
    "nationality",
    "native_country",
    "permit_for_employment",
    "place_of_birth",
    "privacy",
    "professional_registration",
    "pronoun",
    "race",
    "religion",
    "right_to_work",
    "residency",
    "residence_status",
    "sexual_orientation",
    "skin_color",
    "social_security_number",
    "sponsor",
    "ssn",
    "tax_id",
    "taxpayer_identification_number",
    "tin",
    "us_person",
    "green_card",
    "permanent_resident",
    "ead",
    "service_member",
    "contractual_restriction",
    "non_compete",
    "religious_affiliation",
    "faith",
    "certification_status",
    "terms_accepted",
    "applicant_declaration",
    "army_service",
    "authorized_to_work",
    "authorised_to_work",
    "background_check",
    "background_clearance",
    "bar_admission",
    "bar_membership",
    "biometric_data",
    "biometric_information",
    "caregiver_status",
    "caste",
    "conviction_history",
    "criminal_history",
    "criminal_record",
    "creed",
    "export_control",
    "export_license",
    "family_status",
    "genetic_data",
    "genetic_information",
    "indigenous_status",
    "identity_card_number",
    "itar",
    "national_service",
    "national_id",
    "national_identity_number",
    "neurodivergent_status",
    "neurodiversity",
    "parental_status",
    "permit_to_work",
    "political_affiliation",
    "political_beliefs",
    "political_opinions",
    "pregnancy",
    "pregnant",
    "protected_person",
    "right_of_abode",
    "security_vetting",
    "sexual_identity",
    "trade_union",
    "transgender_status",
    "union_membership",
    "veteran",
    "visa",
    "work_eligibility",
    "work_permit",
    "work_right",
    "work_rights",
)
_SENSITIVE_KEY_TOKEN_PREFIXES = (
    "ancestr",
    "arrest",
    "attest",
    "biometr",
    "certif",
    "citizen",
    "convict",
    "criminal",
    "demograph",
    "disab",
    "ethnic",
    "genetic",
    "hiv",
    "licens",
    "politic",
    "pregnan",
    "pronoun",
    "sponsor",
    "transgender",
    "veteran",
)

_SENSITIVE_TEXT_RE = re.compile(
    r"\b(?:"
    r"authori[sz](?:ation|ed|e)|"
    r"citizen(?:ship)?|nationality|national\s+origin|(?:eu|dual)\s+nationals?|"
    r"indigenous\s+status|aboriginal\s+status|first\s+nations?\s+status|"
    r"country\s+of\s+(?:origin|citizenship|birth)|native\s+country|"
    r"right(?:s)?\s+to\s+work|work\s+right(?:s)?|"
    r"(?:work|employment)\s+(?:eligibility|permit)|permit\s+for\s+employment|"
    r"(?:eligible|entitled|permitted|allowed)\s+to\s+(?:work|take\s+employment)|"
    r"legally\s+(?:eligible|entitled)|lawfully\s+(?:work|accept\s+employment)|"
    r"unrestricted\s+(?:work|(?:permission|authori[sz]ation)\s+to\s+work)|"
    r"visa|sponsor(?:ship)?|"
    r"(?:employer|immigration)\s+(?:support|assistance)|"
    r"immigration\s+status|legal\s+status|us\s+person|"
    r"security\s+clearance|security\s+vetting|background\s+(?:check|clearance)|"
    r"classified\s+access|tssci|"
    r"secret\s+(?:access|clearance|cleared)|"
    r"itar|international\s+traffic\s+in\s+arms\s+regulations?|"
    r"export\s+controls?|export\s+(?:control|licen[cs]e)\s+status|"
    r"(?:u\.?\s*s\.?\s+)?protected\s+person(?:\s+status)?|"
    r"licen[cs](?:e|ed|ing)|certif(?:ication|ied|icate)|credential|"
    r"registered\s+professional\s+engineer|cpa|"
    r"demographic|gender|sex|male|female|non\s*binary|"
    r"(?:i(?:\s+am|'m)|the\s+(?:candidate|applicant)\s+is)\s+"
    r"(?:a|an)\s+(?:man|woman)|"
    r"pronouns?|(?:hehim|sheher|theythem)|"
    r"age|how\s+old|years?\s+old|date\s+of\s+birth|birth\s*date|birth\s+date|born|"
    r"identify\s+as\s+(?:a\s+)?(?:man|woman|male|female|non\s*binary)|"
    r"race|ethnicity|ethnic\s+origin|"
    r"disab(?:ility|ilities|led)|workplace\s+accommodation|"
    r"(?:medical|health)\s+condition|"
    r"veteran|armed\s+forces|military\s+(?:service|status)|"
    r"(?:served\s+in\s+(?:the\s+)?(?:idf|israeli\s+army)|"
    r"(?:completed|performed|undertook)\s+national\s+service)|"
    r"national\s+service|army\s+service|"
    r"(?:former\s+)?service\s+member|"
    r"religion|religious|faith|practice\s+(?:judaism|christianity|islam|hinduism)|"
    r"marital\s+status|sexual\s+(?:orientation|identity)|ethnic\s+background|"
    r"social\s+security(?:\s+number)?|(?<![a-z0-9])ssn(?![a-z0-9])|"
    r"national\s+(?:id|identification|identity)(?:\s+(?:card|number))?|"
    r"identity\s+card\s+number|government\s+(?:issued\s+)?id|"
    r"tax(?:payer)?\s+(?:id|identification\s+number)|(?<![a-z0-9])tin(?![a-z0-9])|"
    r"ancestry|skin\s+colou?r|creed|neurodivers(?:ity|e|ent)|"
    r"arrest\s+(?:record|history)|medical\s+history|health\s+information|"
    r"hiv\s+(?:status|positive|negative)|"
    r"political\s+(?:affiliation|beliefs?|opinions?)|"
    r"(?:trade|labor|labour)\s+union(?:\s+membership)?|union\s+membership|"
    r"pregnan(?:cy|t)|parental\s+status|family\s+status|caregiver\s+status|"
    r"genetic\s+(?:information|data)|biometric\s+(?:information|data)|caste|"
    r"transgender\s+(?:status|identity)|"
    r"(?:criminal|conviction)\s+(?:record|history)|criminal\s+convictions?|"
    r"contractual\s+restrictions?|restrictive\s+covenant|non\s*compete|"
    r"(?:bound|restricted)\s+by\s+(?:an?\s+)?agreement|former\s+employer\s+agreement|"
    r"professional\s+(?:registration|license|licence)|"
    r"bar\s+(?:admission|membership)|admitted\s+to\s+(?:the\s+)?bar|"
    r"licensed\s+(?:attorney|lawyer)|"
    r"green\s+card|permanent\s+resident|settled\s+status|employment\s+status|"
    r"employment\s+authorization\s+document|ead|legally\s+employable|"
    r"(?:can|could|may|able|available)\s+(?:you\s+)?(?:legally\s+)?(?:to\s+)?work\s+in|"
    r"(?:can|could|may)\s+work\s+unrestricted|"
    r"(?:without|with\s+no)\s+(?:employment\s+)?restriction|"
    r"passport|permanent\s+residen(?:t|cy)|right\s+of\s+abode|permit\s+to\s+work|"
    r"h\s*1b|(?:f|j)\s*1\s+(?:visa|status)|"
    r"l\s*1\s+visa|o\s*1\s+visa|tn\s+status|"
    r"wheelchair|infantry\s+(?:service|officer)|"
    r"served\s+(?:as|in)\s+(?:an?\s+|the\s+)?(?:infantry|army|navy|air\s+force)|"
    r"how\s+do\s+you\s+identify|"
    r"consent|attest|acknowledge|applicant\s+declaration|"
    r"terms\s+(?:and|&)\s+conditions|terms\s+of\s+service|"
    r"electronic\s+communications?|privacy\s+policy|data\s+processing|"
    r"(?:agree|accept)\s+to\s+(?:the\s+)?(?:privacy|data\s+processing|terms)|"
    r"confirm\b.{0,60}\b(?:information|answers?|statement)\b.{0,30}\baccurate"
    r")\b|"
    r"(?<![\w\u0590-\u05ff])(?:"
    r"אזרחות|אזרח(?:ית|י|ים|יות)?|לאום|לאומיות|מוצא\s+לאומי|"
    r"ארץ\s+המוצא|ארץ\s+מוצא|מדינת\s+מוצא|"
    r"מגדר|מין|גבר|אישה|נקבה|זכר|א\s*בינארי|"
    r"כינויי?\s+ה?גוף|"
    r"גיל|בן\s+כמה|בת\s+כמה|תאריך\s+ה?לידה|נולדתי|נולד|נולדה|"
    r"גזע|מוצא\s+אתני|אתניות|מוגבלות|נכות|התאמות?\s+בעבודה|"
    r"דת|יהדות|מצב\s+משפחתי|זהות\s+מינית|נטי(?:י|)ה\s+מינית|"
    r"מספר\s+ביטוח\s+לאומי|מספר\s+תעודת\s+זהות|תעודת\s+זהות|"
    r"מספר\s+(?:זיהוי\s+)?מס|מוצא\s+משפחתי|צבע\s+עור|אמונה\s+דתית|"
    r"מגוון\s+נוירולוגי|נוירודיברסיות|נוירודיברגנטי(?:ת)?|"
    r"רישום\s+מעצרים|עבר\s+מעצרים|היסטוריה\s+רפואית|מידע\s+בריאותי|"
    r"סטטוס\s+hiv|נשאות\s+hiv|"
    r"שיוך\s+פוליטי|דעות\s+פוליטיות|אמונות\s+פוליטיות|"
    r"חברות\s+(?:באיגוד\s+מקצועי|בוועד\s+עובדים)|איגוד\s+מקצועי|"
    r"הריון|בהיריון|מצב\s+הורי|סטטוס\s+מטפל|מידע\s+גנטי|"
    r"נתונים\s+ביומטריים|מידע\s+ביומטרי|ק(?:א)?סטה|"
    r"זהות\s+טרנסג(?:׳|'|)נדרית|טרנסג(?:׳|'|)נדר|"
    r"עבר\s+פלילי|רישום\s+פלילי|הרשעות?\s+פליליות?|"
    r"שירות\s+(?:צבאי|לאומי)|שירת(?:ת|תי)\s+בצבא|"
    r"מצב\s+רפואי|הגבל(?:ה|ות)\s+חוזי(?:ת|ות)|הסכם\s+עם\s+מעסיק\s+קודם|"
    r"רישום\s+מקצועי|חברות\s+בלשכת\s+עורכי\s+הדין|רישיון\s+עריכת\s+דין|"
    r"מוצא\s+לאומי|המוצא\s+הלאומי|"
    r"אישור\s+עבודה|היתר\s+עבודה|היתר\s+העסקה|זכאות\s+לעבודה|"
    r"מורש(?:ה|ית)\s+לעבוד|רשאי(?:ת)?\s+לעבוד|זכאי(?:ת)?\s+לעבוד|"
    r"זכות\s+לעבוד|לעבוד\s+כחוק|מניעה\s+חוקית|"
    r"אשרת\s+עבודה|ויזה|אשרה|חסות|ספונסר|"
    r"תמיכת\s+המעסיק|סטטוס\s+הגירה|מעמד\s+חוקי|תושבות\s+קבע|גרין\s+קארד|"
    r"דרכון|תושב(?:ת)?\s+קבע|(?:ב|עם\s+)?כיסא\s+גלגלים|(?:ב)?חיל\s+רגלים|"
    r"באפשרותך\s+לעבוד|יכול(?:ה|/ה)?\s+לעבוד|מותרת\s+העסקתך|"
    r"מעמד\s+תושב\s+קבע|"
    r"ניתן\s+להעסיק\s+אותך\s+כחוק|"
    r"סיווג\s+(?:ביטחוני|בטחוני)|"
    r"הסמכה|מוסמ(?:ך|כת)|תעודה|רישיון|רשיון|"
    r"הסכמה|מסכ(?:ים|ימה|ימים|ימות)|תנאי\s+שימוש|תנאים\s+והגבלות|"
    r"תקשורת\s+אלקטרונית|מדיניות\s+פרטיות|עיבוד\s+מידע|"
    r"הצהרת\s+המועמד|הצהרה|מצהיר(?:ה)?|מאשר(?:ת)?|חתימה"
    r")(?![\w\u0590-\u05ff])",
    re.IGNORECASE,
)

_EN_NATIONALITY_VALUE_RE = re.compile(
    r"^(?:(?:(?:i(?:\s+am|'m)|the\s+(?:candidate|applicant)\s+is)\s+"
    r"(?:(?:a|an)\s+)?)"
    r"|(?:as\s+(?:a|an)\s+))?"
    r"(?:american|australian|austrian|belgian|brazilian|british|bulgarian|"
    r"afghan|albanian|algerian|andorran|angolan|antiguan|argentine|armenian|"
    r"azerbaijani|bahamian|bahraini|bangladeshi|barbadian|belarusian|belizean|"
    r"beninese|bhutanese|bolivian|bosnian|botswanan|bruneian|burkinabe|burundian|"
    r"cambodian|cameroonian|canadian|cape\s+verdean|central\s+african|chadian|"
    r"chilean|chinese|colombian|comorian|congolese|costa\s+rican|croatian|"
    r"cuban|cypriot|czech|danish|djiboutian|dominican|dutch|"
    r"egyptian|emirati|estonian|finnish|french|georgian|german|greek|hungarian|"
    r"ecuadorian|equatorial\s+guinean|eritrean|ethiopian|fijian|filipino|gabonese|"
    r"gambian|ghanaian|grenadian|guatemalan|guinean|guyanese|haitian|honduran|"
    r"icelandic|indian|indonesian|iranian|iraqi|irish|israeli|italian|ivorian|"
    r"jamaican|japanese|jordanian|kazakh|kenyan|kiribati|korean|kuwaiti|kyrgyz|"
    r"laotian|latvian|lebanese|liberian|libyan|liechtensteiner|lithuanian|"
    r"luxembourgish|macedonian|malagasy|malawian|malaysian|maldivian|malian|"
    r"maltese|marshallese|mauritanian|mauritian|mexican|micronesian|moldovan|"
    r"monegasque|mongolian|montenegrin|moroccan|mozambican|burmese|namibian|"
    r"nauruan|nepali|new\s+zealander|nicaraguan|nigerien|nigerian|norwegian|"
    r"omani|pakistani|palauan|palestinian|panamanian|papua\s+new\s+guinean|"
    r"paraguayan|peruvian|polish|portuguese|qatari|romanian|russian|rwandan|"
    r"saint\s+lucian|salvadoran|samoan|san\s+marinese|saudi|senegalese|"
    r"serbian|seychellois|sierra\s+leonean|singaporean|somali|"
    r"slovak|slovenian|south\s+african|spanish|sri\s+lankan|swedish|swiss|"
    r"south\s+sudanese|sudanese|surinamese|swazi|syrian|taiwanese|tajik|"
    r"tanzanian|thai|togolese|tongan|trinidadian|tunisian|turkmen|tuvaluan|"
    r"ugandan|uruguayan|uzbek|vanuatuan|venezuelan|vietnamese|yemeni|zambian|"
    r"zimbabwean)(?:\b|$)",
    re.IGNORECASE,
)
_HE_NATIONALITY_VALUE_RE = re.compile(
    r"^(?:אני\s+)?(?:"
    r"ישראלי(?:ת)?|אמריקאי(?:ת)?|בריטי(?:ת)?|קנדי(?:ת)?|אוסטרלי(?:ת)?|"
    r"הודי(?:ת)?|סיני(?:ת)?|צרפתי(?:ת)?|גרמני(?:ת)?|איטלקי(?:ת)?|"
    r"רוסי(?:ת)?|אוקראיני(?:ת)?|פולני(?:ת)?|רומני(?:ת)?|טורקי(?:ת)?|"
    r"איראני(?:ת)?|עיראקי(?:ת)?|מצרי(?:ת)?|ירדני(?:ת)?|לבנוני(?:ת)?|"
    r"סורי(?:ת)?|פלסטיני(?:ת)?|מרוקאי(?:ת)?|ברזילאי(?:ת)?|מקסיקני(?:ת)?"
    r")(?![\w\u0590-\u05ff])"
)
_EN_SELF_NATIONALITY_PREFIX_RE = re.compile(
    r"\b(?:i(?:\s+am|'m)|the\s+(?:candidate|applicant)\s+is)\b",
    re.IGNORECASE,
)
_HE_SELF_NATIONALITY_PREFIX_RE = re.compile(
    r"(?<![\w\u0590-\u05ff])אני\s+",
)

_PROMPT_INJECTION_RE = re.compile(
    r"\b(?:"
    r"ignore|disregard|override"
    r")\s+(?:all\s+)?(?:previous|prior|system|developer)\s+instructions?\b|"
    r"\breveal\s+(?:the\s+)?(?:system\s+prompt|developer\s+message|prompt)\b|"
    r"\bignore\s+(?:embedded\s+)?instructions?\b|"
    r"\b(?:follow|trust)\s+(?:(?:the|this)\s+)?"
    r"(?:page|page\s+text|embedded\s+instructions?)\b|"
    r"\b(?:reveal|return|print)\b.{0,50}\b(?:hidden|private)\b|"
    r"\boverride\s+(?:the\s+)?policy\b|"
    r"\btreat\s+untrusted\b.{0,50}\bas\s+authority\b|"
    r"\bbypass\s+(?:operator\s+)?review\b|"
    r"\binvent\s+(?:candidate\s+)?(?:history|facts?|awards?)\b|"
    r"\b(?:system\s+prompt|developer\s+message|jailbreak)\b|"
    r"\bact\s+as\s+(?:an?\s+)?(?:ai|system|developer)\b|"
    r"(?<![\w\u0590-\u05ff])(?:"
    r"התעלם|התעלמי|התעלמו"
    r")\s+מה(?:הוראות|הנחיות)|"
    r"הוראות\s+קודמות|הנחיות\s+המערכת|חשו(?:ף|פי)\s+את\s+הנחיות",
    re.IGNORECASE,
)


def normalize_policy_text(value: str) -> str:
    """Normalize inclusive punctuation and spacing for deterministic matching."""

    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    normalized = "".join(
        character for character in normalized if unicodedata.category(character) != "Cf"
    ).replace("_", " ")
    normalized = re.sub(r"[/.\u00b7]", "", normalized)
    normalized = re.sub(r"[\-–—]+", " ", normalized)
    return _SPACE_RE.sub(" ", normalized).strip()


def _normalized_policy_variants(value: str) -> tuple[str, ...]:
    """Return bounded variants that reconstruct common PDF line wrapping.

    PDF extraction may split a protected word at a newline, with or without a
    visible hyphen.  The ordinary normalized form preserves word separation;
    the other two forms reconstruct explicit hyphenation and an unmarked
    intra-word wrap.  Callers classify every unique form and still emit only
    the original evidence text.
    """

    raw = str(value)
    dehyphenated = _HYPHENATED_LINE_WRAP_RE.sub("", raw)
    joined = _INTRAWORD_LINE_WRAP_RE.sub("", dehyphenated)
    variants = (
        normalize_policy_text(raw),
        normalize_policy_text(dehyphenated),
        normalize_policy_text(joined),
    )
    return tuple(dict.fromkeys(item for item in variants if item))


def canonical_sensitive_key(value: str) -> str:
    """Return the ASCII key shape used by profile evidence dictionaries."""

    return re.sub(r"_+", "_", _KEY_NORMALIZE_RE.sub("_", str(value).casefold())).strip("_")


def is_sensitive_fact_key(value: str) -> bool:
    """Whether a canonical or loosely named key belongs to a protected class."""

    key = canonical_sensitive_key(value)
    if not key:
        return False
    tokens = tuple(token for token in key.split("_") if token)
    if any(
        token.startswith(prefix) for token in tokens for prefix in _SENSITIVE_KEY_TOKEN_PREFIXES
    ):
        return True
    padded = f"_{key}_"
    return any(f"_{marker}_" in padded for marker in _SENSITIVE_KEY_MARKERS)


def contains_sensitive_text(value: str) -> bool:
    """Detect protected facts in labels, evidence values, and generated prose."""

    return any(
        _SENSITIVE_TEXT_RE.search(normalized)
        or _EN_NATIONALITY_VALUE_RE.search(normalized)
        or _HE_NATIONALITY_VALUE_RE.search(normalized)
        or any(
            _EN_NATIONALITY_VALUE_RE.match(normalized[match.start() :])
            for match in _EN_SELF_NATIONALITY_PREFIX_RE.finditer(normalized)
        )
        or any(
            _HE_NATIONALITY_VALUE_RE.match(normalized[match.start() :])
            for match in _HE_SELF_NATIONALITY_PREFIX_RE.finditer(normalized)
        )
        for normalized in _normalized_policy_variants(value)
    )


def contains_prompt_injection(value: str) -> bool:
    """Detect bounded instruction-override language from untrusted sources."""

    return any(
        _PROMPT_INJECTION_RE.search(normalized) for normalized in _normalized_policy_variants(value)
    )
