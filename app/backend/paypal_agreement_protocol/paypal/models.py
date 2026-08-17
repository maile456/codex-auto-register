from dataclasses import dataclass, field, replace
from typing import Optional
import hashlib
import httpx
from pathlib import Path
import random
import re
import unicodedata
import string
import threading
import time
import uuid


@dataclass
class UserInfo:
    first_name: str
    last_name: str
    email: str
    phone: str
    phone_local: str
    phone_country_code: str
    password: str
    dob: str  # DD/MM/YYYY
    cpf: str  # XXX.XXX.XXX-XX
    identity_document_type: str = ""
    identity_document_number: str = ""
    nationality: str = ""
    middle_name: str = ""
    occupation: str = ""
    place_of_birth: str = ""


@dataclass
class CardInfo:
    number: str
    expiry: str  # MM/YYYY
    cvv: str
    card_type: str = "CREDIT"


@dataclass
class BillingAddress:
    street: str
    house_number: str
    district: str
    city: str
    state: str
    postal_code: str
    country: str = "BR"


@dataclass
class SessionState:
    ba_token: str = ""
    ec_token: str = ""
    ssrt: str = ""
    ctx_id: str = ""
    nsid: str = ""
    d_id: str = ""
    user_id: str = ""
    datadome_cookie: str = ""
    tltsid: str = ""
    tltdid: str = ""
    paypal_client_metadata_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    euat_token: str = ""
    return_url: str = ""
    content_hash: str = ""
    content_identifier: str = ""
    signup_url: str = ""
    signup_context_ready: bool = False
    onboarding_url: str = ""
    show_create_account_action_id: str = ""
    create_user_action_id: str = ""

    def update_from_cookies(self, cookies: dict):
        if "nsid" in cookies:
            self.nsid = cookies["nsid"]
        if "d_id" in cookies:
            self.d_id = cookies["d_id"]
        if "datadome" in cookies:
            self.datadome_cookie = cookies["datadome"]
        if "TLTSID" in cookies:
            self.tltsid = cookies["TLTSID"]
        if "TLTDID" in cookies:
            self.tltdid = cookies["TLTDID"]
        euat_key = "AV894Kt2TSumQQrJwe-8mzmyREO"  # gitleaks:allow
        if euat_key in cookies:
            self.euat_token = cookies[euat_key]


def generate_random_email() -> str:
    chars = string.ascii_lowercase + string.digits
    user = "".join(random.choice(chars) for _ in range(12))
    return f"{user}@gmail.com"


def generate_eteid() -> list:
    return [
        random.randint(-10000000000, 20000000000),
        random.randint(-10000000000, 20000000000),
        random.randint(-10000000000, 20000000000),
        random.randint(-10000000000, 20000000000),
        random.randint(-10000000000, 20000000000),
        random.randint(-10000000000, 20000000000),
        None,
        None,
    ]


# --- Random generators for Brazil ---

_BR_FIRST_NAMES = [
    "Lucas", "Gabriel", "Miguel", "Arthur", "Matheus", "Pedro", "Rafael",
    "Gustavo", "Felipe", "Bernardo", "Henrique", "Daniel", "Leonardo",
    "Ana", "Maria", "Julia", "Beatriz", "Larissa", "Fernanda", "Camila",
    "Leticia", "Amanda", "Carolina", "Bruna", "Mariana", "Isabela",
]

_BR_LAST_NAMES = [
    "Silva", "Santos", "Oliveira", "Souza", "Rodrigues", "Ferreira",
    "Almeida", "Nascimento", "Lima", "Araujo", "Pereira", "Carvalho",
    "Ribeiro", "Gomes", "Martins", "Costa", "Barbosa", "Moreira",
    "Mendes", "Cardoso", "Teixeira", "Vieira", "Correia", "Nunes",
]

_GB_FIRST_NAMES = [
    "Oliver", "George", "Harry", "Jack", "Noah", "Charlie", "Thomas",
    "James", "William", "Henry", "Amelia", "Olivia", "Isla", "Emily",
    "Sophie", "Grace", "Charlotte", "Ella", "Lucy", "Alice",
]

_GB_LAST_NAMES = [
    "Smith", "Jones", "Taylor", "Brown", "Williams", "Wilson", "Johnson",
    "Davies", "Patel", "Robinson", "Wright", "Thompson", "Evans",
    "Walker", "White", "Edwards", "Green", "Hall", "Thomas", "Clarke",
]

_BA_FIRST_NAMES = [
    "Amar", "Adnan", "Emir", "Haris", "Tarik", "Kenan", "Mirza", "Nermin",
    "Amina", "Lejla", "Selma", "Emina", "Amra", "Nadja", "Maja", "Sara",
]

_BA_LAST_NAMES = [
    "Hodzic", "Kovacevic", "Dedic", "Basic", "Halilovic", "Music", "Begic", "Delic",
    "Markovic", "Petrovic", "Jovanovic", "Nikolic", "Maric", "Babic", "Ilic", "Popovic",
]

_BH_FIRST_NAMES = [
    "Ahmed", "Mohammed", "Ali", "Hassan", "Yusuf", "Omar", "Khalid", "Salman",
    "Fatima", "Maryam", "Noor", "Aisha", "Sara", "Huda", "Layla", "Amal",
]

_BH_LAST_NAMES = [
    "Al Khalifa", "Al Doseri", "Al Zayani", "Al Arrayed", "Al Jalahma", "Al Mannai",
    "Al Kooheji", "Al Moayyed", "Al Sayed", "Al Ansari", "Al Mahmood", "Al Fardan",
]

_US_FIRST_NAMES = [
    "James", "Michael", "Robert", "John", "David", "William", "Daniel",
    "Matthew", "Joseph", "Christopher", "Emma", "Olivia", "Sophia",
    "Mia", "Amelia", "Harper", "Evelyn", "Abigail", "Emily", "Ella",
]

_US_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
]

_JP_FIRST_NAMES = [
    "Haruto", "Ren", "Yuto", "Sota", "Yuki", "Kaito", "Daiki", "Takumi",
    "Yui", "Aoi", "Hina", "Sakura", "Mei", "Rin", "Akari", "Mio",
]

_JP_LAST_NAMES = [
    "Sato", "Suzuki", "Takahashi", "Tanaka", "Watanabe", "Ito", "Yamamoto",
    "Nakamura", "Kobayashi", "Kato", "Yoshida", "Yamada", "Sasaki",
    "Yamaguchi", "Matsumoto", "Inoue",
]

_TH_FIRST_NAMES = [
    "Niran", "Somchai", "Anan", "Kittisak", "Thanawat", "Preecha",
    "Arthit", "Chaiwat", "Suda", "Siriporn", "Kanya", "Naree",
    "Pimchanok", "Ratchanee", "Sasithorn", "Waranya",
]

_TH_LAST_NAMES = [
    "Srisuk", "Saelim", "Thongchai", "Wongsa", "Chantarangsu",
    "Boonmee", "Kittipong", "Sukprasert", "Rattanakul", "Phromma",
    "Kaewta", "Chaiyaporn", "Suwan", "Inthara", "Wattanakul",
]

_ID_FIRST_NAMES = [
    "Adi", "Agus", "Budi", "Dimas", "Fajar", "Rizky", "Andi", "Bayu",
    "Siti", "Ayu", "Dewi", "Putri", "Rina", "Nadia", "Intan", "Maya",
]

_ID_LAST_NAMES = [
    "Santoso", "Wijaya", "Pratama", "Saputra", "Hidayat", "Setiawan",
    "Kurniawan", "Nugroho", "Permana", "Ramadhan", "Lestari", "Maharani",
    "Anggraini", "Puspita", "Wulandari", "Sari",
]

_PH_FIRST_NAMES = [
    "Juan", "Jose", "Miguel", "Paolo", "Gabriel", "Carlo", "Marco", "Rafael",
    "Maria", "Angela", "Sofia", "Isabella", "Camille", "Patricia", "Bianca", "Andrea",
]

_PH_LAST_NAMES = [
    "Santos", "Reyes", "Cruz", "Garcia", "Mendoza", "Torres", "Flores", "Ramos",
    "Aquino", "Castillo", "Navarro", "Villanueva", "Fernandez", "Gonzales", "Bautista", "Diaz",
]

_TW_FIRST_NAMES = [
    "Wei", "Ming", "Jun", "Hao", "Yu", "Cheng", "Jia", "Kai",
    "Mei", "Ling", "Yun", "Ting", "Hsin", "Chia", "Pei", "An",
]

_TW_LAST_NAMES = [
    "Chen", "Lin", "Huang", "Chang", "Lee", "Wang", "Wu", "Liu",
    "Tsai", "Yang", "Hsu", "Cheng", "Kuo", "Chou", "Yeh", "Su",
]

_MX_FIRST_NAMES = [
    "Santiago", "Mateo", "Sebastian", "Leonardo", "Emiliano", "Diego",
    "Daniel", "Alejandro", "Regina", "Valentina", "Camila", "Sofia",
    "Mariana", "Fernanda", "Daniela", "Ximena",
]

_MX_LAST_NAMES = [
    "Hernandez", "Garcia", "Martinez", "Lopez", "Gonzalez", "Perez",
    "Rodriguez", "Sanchez", "Ramirez", "Cruz", "Flores", "Gomez",
    "Morales", "Vazquez", "Reyes", "Torres",
]

_AE_FIRST_NAMES = [
    "Omar", "Ahmed", "Khalid", "Saeed", "Hamdan", "Rashid", "Yousef", "Majid",
    "Mariam", "Fatima", "Aisha", "Noora", "Hessa", "Latifa", "Amal", "Sara",
]

_AE_LAST_NAMES = [
    "Al Mansoori", "Al Mazrouei", "Al Nuaimi", "Al Ketbi", "Al Suwaidi", "Al Falasi",
    "Al Marri", "Al Shamsi", "Al Hammadi", "Al Dhaheri", "Al Kaabi", "Al Muhairi",
]

_AU_FIRST_NAMES = [
    "Oliver", "Noah", "Jack", "William", "Henry", "Thomas", "James", "Lucas",
    "Charlotte", "Amelia", "Olivia", "Mia", "Isla", "Grace", "Sophie", "Chloe",
]

_AU_LAST_NAMES = [
    "Smith", "Jones", "Williams", "Brown", "Wilson", "Taylor", "Johnson", "White",
    "Martin", "Anderson", "Thompson", "Nguyen", "Walker", "Harris", "Clark", "Lewis",
]

_CA_FIRST_NAMES = [
    "Liam", "Noah", "Oliver", "William", "James", "Lucas", "Benjamin", "Ethan",
    "Olivia", "Emma", "Charlotte", "Amelia", "Sophia", "Ava", "Mia", "Chloe",
]

_CA_LAST_NAMES = [
    "Smith", "Brown", "Tremblay", "Martin", "Roy", "Wilson", "MacDonald", "Gagnon",
    "Johnson", "Taylor", "Cote", "Campbell", "Anderson", "Leblanc", "Lee", "Thompson",
]

_BR_STREETS = [
    "Rua das Flores", "Rua da Paz", "Rua São Paulo", "Rua dos Andradas",
    "Rua Voluntários da Pátria", "Rua General Osório", "Rua Sete de Setembro",
    "Rua Marechal Deodoro", "Rua Quinze de Novembro", "Rua Dom Pedro II",
    "Avenida Brasil", "Avenida Independência", "Avenida Getúlio Vargas",
    "Rua Tiradentes", "Rua Santos Dumont", "Rua Bento Gonçalves",
]

_BR_DISTRICTS = [
    "Centro", "Centro Histórico", "Bela Vista", "Jardim América",
    "Vila Mariana", "Pinheiros", "Consolação", "Liberdade",
    "Santa Cecília", "Moema", "Itaim Bibi", "Perdizes",
]

_KNOWN_BR_ADDRESSES = [
    ("Avenida Cristóvão Colombo", "287", "Savassi", "Belo Horizonte", "MG", "30140-140"),
    ("Rua Siqueira Campos", "946", "Centro Histórico", "Porto Alegre", "RS", "90010-001"),
    ("Avenida Paulista", "1000", "Bela Vista", "São Paulo", "SP", "01310-100"),
    ("Rua da Assembleia", "10", "Centro", "Rio de Janeiro", "RJ", "20011-901"),
    ("Rua XV de Novembro", "100", "Centro", "Curitiba", "PR", "80020-310"),
    ("Avenida Sete de Setembro", "1200", "Centro", "Salvador", "BA", "40060-001"),
]

_KNOWN_DE_ADDRESSES = [
    ("Unter den Linden", "77", "Mitte", "Berlin", "Berlin", "10117"),
    ("Marienplatz", "8", "Altstadt-Lehel", "M\u00fcnchen", "Bayern", "80331"),
    ("M\u00f6nckebergstra\u00dfe", "7", "Hamburg-Altstadt", "Hamburg", "Hamburg", "20095"),
    ("K\u00f6nigsallee", "2", "Stadtmitte", "D\u00fcsseldorf", "Nordrhein-Westfalen", "40212"),
    ("Zeil", "106", "Innenstadt", "Frankfurt am Main", "Hessen", "60313"),
]

_KNOWN_GB_ADDRESSES = [
    # Exact house/street/postcode pairs verified against UK postcode and map data.
    ("Arundel Gardens", "12", "Notting Hill", "London", "Greater London", "W11 2LW"),
    ("Noel Road", "25", "Islington", "London", "Greater London", "N1 8HQ"),
    ("Derngate", "78", "Semilong", "Northampton", "Northamptonshire", "NN1 1UH"),
    ("Forthlin Road", "20", "Allerton", "Liverpool", "Merseyside", "L18 9TL"),
    ("Menlove Avenue", "251", "Woolton", "Liverpool", "Merseyside", "L25 7SA"),
    ("Plymouth Grove", "84", "Chorlton-on-Medlock", "Manchester", "Greater Manchester", "M13 9LW"),
]

_KNOWN_BA_ADDRESSES = [
    ("Zmaja od Bosne", "4", "Marijin Dvor", "Sarajevo", "Federation of Bosnia and Herzegovina", "71000"),
    ("Vladislava Skarica", "8", "Bascarsija", "Sarajevo", "Federation of Bosnia and Herzegovina", "71144"),
    ("Krajina Square", "2", "Centar", "Banja Luka", "Republika Srpska", "78000"),
]

_KNOWN_BH_ADDRESSES = [
    ("Avenue 40", "64", "Al Seef", "Manama", "Capital Governorate", ""),
    ("Road 1705", "144", "Diplomatic Area", "Manama", "Capital Governorate", ""),
    ("Road 2827", "2431", "Seef District", "Manama", "Capital Governorate", ""),
    ("Road 1010", "429", "Sanabis", "Manama", "Capital Governorate", ""),
]

_KNOWN_US_ADDRESSES = [
    ("Pennsylvania Avenue NW", "1600", "Northwest Washington", "Washington", "DC", "20500"),
    ("Fifth Avenue", "350", "Manhattan", "New York", "NY", "10118"),
    ("South Figueroa Street", "900", "Downtown", "Los Angeles", "CA", "90015"),
    ("North Michigan Avenue", "875", "Streeterville", "Chicago", "IL", "60611"),
    ("Congress Avenue", "1100", "Downtown", "Austin", "TX", "78701"),
]

_KNOWN_JP_ADDRESSES = [
    ("Marunouchi", "1-1-1", "Chiyoda-ku", "Chiyoda", "Tokyo", "100-0005"),
    ("Nishishinjuku", "2-8-1", "Shinjuku-ku", "Shinjuku", "Tokyo", "163-8001"),
    ("Umeda", "3-1-1", "Kita-ku", "Osaka", "Osaka", "530-0001"),
    ("Sakae", "3-5-1", "Naka-ku", "Nagoya", "Aichi", "460-0008"),
    ("Minatomirai", "2-2-1", "Nishi-ku", "Yokohama", "Kanagawa", "220-0012"),
]

_KNOWN_TH_ADDRESSES = [
    ("Rama I Road", "991", "Pathum Wan", "Bangkok", "Bangkok", "10330"),
    ("Rama I Road", "999/9", "Pathum Wan", "Bangkok", "Bangkok", "10330"),
    ("Sukhumvit Road", "88", "Khlong Toei", "Bangkok", "Bangkok", "10110"),
    ("Vibhavadi Rangsit Road", "252/1", "Din Daeng", "Bangkok", "Bangkok", "10400"),
    ("Nimmanahaeminda Road", "1", "Suthep", "Mueang Chiang Mai", "Chiang Mai", "50200"),
]

_KNOWN_ID_ADDRESSES = [
    ("Jalan M.H. Thamrin", "1", "Menteng", "Jakarta Pusat", "DKI Jakarta", "10310"),
    ("Jalan Jenderal Sudirman", "52-53", "Senayan", "Jakarta Selatan", "DKI Jakarta", "12190"),
    ("Jalan Asia Afrika", "8", "Gelora", "Jakarta Pusat", "DKI Jakarta", "10270"),
    ("Jalan Malioboro", "52-58", "Suryatmajan", "Yogyakarta", "DI Yogyakarta", "55213"),
    ("Jalan Raya Kuta", "1", "Kuta", "Badung", "Bali", "80361"),
]

_KNOWN_PH_ADDRESSES = [
    ("Ayala Avenue", "6750", "San Lorenzo", "Makati", "Metro Manila", "1226"),
    ("Bonifacio Drive", "1", "Port Area", "Manila", "Metro Manila", "1018"),
    ("Cardinal Rosales Avenue", "1", "Cebu Business Park", "Cebu City", "Cebu", "6000"),
    ("J.P. Laurel Avenue", "123", "Bajada", "Davao City", "Davao del Sur", "8000"),
    ("Lacson Street", "12", "Barangay 5", "Bacolod", "Negros Occidental", "6100"),
]

_KNOWN_TW_ADDRESSES = [
    ("Section 4, Zhongxiao East Road", "45", "Da'an District", "Taipei City", "Taipei City", "106"),
    ("Section 5, Xinyi Road", "7", "Xinyi District", "Taipei City", "Taipei City", "110"),
    ("Section 3, Taiwan Boulevard", "99", "Xitun District", "Taichung City", "Taichung City", "407"),
    ("Zhongshan 1st Road", "1", "Xinxing District", "Kaohsiung City", "Kaohsiung City", "800"),
    ("Zhongzheng Road", "120", "East District", "Tainan City", "Tainan City", "701"),
]

_KNOWN_MX_ADDRESSES = [
    ("Avenida Paseo de la Reforma", "222", "Juarez", "Cuauhtemoc", "Ciudad de Mexico", "06600"),
    ("Avenida Presidente Masaryk", "111", "Polanco", "Miguel Hidalgo", "Ciudad de Mexico", "11560"),
    ("Avenida Vallarta", "6503", "Ciudad Granja", "Zapopan", "Jalisco", "45010"),
    ("Avenida Constituyentes", "1000", "Lomas Altas", "Monterrey", "Nuevo Leon", "64650"),
    ("Boulevard Kukulcan", "12", "Zona Hotelera", "Cancun", "Quintana Roo", "77500"),
]

_KNOWN_AE_ADDRESSES = [
    ("PO Box", "12345", "Downtown Dubai", "Dubai", "Dubai", ""),
    ("PO Box", "2828", "Business Bay", "Dubai", "Dubai", ""),
    ("PO Box", "12900", "Al Danah", "Abu Dhabi", "Abu Dhabi", ""),
    ("PO Box", "5555", "Al Majaz", "Sharjah", "Sharjah", ""),
    ("PO Box", "16000", "Al Nakheel", "Ras Al Khaimah", "Ras Al Khaimah", ""),
    ("PO Box", "777", "Al Nuaimiya", "Ajman", "Ajman", ""),
    ("PO Box", "300", "Al Ras", "Umm Al Quwain", "Umm Al Quwain", ""),
    ("PO Box", "880", "Al Faseel", "Fujairah", "Fujairah", ""),
]

_KNOWN_AU_ADDRESSES = [
    ("Martin Place", "1", "", "Sydney", "NSW", "2000"),
    ("George Street", "200", "", "Sydney", "NSW", "2000"),
    ("Exhibition Street", "8", "", "Melbourne", "VIC", "3000"),
    ("Queen Street", "123", "", "Brisbane", "QLD", "4000"),
    ("St Georges Terrace", "140", "", "Perth", "WA", "6000"),
    ("King William Street", "100", "", "Adelaide", "SA", "5000"),
    ("Franklin Wharf", "1", "", "Hobart", "TAS", "7000"),
    ("London Circuit", "1", "", "Canberra", "ACT", "2601"),
    ("Smith Street", "19", "", "Darwin", "NT", "0800"),
]

_KNOWN_CA_ADDRESSES = [
    ("Queen Street West", "100", "", "Toronto", "ON", "M5H 2N2"),
    ("Bloor Street West", "60", "", "Toronto", "ON", "M4W 3B8"),
    ("Rue Sainte-Catherine Ouest", "1255", "", "Montreal", "QC", "H3G 1P7"),
    ("West Georgia Street", "701", "", "Vancouver", "BC", "V7Y 1G5"),
    ("Stephen Avenue SW", "225", "", "Calgary", "AB", "T2P 2W3"),
    ("Portage Avenue", "360", "", "Winnipeg", "MB", "R3C 0G8"),
    ("Barrington Street", "1800", "", "Halifax", "NS", "B3J 3K8"),
    ("Elgin Street", "90", "", "Ottawa", "ON", "K1P 5E9"),
]

_KNOWN_RO_ADDRESSES = [
    ("Calea Victoriei", "100", "Sector 1", "Bucuresti", "Bucuresti", "010099"),
    ("Strada Memorandumului", "21", "Centru", "Cluj-Napoca", "Cluj", "400114"),
    ("Strada Republicii", "35", "Centrul Vechi", "Brasov", "Brasov", "500030"),
    ("Bulevardul Revolutiei", "78", "Centru", "Arad", "Arad", "310025"),
    ("Strada Stefan cel Mare", "69", "Tasnad", "Tasnad", "Satu Mare", "445300"),
]

_KNOWN_NL_ADDRESSES = [
    ("Dam", "1", "", "Amsterdam", "Noord-Holland", "1012 JS"),
    ("Museumplein", "6", "", "Amsterdam", "Noord-Holland", "1071 DJ"),
    ("Coolsingel", "40", "", "Rotterdam", "Zuid-Holland", "3011 AD"),
    ("Stadhuisbrug", "1", "", "Utrecht", "Utrecht", "3511 KP"),
    ("Grote Markt", "1", "", "Groningen", "Groningen", "9712 HN"),
    ("Markt", "1", "", "Maastricht", "Limburg", "6211 CH"),
]

_MAP_ADDRESS_POOLS = {
    "BR": _KNOWN_BR_ADDRESSES,
    "DE": _KNOWN_DE_ADDRESSES,
    "GB": _KNOWN_GB_ADDRESSES,
    "BA": _KNOWN_BA_ADDRESSES,
    "BH": _KNOWN_BH_ADDRESSES,
    "US": _KNOWN_US_ADDRESSES,
    "JP": _KNOWN_JP_ADDRESSES,
    "TH": _KNOWN_TH_ADDRESSES,
    "ID": _KNOWN_ID_ADDRESSES,
    "PH": _KNOWN_PH_ADDRESSES,
    "TW": _KNOWN_TW_ADDRESSES,
    "MX": _KNOWN_MX_ADDRESSES,
    "AE": _KNOWN_AE_ADDRESSES,
    "AU": _KNOWN_AU_ADDRESSES,
    "CA": _KNOWN_CA_ADDRESSES,
    "NL": _KNOWN_NL_ADDRESSES,
    "RO": _KNOWN_RO_ADDRESSES,
}

_MAP_COUNTRY_NAMES = {
    "BR": "Brazil",
    "DE": "Germany",
    "GB": "United Kingdom",
    "BA": "Bosnia and Herzegovina",
    "BH": "Bahrain",
    "US": "United States",
    "JP": "Japan",
    "TH": "Thailand",
    "ID": "Indonesia",
    "PH": "Philippines",
    "TW": "Taiwan",
    "MX": "Mexico",
    "AE": "United Arab Emirates",
    "RO": "Romania",
    "AU": "Australia",
    "CA": "Canada",
    "NL": "Netherlands",
}

_MAP_ADDRESS_CACHE: dict[tuple[str, tuple[str, ...]], BillingAddress] = {}
_MAP_RESOLVE_LOCK = threading.Lock()
_MAP_LAST_REQUEST_AT = 0.0


def _map_query(country: str, seed: tuple[str, str, str, str, str, str]) -> str:
    street, house_number, district, city, state, postal_code = seed
    parts = [
        f"{house_number} {street}", district, city, state, postal_code,
        _MAP_COUNTRY_NAMES.get(country, country),
    ]
    return ", ".join(part for part in parts if str(part or "").strip())


def _generate_address_from_online_map(country: str) -> Optional[BillingAddress]:
    """Resolve one country-specific seed through OpenStreetMap/Nominatim.

    The request is non-blocking under concurrency: one worker performs the
    external lookup while other workers immediately use the local fallback.
    Successful results are cached and copied before use so later normalization
    cannot mutate the shared cache entry.
    """
    global _MAP_LAST_REQUEST_AT
    country = str(country or "").upper()
    pool = _MAP_ADDRESS_POOLS.get(country) or []
    if not pool:
        return None
    seed = tuple(random.choice(pool))
    cache_key = (country, seed)
    cached = _MAP_ADDRESS_CACHE.get(cache_key)
    if cached is not None:
        return replace(cached)
    if not _MAP_RESOLVE_LOCK.acquire(blocking=False):
        return None
    try:
        cached = _MAP_ADDRESS_CACHE.get(cache_key)
        if cached is not None:
            return replace(cached)
        seed_street, seed_number, seed_district, seed_city, seed_state, seed_postal = seed
        queries = [
            _map_query(country, seed),
            ", ".join(
                part for part in (
                    seed_street, seed_city, seed_state, seed_postal,
                    _MAP_COUNTRY_NAMES.get(country, country),
                ) if str(part or "").strip()
            ),
        ]
        rows = []
        with httpx.Client(
            timeout=httpx.Timeout(6.0),
            trust_env=False,
            headers={"User-Agent": "pay153-address-resolver/2.0"},
        ) as client:
            for query in dict.fromkeys(queries):
                wait_seconds = 1.05 - (time.monotonic() - _MAP_LAST_REQUEST_AT)
                if wait_seconds > 0:
                    time.sleep(wait_seconds)
                response = client.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={
                        "q": query,
                        "format": "jsonv2",
                        "addressdetails": 1,
                        "limit": 1,
                        "countrycodes": country.lower(),
                        "accept-language": "en",
                    },
                )
                _MAP_LAST_REQUEST_AT = time.monotonic()
                response.raise_for_status()
                rows = response.json()
                if rows:
                    break
        if not rows:
            return None
        address = rows[0].get("address") or {}
        house_number = str(address.get("house_number") or seed_number).strip()
        street = str(
            address.get("road")
            or address.get("pedestrian")
            or address.get("residential")
            or address.get("footway")
            or address.get("path")
            or seed_street
        ).strip()
        district = str(
            address.get("suburb")
            or address.get("quarter")
            or address.get("neighbourhood")
            or address.get("borough")
            or address.get("city_district")
            or seed_district
        ).strip()
        city = str(
            address.get("city")
            or address.get("town")
            or address.get("municipality")
            or address.get("village")
            or address.get("county")
            or seed_city
        ).strip()
        state = str(
            address.get("state")
            or address.get("region")
            or address.get("province")
            or address.get("state_district")
            or seed_state
        ).strip()
        postal_code = str(address.get("postcode") or seed_postal).strip().upper()
        if country == "JP":
            digits = re.sub(r"[^0-9]", "", unicodedata.normalize("NFKC", postal_code))
            postal_code = f"{digits[:3]}-{digits[3:]}" if len(digits) == 7 else seed_postal
        if country == "AE":
            state_key = state.lower().replace("-", " ").replace("emirate", "").strip()
            emirates = {
                "abu dhabi": "Abu Dhabi",
                "dubai": "Dubai",
                "sharjah": "Sharjah",
                "ajman": "Ajman",
                "ras al khaimah": "Ras Al Khaimah",
                "ras al khaymah": "Ras Al Khaimah",
                "umm al quwain": "Umm Al Quwain",
                "umm al qaywayn": "Umm Al Quwain",
                "fujairah": "Fujairah",
                "al fujairah": "Fujairah",
            }
            state = emirates.get(state_key, seed_state)
            city = city or seed_city
            postal_code = ""
        if country == "AU":
            state_key = state.lower().strip()
            australian_states = {
                "new south wales": "NSW", "nsw": "NSW",
                "victoria": "VIC", "vic": "VIC",
                "queensland": "QLD", "qld": "QLD",
                "western australia": "WA", "wa": "WA",
                "south australia": "SA", "sa": "SA",
                "tasmania": "TAS", "tas": "TAS",
                "australian capital territory": "ACT", "act": "ACT",
                "northern territory": "NT", "nt": "NT",
            }
            state = australian_states.get(state_key, seed_state)
            city = seed_city
            district = ""
        if country == "CA":
            state_key = state.lower().strip()
            canadian_provinces = {
                "alberta": "AB", "ab": "AB",
                "british columbia": "BC", "bc": "BC",
                "manitoba": "MB", "mb": "MB",
                "new brunswick": "NB", "nb": "NB",
                "newfoundland and labrador": "NL", "nl": "NL",
                "nova scotia": "NS", "ns": "NS",
                "ontario": "ON", "on": "ON",
                "prince edward island": "PE", "pe": "PE",
                "quebec": "QC", "qu?bec": "QC", "qc": "QC",
                "saskatchewan": "SK", "sk": "SK",
                "northwest territories": "NT", "nt": "NT",
                "nunavut": "NU", "nu": "NU",
                "yukon": "YT", "yt": "YT",
            }
            state = canadian_provinces.get(state_key, seed_state)
            city = seed_city
            district = ""
        if not house_number or not street or not city:
            return None
        result = BillingAddress(
            street=street,
            house_number=house_number,
            district=district,
            city=city,
            state=state,
            postal_code=postal_code,
            country=country,
        )
        _MAP_ADDRESS_CACHE[cache_key] = result
        return replace(result)
    except Exception:
        return None
    finally:
        _MAP_RESOLVE_LOCK.release()

def _luhn_checksum(partial: str) -> int:
    """Calculate the Luhn check digit for a partial card number (without the check digit)."""
    digits = [int(d) for d in partial]
    # Process from right to left: double every other digit starting from the rightmost
    for i in range(len(digits) - 1, -1, -2):
        digits[i] *= 2
        if digits[i] > 9:
            digits[i] -= 9
    total = sum(digits)
    return (10 - (total % 10)) % 10


SUIJIDAQUAN_CARD_API = "https://api2.suijidaquan.com/api/v2/random-credit-card"
SUIJIDAQUAN_CARD_REFERER = "https://www.suijidaquan.com/credit-card-generator"


def _normalize_suijidaquan_card_type(value: str) -> str:
    normalized = (value or "").strip().lower().replace(" ", "")
    if normalized == "visa":
        return "VISA"
    if normalized in {"mastercard", "master"}:
        return "MASTER_CARD"
    return ""


def _fetch_suijidaquan_card(count: int = 20, proxy_url: str | None = None) -> Optional[CardInfo]:
    """Fetch one Visa/MasterCard entry from suijidaquan's public generator API."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        ),
        "Origin": "https://www.suijidaquan.com",
        "Referer": SUIJIDAQUAN_CARD_REFERER,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
    }
    payload = {"count": count, "method": "random_credit_card"}

    client_kwargs = {
        "timeout": httpx.Timeout(15.0),
        "headers": headers,
        "trust_env": False,
    }
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    with httpx.Client(**client_kwargs) as client:
        resp = client.post(SUIJIDAQUAN_CARD_API, json=payload)
        resp.raise_for_status()
        result = resp.json()

    if result.get("status") != "ok" or not isinstance(result.get("data"), list):
        raise RuntimeError(f"Unexpected suijidaquan response: {result!r}")

    candidates: list[CardInfo] = []
    for item in result["data"]:
        if not isinstance(item, dict):
            continue
        issuer = _normalize_suijidaquan_card_type(item.get("Credit_Card_Type", ""))
        if issuer not in {"VISA", "MASTER_CARD"}:
            continue
        number = "".join(ch for ch in str(item.get("Credit_Card_Number", "")) if ch.isdigit())
        expiry = str(item.get("Expires", "")).strip()
        cvv = "".join(ch for ch in str(item.get("CVV2", "")) if ch.isdigit())
        if len(number) < 13 or "/" not in expiry or len(cvv) < 3:
            continue
        candidates.append(CardInfo(number=number, expiry=expiry, cvv=cvv[:4], card_type="CREDIT"))

    if not candidates:
        raise RuntimeError("suijidaquan returned no Visa/MasterCard candidates")
    return random.choice(candidates)


# Local generation is the primary card source. The remote generator is retained
# only as a fallback in case the local generator cannot produce a fresh entry.
_FALLBACK_CARD_PREFIXES = [
    ("4", "VISA"),
    ("51", "MASTER_CARD"),
    ("52", "MASTER_CARD"),
    ("53", "MASTER_CARD"),
    ("54", "MASTER_CARD"),
    ("55", "MASTER_CARD"),
]


_LOCAL_CARD_LOCK = threading.Lock()
_LOCAL_CARD_HASH_FILE = (
    Path(__file__).resolve().parents[1] / "data" / "used_local_card_hashes.txt"
)
_LOCAL_CARD_HASHES: set[str] | None = None


def _load_local_card_hashes() -> set[str]:
    global _LOCAL_CARD_HASHES
    if _LOCAL_CARD_HASHES is not None:
        return _LOCAL_CARD_HASHES
    try:
        values = {
            line.strip()
            for line in _LOCAL_CARD_HASH_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    except FileNotFoundError:
        values = set()
    _LOCAL_CARD_HASHES = values
    return values


def _generate_local_unique_card() -> CardInfo:
    """Generate a Luhn-valid card locally and never reuse it on this server."""
    with _LOCAL_CARD_LOCK:
        used = _load_local_card_hashes()
        for _ in range(1000):
            prefix, _issuer = random.choice(_FALLBACK_CARD_PREFIXES)
            remaining = 16 - len(prefix) - 1
            body = prefix + "".join(str(random.randint(0, 9)) for _ in range(remaining))
            number = body + str(_luhn_checksum(body))
            fingerprint = hashlib.sha256(number.encode("ascii")).hexdigest()
            if fingerprint in used:
                continue

            month = random.randint(1, 12)
            year = random.randint(2027, 2031)
            expiry = f"{month:02d}/{year}"
            cvv = f"{random.randint(0, 999):03d}"

            _LOCAL_CARD_HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
            with _LOCAL_CARD_HASH_FILE.open("a", encoding="utf-8") as handle:
                handle.write(fingerprint + "\n")
            used.add(fingerprint)
            return CardInfo(
                number=number,
                expiry=expiry,
                cvv=cvv,
                card_type="CREDIT",
            )
    raise RuntimeError("local unique card generation exhausted")


def generate_card(
    proxy_url: str | None = None,
    *,
    prefer_local: bool = False,
    prefer_remote: bool = False,
) -> CardInfo:
    if prefer_remote:
        for _ in range(4):
            try:
                card = _fetch_suijidaquan_card(proxy_url=proxy_url)
                if card:
                    return card
            except Exception:
                pass
        return _generate_local_unique_card()

    # Always try the local channel first. ``prefer_local`` is kept for backwards
    # compatibility and means the caller does not want a remote fallback.
    try:
        return _generate_local_unique_card()
    except Exception:
        if prefer_local:
            raise

    for _ in range(3):
        try:
            card = _fetch_suijidaquan_card(proxy_url=proxy_url)
            if card:
                return card
        except Exception:
            pass

    # Surface a clear failure if both channels are exhausted.
    raise RuntimeError("local and remote card generation both failed")


def generate_cpf() -> str:
    digits = [random.randint(0, 9) for _ in range(9)]

    # first check digit
    s = sum(d * w for d, w in zip(digits, range(10, 1, -1)))
    r = s % 11
    d1 = 0 if r < 2 else 11 - r
    digits.append(d1)

    # second check digit
    s = sum(d * w for d, w in zip(digits, range(11, 1, -1)))
    r = s % 11
    d2 = 0 if r < 2 else 11 - r
    digits.append(d2)

    d = digits
    return f"{d[0]}{d[1]}{d[2]}.{d[3]}{d[4]}{d[5]}.{d[6]}{d[7]}{d[8]}-{d[9]}{d[10]}"


def generate_indonesia_nik(dob: str, female: bool = False) -> str:
    """Generate a structurally valid 16-digit Indonesian NIK."""
    parts = str(dob or "").split("/")
    day = int(parts[0]) if len(parts) == 3 and parts[0].isdigit() else random.randint(1, 28)
    month = int(parts[1]) if len(parts) == 3 and parts[1].isdigit() else random.randint(1, 12)
    year = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else random.randint(1980, 2000)
    encoded_day = day + 40 if female else day
    # Jakarta/Bali/Yogyakarta regency prefixes followed by DDMMYY + serial.
    district_code = random.choice(("317301", "317302", "317305", "347101", "517101"))
    return f"{district_code}{encoded_day:02d}{month:02d}{year % 100:02d}{random.randint(1, 9999):04d}"


def generate_taiwan_national_id() -> str:
    """Generate a checksum-valid Taiwan national identification number."""
    letter_codes = {
        "A": 10, "B": 11, "C": 12, "D": 13, "E": 14, "F": 15, "G": 16,
        "H": 17, "I": 34, "J": 18, "K": 19, "L": 20, "M": 21, "N": 22,
        "O": 35, "P": 23, "Q": 24, "R": 25, "S": 26, "T": 27, "U": 28,
        "V": 29, "W": 32, "X": 30, "Y": 31, "Z": 33,
    }
    letter = random.choice(tuple(letter_codes))
    body = [random.choice((1, 2))] + [random.randint(0, 9) for _ in range(7)]
    code = letter_codes[letter]
    total = code // 10 + (code % 10) * 9
    total += sum(value * weight for value, weight in zip(body, range(8, 0, -1)))
    check = (-total) % 10
    return letter + "".join(str(value) for value in body) + str(check)


def generate_philippines_national_id() -> str:
    """Generate a 16-digit PhilSys-style card number for the National ID field."""
    return "".join(str(random.randint(0, 9)) for _ in range(16))


def generate_thai_national_id() -> str:
    """Generate a checksum-valid 13-digit Thai national ID number."""
    digits = [random.randint(1, 8)] + [random.randint(0, 9) for _ in range(11)]
    weighted_sum = sum(value * weight for value, weight in zip(digits, range(13, 1, -1)))
    check_digit = (11 - (weighted_sum % 11)) % 10
    return "".join(str(value) for value in digits) + str(check_digit)


def generate_uae_emirates_id(dob: str) -> str:
    """Generate a structurally consistent 15-digit Emirates ID-style number."""
    parts = str(dob or "").split("/")
    year = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else random.randint(1980, 2000)
    base = "784" + f"{year:04d}" + f"{random.randint(0, 9_999_999):07d}"
    total = 0
    for index, char in enumerate(reversed(base)):
        value = int(char)
        if index % 2 == 0:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return base + str((-total) % 10)


def generate_dob() -> str:
    day = random.randint(1, 28)
    month = random.randint(1, 12)
    year = random.randint(1980, 2000)
    return f"{day:02d}/{month:02d}/{year}"


def generate_password() -> str:
    lower = string.ascii_lowercase
    upper = string.ascii_uppercase
    digits_chars = string.digits
    symbols = "!@#$%^"
    pwd = [
        random.choice(lower) for _ in range(6)
    ] + [
        random.choice(upper) for _ in range(3)
    ] + [
        random.choice(digits_chars) for _ in range(3)
    ] + [
        random.choice(symbols) for _ in range(2)
    ]
    random.shuffle(pwd)
    return "".join(pwd)


_SUPPORTED_COUNTRY_CATALOG = Path(__file__).resolve().parents[1] / "data" / "paypal_supported_countries.json"
_SUPPORTED_CALLING_CODES: dict[str, str] | None = None


def _supported_calling_codes() -> dict[str, str]:
    global _SUPPORTED_CALLING_CODES
    if _SUPPORTED_CALLING_CODES is not None:
        return _SUPPORTED_CALLING_CODES
    try:
        import json
        payload = json.loads(_SUPPORTED_COUNTRY_CATALOG.read_text(encoding="utf-8"))
        rows = payload.get("countries") or []
        _SUPPORTED_CALLING_CODES = {
            str(item.get("code") or "").upper(): str(item.get("calling_code") or "")
            for item in rows if isinstance(item, dict) and item.get("code")
        }
    except Exception:
        _SUPPORTED_CALLING_CODES = {}
    return _SUPPORTED_CALLING_CODES


def generate_user(phone: str, country: str = "BR") -> UserInfo:
    country = str(country or "BR").strip().upper()
    profiles = {
        "BR": (_BR_FIRST_NAMES, _BR_LAST_NAMES, "+55"),
        "GB": (_GB_FIRST_NAMES, _GB_LAST_NAMES, "+44"),
        "BA": (_BA_FIRST_NAMES, _BA_LAST_NAMES, "+387"),
        "BH": (_BH_FIRST_NAMES, _BH_LAST_NAMES, "+973"),
        "US": (_US_FIRST_NAMES, _US_LAST_NAMES, "+1"),
        "JP": (_JP_FIRST_NAMES, _JP_LAST_NAMES, "+81"),
        "TH": (_TH_FIRST_NAMES, _TH_LAST_NAMES, "+66"),
        "ID": (_ID_FIRST_NAMES, _ID_LAST_NAMES, "+62"),
        "PH": (_PH_FIRST_NAMES, _PH_LAST_NAMES, "+63"),
        "TW": (_TW_FIRST_NAMES, _TW_LAST_NAMES, "+886"),
        "MX": (_MX_FIRST_NAMES, _MX_LAST_NAMES, "+52"),
        "AE": (_AE_FIRST_NAMES, _AE_LAST_NAMES, "+971"),
        "AU": (_AU_FIRST_NAMES, _AU_LAST_NAMES, "+61"),
        "CA": (_CA_FIRST_NAMES, _CA_LAST_NAMES, "+1"),
    }
    if country in profiles:
        first_names, last_names, phone_country_code = profiles[country]
    else:
        # Generic profile data is only a bootstrap for loading the selected
        # country's signup app. Runtime metadata/KYC resolution runs before
        # mutation submission and supplies the actual country requirements.
        first_names, last_names = _US_FIRST_NAMES, _US_LAST_NAMES
        phone_country_code = _supported_calling_codes().get(country) or "+"
    first = random.choice(first_names)
    last = random.choice(last_names)
    dob = generate_dob()
    identity_document = generate_cpf() if country == "BR" else ""
    identity_document_type = ""
    identity_document_number = ""
    nationality = ""
    if country == "ID":
        female = first in {"Siti", "Ayu", "Dewi", "Putri", "Rina", "Nadia", "Intan", "Maya"}
        identity_document_type = "NATIONAL_ID"
        identity_document_number = generate_indonesia_nik(dob, female=female)
        nationality = "ID"
    elif country == "TH":
        identity_document_type = "NATIONAL_ID"
        identity_document_number = generate_thai_national_id()
        nationality = "TH"
    elif country == "PH":
        identity_document_type = "NATIONAL_ID"
        identity_document_number = generate_philippines_national_id()
        nationality = "PH"
    elif country == "TW":
        identity_document_type = "NATIONAL_ID"
        identity_document_number = generate_taiwan_national_id()
        nationality = "TW"
    elif country == "AE":
        identity_document_type = "NATIONAL_ID"
        identity_document_number = generate_uae_emirates_id(dob)
        nationality = "AE"

    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    calling_code = phone_country_code.lstrip("+")
    # Some number providers return 00<country><number> or duplicate the
    # country code (for example 387387...). Normalize that before runtime
    # country-pattern validation and before sending the GraphQL phone object.
    while digits.startswith("00"):
        digits = digits[2:]
    phone_local = digits[len(calling_code):] if calling_code and digits.startswith(calling_code) else digits
    while calling_code and phone_local.startswith(calling_code) and len(phone_local) > len(calling_code) + 5:
        phone_local = phone_local[len(calling_code):]
    if phone_local.startswith("0") and phone_country_code not in {"", "+"}:
        phone_local = phone_local[1:]
    full_phone = f"{phone_country_code}{phone_local}"

    return UserInfo(
        first_name=first,
        last_name=last,
        email=generate_random_email(),
        phone=full_phone,
        phone_local=phone_local,
        phone_country_code=phone_country_code,
        password=generate_password(),
        dob=dob,
        cpf=identity_document,
        identity_document_type=identity_document_type,
        identity_document_number=identity_document_number,
        nationality=nationality,
    )


def generate_address(country: str = "BR") -> BillingAddress:
    country = str(country or "BR").strip().upper()
    # Known countries already have verified local pools. Avoid blocking every
    # task on a public map service; runtime online resolution remains available
    # for dynamic countries or when PayPal rejects a cached/local address.
    if country == "BR":
        street, house_number, district, city, state, postal_code = random.choice(_KNOWN_BR_ADDRESSES)
    elif country == "DE":
        street, house_number, district, city, state, postal_code = random.choice(_KNOWN_DE_ADDRESSES)
    elif country == "GB":
        street, house_number, district, city, state, postal_code = random.choice(_KNOWN_GB_ADDRESSES)
    elif country == "BA":
        street, house_number, district, city, state, postal_code = random.choice(_KNOWN_BA_ADDRESSES)
    elif country == "BH":
        street, house_number, district, city, state, postal_code = random.choice(_KNOWN_BH_ADDRESSES)
    elif country == "US":
        street, house_number, district, city, state, postal_code = random.choice(_KNOWN_US_ADDRESSES)
    elif country == "JP":
        street, house_number, district, city, state, postal_code = random.choice(_KNOWN_JP_ADDRESSES)
    elif country == "TH":
        street, house_number, district, city, state, postal_code = random.choice(_KNOWN_TH_ADDRESSES)
    elif country == "ID":
        street, house_number, district, city, state, postal_code = random.choice(_KNOWN_ID_ADDRESSES)
    elif country == "PH":
        street, house_number, district, city, state, postal_code = random.choice(_KNOWN_PH_ADDRESSES)
    elif country == "TW":
        street, house_number, district, city, state, postal_code = random.choice(_KNOWN_TW_ADDRESSES)
    elif country == "MX":
        street, house_number, district, city, state, postal_code = random.choice(_KNOWN_MX_ADDRESSES)
    elif country == "AE":
        street, house_number, district, city, state, postal_code = random.choice(_KNOWN_AE_ADDRESSES)
    elif country == "AU":
        street, house_number, district, city, state, postal_code = random.choice(_KNOWN_AU_ADDRESSES)
    elif country == "CA":
        street, house_number, district, city, state, postal_code = random.choice(_KNOWN_CA_ADDRESSES)
    elif country == "NL":
        street, house_number, district, city, state, postal_code = random.choice(_KNOWN_NL_ADDRESSES)
    elif country == "RO":
        street, house_number, district, city, state, postal_code = random.choice(_KNOWN_RO_ADDRESSES)
    else:
        # The runtime address resolver fills this after PayPal returns the
        # selected country's live address schema. Keeping the requested country
        # here is essential because Phase 2 uses it to open the correct form.
        street = house_number = district = city = state = postal_code = ""

    return BillingAddress(
        street=street,
        house_number=house_number,
        district=district,
        city=city,
        state=state,
        postal_code=postal_code,
        country=country,
    )
